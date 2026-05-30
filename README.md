# pacabot

A fire-and-forget Python CLI tool for running algorithmic trading strategies against Alpaca brokerage accounts (paper and live).

**Supported strategies:** Cross-Sectional Momentum · Mean Reversion · Pairs Trading

**Supported assets:** U.S. public exchange stocks and ETFs only. Options, forex, and crypto are not supported.

---

## Installation

Python 3.11 or later is required.

```bash
pip install -r requirements.txt
```

## API Credentials

Set your Alpaca API keys as environment variables. Never put them in config files.

```bash
export ALPACA_API_KEY="your-api-key"
export ALPACA_SECRET_KEY="your-secret-key"
```

Use your **paper trading** keys when `paper = true` in the config, and your **live trading** keys when `paper = false`.

---

## Usage

```bash
# Run a strategy (fire-and-forget, runs 24/7)
python pacabot.py --config configs/my-strategy.toml

# Override log level for this session
python pacabot.py --config configs/my-strategy.toml --log-level debug

# Run a backtest
python pacabot.py --config configs/my-strategy.toml --backtest --start 2022-01-01 --end 2024-12-31

# Emergency stop: cancel all orders and close all positions immediately
python pacabot.py --config configs/my-strategy.toml --close-all
```

### Running in the background (tmux recommended)

```bash
tmux new -s pacabot
python pacabot.py --config configs/my-strategy.toml
# Ctrl+B, D to detach
```

---

## Configuration

Each config file defines one account and one strategy. Create a separate `.toml` file per account/strategy combination.

Copy an example config from `configs/` and adjust it for your needs.

### `[account]`

| Field | Type | Description |
|---|---|---|
| `name` | string | Friendly label for this account (used in logs and reports) |
| `paper` | boolean | `true` for paper trading, `false` for live trading |

### `[strategy]`

