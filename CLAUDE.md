# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**pacabot** — a fire-and-forget Python CLI tool for running algorithmic trading strategies against Alpaca brokerage accounts (paper and live). Not yet built; this file captures all confirmed architectural decisions from the planning phase.

## Confirmed Tech Stack

- **alpaca-py** — order execution, account management, market data
- **vectorbt** — offline backtesting
- **pandas / numpy** — data wrangling
- **schedule / asyncio** — job scheduling
- **tomllib** (Python 3.11+ stdlib) — config file parsing

## CLI Invocation

```bash
# Run strategy
python pacabot.py --config configs/paper1-momentum.toml

# Run strategy with log level override
python pacabot.py --config configs/paper1-momentum.toml --log-level debug

# Run backtest
python pacabot.py --config configs/paper1-momentum.toml --backtest --start 2022-01-01 --end 2024-12-31

# Emergency stop — close all positions and exit
python pacabot.py --config configs/paper1-momentum.toml --close-all
```

Each config file represents one account + strategy combination. Only one strategy runs per account at a time. Switching strategies means pointing to a different config file and restarting.

## Config File Format (TOML)

Separate `.toml` files per account/strategy combination. Credentials are never stored in config files. All fields are required — the app exits with a descriptive error on any missing, malformed, or wrong-type value.

```toml
[account]
name = "paper1"
paper = true

[strategy]
name = "cross-sectional-momentum"   # cross-sectional-momentum, mean-reversion, pairs-trading
universe = "sp500"                  # named index slug or "custom" with ticker list
long-only = true                    # false = long/short mode; omit for pairs-trading
order-type = "market"               # market or limit

[strategy.parameters]
# Strategy-specific — see Strategy Parameters section
lookback-period = 252
top-n = 10
rebalance-frequency = "monthly"

[execution]
trading-start = "09:45"             # HH:MM Eastern; false to disable restriction
trading-end = "15:45"               # HH:MM Eastern; false to disable restriction

[risk]
margin-utilization-cap = 0.70       # max buying power used during normal operation
margin-warning-threshold = 0.80     # stop new positions above this utilization
margin-critical-threshold = 0.90    # flatten all + halt above this utilization
stop-loss = 0.05                    # fixed % per position, or false for none
max-position-size = 0.10            # % of account equity
max-position-size-usd = 5000.00     # dollar cap; more restrictive of the two is applied
max-positions = 10                  # hard cap on open positions, or 0 for uncapped
daily-loss-limit = 0.02             # halt if down X% on the day, or false to disable

[logging]
log-level = "warning"               # critical, error, warning, info, debug
log-file = "pacabot.log"            # path relative to working directory
```

## API Credentials

Always via environment variables — never in config files:

```bash
export ALPACA_API_KEY="..."
export ALPACA_SECRET_KEY="..."
```

## Supported Strategies

| Strategy | Description |
|---|---|
| `cross-sectional-momentum` | Rank universe by trailing return, hold top N, rebalance on schedule |
| `mean-reversion` | Buy oversold large-caps, exit at mean reversion; signal via RSI, Bollinger Bands, or z-score |
| `pairs-trading` | Long/short correlated asset pairs on spread z-score divergence |

Options, forex, and crypto are explicitly out of scope.

## Supported Universes

| Slug | Index |
|---|---|
| `sp500` | S&P 500 |
| `sp100` | S&P 100 |
| `nasdaq100` | Nasdaq 100 |
| `russell1000` | Russell 1000 |
| `russell2000` | Russell 2000 |
| `dow30` | Dow Jones Industrial Average |
| `custom` | User-defined ticker list in config |

Asset class filters (e.g. `"etf_liquid"`) are not supported. Universe is set per config file — no defaults. For pairs trading, universe is always `custom` with pairs explicitly defined as ticker pairs:

```toml
[strategy]
name = "pairs-trading"
universe = "custom"

[strategy.parameters]
pairs = [["KO", "PEP"], ["XOM", "CVX"], ["JPM", "BAC"]]
```

The README.md Configuration section should show the most sensible named index per strategy as a best-practice example (not a code default):
- Cross-sectional momentum → `sp500` or `russell1000` (broad universe improves ranking signal)
- Mean reversion → `sp100` or `sp500` (large-cap stocks are more liquid and more mean-reverting)
- Pairs trading → `custom` always (pairs must be user-researched and explicitly defined)

## Margin Management

Two-layer approach:

**Layer 1 — Preventive:** `margin-utilization-cap` constrains position sizing at order time so the bot structurally cannot over-leverage under normal conditions.

**Layer 2 — Reactive:** Continuous monitoring with two configurable thresholds:

| Threshold | Action |
|---|---|
| `margin-warning-threshold` | Stop opening new positions |
| `margin-critical-threshold` | Flatten all positions, halt bot, log reason |