| Field | Type | Valid values | Description |
|---|---|---|---|
| `name` | string | `cross-sectional-momentum`, `mean-reversion`, `pairs-trading` | Strategy to run |
| `universe` | string | See [Universes](#universes) | Asset universe to trade |
| `long-only` | boolean | `true` / `false` | `true` = long only; `false` = long/short. **Omit for pairs-trading.** |
| `order-type` | string | `market`, `limit` | Order type for position entries |
| `tickers` | list | e.g. `["AAPL","MSFT"]` | Required when `universe = "custom"` and strategy is not pairs-trading |

### `[execution]`

| Field | Type | Description |
|---|---|---|
| `trading-start` | string or `false` | Earliest time to submit orders (HH:MM Eastern). `false` to disable. |
| `trading-end` | string or `false` | Latest time to submit orders (HH:MM Eastern). `false` to disable. |

### `[risk]`

| Field | Type | Description |
|---|---|---|
| `margin-utilization-cap` | float (0–1) | Max fraction of buying power used in normal operation |
| `margin-warning-threshold` | float (0–1) | Stop opening positions above this utilization |
| `margin-critical-threshold` | float (0–1) | Flatten all positions and halt above this utilization |
| `stop-loss` | float or `false` | Per-position stop loss as fraction of entry price. `false` = none. |
| `max-position-size` | float (0–1) | Max position as fraction of account equity |
| `max-position-size-usd` | float | Max position in dollars. The more restrictive limit is applied. |
| `max-positions` | integer | Max simultaneous open positions. `0` = uncapped. |
| `daily-loss-limit` | float or `false` | Halt trading if account is down this fraction on the day. `false` = none. |

### `[logging]`

| Field | Type | Valid values | Description |
|---|---|---|---|
| `log-level` | string | `critical`, `error`, `warning`, `info`, `debug` | Verbosity level |
| `log-file` | string | File path | Log file path (relative to working directory) |

---

## Universes

| Slug | Index | Size | Best for |
|---|---|---|---|
| `sp500` | S&P 500 | ~500 | Momentum, mean reversion |
| `sp100` | S&P 100 | 100 | Mean reversion (most liquid large-caps) |
| `nasdaq100` | Nasdaq 100 | 100 | Momentum (tech-heavy) |
| `russell1000` | Russell 1000 | ~1,000 | Momentum (broad large+mid cap) |
| `russell2000` | Russell 2000 | ~2,000 | Small-cap exposure (higher turnover) |
| `dow30` | Dow Jones Industrial Average | 30 | Conservative, highly liquid |
| `custom` | User-defined | Any | Pairs trading (always); custom stock lists |

**Best-practice recommendations (not enforced):**
- Cross-sectional momentum → `sp500` or `russell1000` (broad universe improves ranking signal)
- Mean reversion → `sp100` or `sp500` (large-caps are more liquid and more reliably mean-reverting)
- Pairs trading → always `custom` (pairs must be individually researched)

---

## Strategy Reference

### Cross-Sectional Momentum

Ranks all assets in the universe by trailing return and holds the top N. Rebalances on a schedule.

#### Parameters

| Parameter | Type | Valid values | Description |
|---|---|---|---|
| `lookback-period` | integer | Any positive integer | Days of trailing return used to rank assets |
| `top-n` | integer | Any positive integer | Number of top-ranked assets to hold long (and short in long/short mode) |
| `rebalance-frequency` | string | `weekly`, `monthly` | How often to re-rank and rebalance |

#### Lookback Period Guide

| Value | Characteristic |
|---|---|
| 21 days (~1 month) | Short-term, high turnover, more noise |
| 63 days (~3 months) | Medium-term, balanced |
| 126 days (~6 months) | Intermediate momentum |
| 231 days (11 months) | Classic academic momentum — skips short-term reversal |
| 252 days (~1 year) | Full-year momentum, lowest turnover |

The 231-day (11-month, skip-last-month) variant has the strongest academic backing (Jegadeesh & Titman 1993).

#### Position Count Note

Avoid setting `top-n` to a large percentage of a large universe. On a $50,000 account with `top-n = 50` from S&P 500:

- Average position size = $50,000 / 50 = **$1,000 per position**
- At $20/share that is only 50 shares per position
- Slippage and `max-position-size-usd` limits will override and reduce positions further

For most retail accounts, `top-n` between **5 and 20** is most practical.

#### Example Config

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

---

### Mean Reversion

Buys oversold assets and exits when price reverts toward the mean. Three indicator options; choose one per config file.

#### Common Parameters

| Parameter | Type | Valid values | Description |
|---|---|---|---|
| `indicator` | string | `rsi`, `bollinger-bands`, `zscore` | Signal indicator to use |
| `max-holding-days` | integer or `false` | Positive integer or `false` | Loss-only time stop: exit after N days **only if the position is at a loss**. Profitable positions run until the exit signal fires. |

#### RSI Parameters

| Parameter | Type | Valid values | Conventional |
|---|---|---|---|
| `period` | integer | Positive integer | 14 |
| `entry-threshold` | float | 1–49 | 30 (oversold) |
| `exit-threshold` | float | > entry-threshold, ≤ 99 | 50 (normalized) |

```toml
[strategy.parameters]
indicator = "rsi"
max-holding-days = 10

[strategy.parameters.rsi]
period = 14
entry-threshold = 30
exit-threshold = 50
```

#### Bollinger Bands Parameters

| Parameter | Type | Valid values | Conventional |
|---|---|---|---|
| `period` | integer | Positive integer | 20 |
| `std-dev` | float | Positive float | 2.0 |
| `exit-band` | string | `middle`, `upper` | `middle` |

`exit-band = "middle"` exits at the rolling mean. `exit-band = "upper"` exits at the upper band, letting profits run further.

```toml
[strategy.parameters]
indicator = "bollinger-bands"
max-holding-days = 10

[strategy.parameters.bollinger-bands]
period = 20
std-dev = 2.0
exit-band = "middle"
```

#### Z-Score Parameters

| Parameter | Type | Valid values | Conventional |
|---|---|---|---|
| `period` | integer | Positive integer | 20 |
| `entry-threshold` | float | Any negative float | -2.0 |
| `exit-threshold` | float | Any float > entry-threshold (including positive) | 0.0 |

```toml
[strategy.parameters]
indicator = "zscore"
max-holding-days = 10

[strategy.parameters.zscore]
period = 20
entry-threshold = -2.0
exit-threshold = 0.0
```

---

### Pairs Trading

Simultaneously longs the underperformer and shorts the outperformer of a correlated asset pair when their price spread diverges. Exits when the spread reverts.

Pairs must be pre-defined in the config. Research cointegration before adding a pair — use statistical tests (Engle-Granger, Johansen) to verify. The hedge ratio is calculated automatically using OLS regression.

Both `stop-loss-zscore` and the per-position `stop-loss` in `[risk]` apply independently — whichever triggers first closes the position.

#### Parameters

| Parameter | Type | Valid values | Conventional |
|---|---|---|---|
| `pairs` | array of 2-ticker arrays | Min 1 pair | — |
| `lookback-period` | integer (days) | Positive integer | 60 |
| `entry-zscore` | float | Positive float | 2.0 |
| `exit-zscore` | float | Positive float < entry-zscore | 0.5 |
| `stop-loss-zscore` | float | Positive float > entry-zscore | 3.0 |
| `recalculate-frequency` | string | `weekly`, `monthly` | — |

#### Example Config

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
pairs = [
    ["KO",  "PEP"],
    ["XOM", "CVX"],
    ["JPM", "BAC"],
]
```

---

## Backtesting

Backtesting uses Alpaca's historical data API and generates three outputs:

| Output | Location |
|---|---|
| Performance summary | Console (Sharpe ratio, CAGR, max drawdown, win rate, total trades) |
| Equity curve + drawdown chart | `./backtest_results/` (HTML, opens in browser automatically) |
| Trade log and daily returns | `./backtest_results/` (CSV) |

```bash
python pacabot.py \
    --config configs/example-momentum.toml \
    --backtest \
    --start 2021-01-01 \
    --end 2024-12-31
```

---

## Output Directories

| Directory | Contents |
|---|---|
| `backtest_results/` | HTML charts and CSV files from backtests |
| `reports/` | Daily end-of-day P&L reports (`report_YYYY-MM-DD.txt`) |
| `.cache/` | Cached universe constituent lists (refreshed every 24 hours) |
| `.state/` | Strategy state files (holding period tracking, open pairs) |

These directories are created automatically and are excluded from git (see `.gitignore`).

---

## Risk Management

pacabot uses a two-layer approach to protect margin accounts:

**Layer 1 — Preventive:** `margin-utilization-cap` constrains position sizing at order time so the bot cannot over-leverage under normal conditions.

**Layer 2 — Reactive:** Continuous monitoring with two configurable thresholds:
- At `margin-warning-threshold`: stop opening new positions
- At `margin-critical-threshold`: flatten all positions, halt the bot

After a critical halt or daily loss limit breach, **manual restart is required**. Inspect the account, resolve the issue, then restart.