After a critical halt, **manual restart is required** — the bot does not auto-resume. The operator must inspect the account, resolve the margin situation, and restart with `--config`.

Monitored via Alpaca account fields: `equity`, `buying_power`, `initial_margin`, `maintenance_margin`.

## Backtesting

Triggered via CLI flags alongside `--config`. Date range specified with `--start` and `--end` (YYYY-MM-DD).

**Data source:** Alpaca historical data API (via `alpaca-py`). No external data provider.

**Outputs — all three are always produced:**

| Output | Format | Location |
|---|---|---|
| Performance summary | Printed to console | stdout |
| Equity curve + drawdown chart | HTML (Plotly via vectorbt) | `./backtest_results/` |
| Trade log + daily returns | CSV | `./backtest_results/` |

Console summary metrics: Sharpe ratio, CAGR, max drawdown, win rate, total trades.

The HTML file is automatically opened in the default browser after the backtest completes. Output files are named with strategy name and date range, e.g. `momentum_2022-01-01_2024-12-31.html`.

## Monitoring & Observability

### Logging

Output to both console and log file simultaneously. The `--log-level` CLI flag takes precedence over the config value when both are set.

| Config / CLI value | Python level | What is logged |
|---|---|---|
| `critical` | CRITICAL | Fatal errors only (margin call halt, daily loss halt) |
| `error` | ERROR | Errors that affect operation (order rejection, API failure) |
| `warning` | WARNING | Non-fatal issues (margin warning threshold crossed, partial fill) |
| `info` | INFO | Trade executions, position changes, rebalance events |
| `debug` | DEBUG | Full verbosity — all signals, decisions, order lifecycle events |

### Trade Notifications

Console and log file only — no external services (no email, Slack, SMS). All order events (submission, fill, rejection, cancellation) are logged at `info` level.

### End-of-Day Report

A daily P&L summary is printed to console and saved to `./reports/` at market close each trading day, named e.g. `report_2024-11-15.txt`.

Minimum contents:
- Date, strategy name, account name
- Starting and ending equity
- Day P&L (dollar and %)
- Open positions and unrealised P&L
- Trades executed during the day (count, total volume)
- Current margin utilization

## Order Execution

| Order purpose | Type | TIF | Notes |
|---|---|---|---|
| Position entry | Market or limit (configurable) | DAY | Limit orders may not fill if price moves away |
| Stop loss | Stop | GTC | Persists across sessions; protects overnight positions |
| Take profit / exit target | Limit | GTC | Active until position closes |
| Strategy-driven exit | Market | DAY | Signal-based exits at current price |

`order-type` is set in `[strategy]`. `trading-start` and `trading-end` in `[execution]` restrict order submission to a window within market hours; signals outside the window are skipped until the next valid window. Set either to `false` to disable the restriction.

**GTC order reconciliation on startup:** On startup the bot must fetch all open GTC orders from Alpaca and reconcile them against current positions to avoid placing duplicate stop losses on existing positions.

## Process Lifecycle

The bot runs **24/7** as a long-lived process. It does not exit at market close — it sleeps during off-hours and resumes at market open. Must handle:

- Market hours detection via Alpaca's clock API (`/v2/clock`)
- Weekends and market holidays — sleep until next open
- Network interruptions — reconnect with backoff, log the outage
- Alpaca API downtime — retry with exponential backoff, do not crash

## Emergency Stop

`--close-all` immediately cancels all open orders and closes all positions at market price, then exits. Does not run the strategy.

Behavior:
1. Cancel all open orders (DAY and GTC)
2. Submit market sell orders for all open long positions
3. Submit market buy-to-cover orders for all open short positions
4. Log all actions at `critical` level
5. Exit cleanly

## Startup Behavior

All three strategies hold positions overnight — pre-existing positions at startup are the normal case, not an exception. On startup the bot must:

1. Fetch all open positions from Alpaca
2. Fetch all open GTC orders and match them to positions
3. Identify positions missing stop loss orders and re-submit them
4. Resume signal evaluation and monitoring as normal

Requiring a flat account before running is not supported.

## Config Validation

All parameters must be explicitly set — there are no application defaults. On startup, validate all required fields and exit with a descriptive error for any:
- Missing required parameter
- Wrong type (e.g. string where integer expected)
- Invalid value (e.g. unknown strategy name, unknown universe slug, negative position size)

## README.md Requirements

The README.md must include a **Configuration** section containing:
- A copyable example `.toml` config file for each strategy
- Parameter reference tables per strategy with valid values, units, and descriptions
- Conventional/recommended values clearly indicated (not enforced in code)
- Best-practice universe recommendations per strategy
- Lookback period value vs. characteristic table (for momentum)
- `top-n` retail scale tradeoff explanation (for momentum)
- All mean reversion indicator parameter tables (RSI, Bollinger Bands, z-score)
- Pairs trading parameter reference table

## Scope Decisions (Planning Phase)

The following have been confirmed; do not re-litigate unless the user raises them:

- **Asset scope:** U.S. public exchange stocks and ETFs only
- **Account types:** Both paper and live Alpaca accounts
- **Interface:** Fire-and-forget CLI (no interactive prompt)
- **Concurrency:** One strategy per account at a time
- **Config format:** TOML, separate file per account/strategy
- **Credentials:** Environment variables only
- **Long/short:** Controlled by single `long-only` flag for momentum and mean reversion. Pairs trading is inherently long/short — flag not applicable.

## Strategy Parameters

### Cross-Sectional Momentum

| Parameter | Type | Valid values | Notes |
|---|---|---|---|
| `lookback-period` | integer | Any positive integer (days) | README documents common values and tradeoffs |
| `top-n` | integer | Any positive integer | Fixed count only — no percentage option. README documents retail scale tradeoffs |
| `rebalance-frequency` | string | `"weekly"`, `"monthly"` | `daily` not supported — PDT risk and API rate limit concerns |

- Minimum holding period: not supported
- In long/short mode (`long-only = false`): short count mirrors `top-n` exactly (e.g. `top-n = 10` → 10 longs + 10 shorts of bottom-ranked assets)

```toml
[strategy]
name = "cross-sectional-momentum"
universe = "sp500"
long-only = true
order-type = "market"

[strategy.parameters]
lookback-period = 252
top-n = 10
rebalance-frequency = "monthly"
```

### Mean Reversion

Supports three indicators: `rsi`, `bollinger-bands`, `zscore`. One indicator is selected per config file via `indicator`. Only the selected indicator's sub-table is required — the others must be omitted.

`max-holding-days` is a **loss-only time stop** — only exits if the position is currently at a loss after N days. Profitable positions are held until the indicator exit signal fires. Set to `false` to disable.

| Parameter | Indicator | Validation |
|---|---|---|
| `period` | RSI, Bollinger Bands, Z-score | Positive integer |
| `entry-threshold` | RSI | 1–49 |
| `exit-threshold` | RSI | > `entry-threshold`, max 99 |
| `std-dev` | Bollinger Bands | Positive float |
| `exit-band` | Bollinger Bands | `"middle"` or `"upper"` |
| `entry-threshold` | Z-score | Any negative float |
| `exit-threshold` | Z-score | Any float > `entry-threshold` (including positive) |
| `max-holding-days` | All | Positive integer or `false` |

Bollinger Bands `exit-band`: `"middle"` exits at the rolling mean (conventional); `"upper"` exits at the upper band, letting profits run further.

Example using RSI (only the active indicator's sub-table is included):

```toml
[strategy]
name = "mean-reversion"
universe = "sp500"
long-only = true
order-type = "market"

[strategy.parameters]
indicator = "rsi"
max-holding-days = 10

[strategy.parameters.rsi]
period = 14
entry-threshold = 30
exit-threshold = 50
```

### Pairs Trading

Pairs are always pre-defined in config — no dynamic discovery. Hedge ratio uses OLS (ordinary least squares), recalculated at each rebalance. Both `stop-loss-zscore` and the position-level `stop-loss` in `[risk]` apply independently — whichever triggers first exits the position.

| Parameter | What it controls | Validation | Conventional value |
|---|---|---|---|
| `pairs` | List of ticker pairs to trade | Min 1 pair; each pair must be exactly 2 tickers | — |
| `lookback-period` | Rolling window for spread mean, std dev, and OLS hedge ratio | Positive integer (days) | 60 |
| `entry-zscore` | Enter when spread z-score exceeds this magnitude | Positive float | 2.0 |
| `exit-zscore` | Exit when spread z-score reverts within this of zero | Positive float < `entry-zscore` | 0.5 |
| `stop-loss-zscore` | Cut losses if spread widens beyond this (broken thesis) | Positive float > `entry-zscore` | 3.0 |
| `recalculate-frequency` | How often OLS and spread stats are recalculated | `"weekly"` or `"monthly"` | — |

`exit-zscore` must be < `entry-zscore`. `stop-loss-zscore` must be > `entry-zscore`.

```toml
[strategy]
name = "pairs-trading"
universe = "custom"
order-type = "market"

[strategy.parameters]
lookback-period = 60
entry-zscore = 2.0
exit-zscore = 0.5
stop-loss-zscore = 3.0
recalculate-frequency = "monthly"
pairs = [["KO", "PEP"], ["XOM", "CVX"], ["JPM", "BAC"]]
```
