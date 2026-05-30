"""Backtesting via vectorbt using Alpaca historical data."""

import subprocess
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import vectorbt as vbt
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from pacabot.config import (
    BollingerBandsParameters,
    Config,
    MeanReversionParameters,
    MomentumParameters,
    PairsParameters,
    RSIParameters,
    ZScoreParameters,
)
from pacabot.logging_setup import get_logger
from pacabot.universe import get_universe

_RESULTS_DIR = Path("backtest_results")


def _fetch_close(
    client: StockHistoricalDataClient,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
    )
    bars = client.get_stock_bars(req)
    df = bars.df
    if df.empty:
        return pd.DataFrame()
    close = df["close"].unstack(level=0)
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.sort_index()


# ---------------------------------------------------------------------------
# Strategy-specific signal builders
# ---------------------------------------------------------------------------

def _momentum_signals(close: pd.DataFrame, params: MomentumParameters, long_only: bool):
    returns = close.pct_change(params.lookback_period)
    ranked = returns.rank(axis=1, ascending=False)
    in_top = ranked <= params.top_n

    entries = in_top & ~in_top.shift(1, fill_value=False)
    exits = ~in_top & in_top.shift(1, fill_value=False)

    short_entries = short_exits = None
    if not long_only:
        in_bottom = ranked > (ranked.shape[1] - params.top_n)
        short_entries = in_bottom & ~in_bottom.shift(1, fill_value=False)
        short_exits = ~in_bottom & in_bottom.shift(1, fill_value=False)

    return entries, exits, short_entries, short_exits


def _calc_rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _mean_reversion_signals(close: pd.DataFrame, params: MeanReversionParameters):
    p = params
    entries = pd.DataFrame(False, index=close.index, columns=close.columns)
    exits = pd.DataFrame(False, index=close.index, columns=close.columns)

    for col in close.columns:
        s = close[col]

        if p.rsi and p.indicator == "rsi":
            rp: RSIParameters = p.rsi
            rsi = _calc_rsi(s, rp.period)
            entries[col] = rsi < rp.entry_threshold
            exits[col] = rsi > rp.exit_threshold

        elif p.bollinger_bands and p.indicator == "bollinger-bands":
            bp: BollingerBandsParameters = p.bollinger_bands
            mean = s.rolling(bp.period).mean()
            std = s.rolling(bp.period).std()
            lower = mean - bp.std_dev * std
            upper_band = mean + bp.std_dev * std
            entries[col] = s < lower
            exits[col] = s > (upper_band if bp.exit_band == "upper" else mean)

        elif p.zscore and p.indicator == "zscore":
            zp: ZScoreParameters = p.zscore
            mean = s.rolling(zp.period).mean()
            std = s.rolling(zp.period).std()
            z = (s - mean) / std.replace(0, np.nan)
            entries[col] = z < zp.entry_threshold
            exits[col] = z > zp.exit_threshold

    # Convert level signals to entry/exit edges
    entry_edges = entries & ~entries.shift(1, fill_value=False)
    exit_edges = exits & ~exits.shift(1, fill_value=False)
    return entry_edges, exit_edges


def _pairs_signals(close: pd.DataFrame, params: PairsParameters):
    """Build vectorbt-compatible signal DataFrames for each pair leg."""
    all_entries_long: list[pd.Series] = []
    all_exits_long: list[pd.Series] = []
    all_entries_short: list[pd.Series] = []
    all_exits_short: list[pd.Series] = []
    pair_labels: list[str] = []

    for pair in params.pairs:
        a, b = pair[0], pair[1]
        if a not in close.columns or b not in close.columns:
            continue

        # OLS hedge ratio (rolling)
        price_a = close[a]
        price_b = close[b]
        spread = price_a - price_b  # simplified; full OLS would roll

        lp = params.lookback_period
        mean = spread.rolling(lp).mean()
        std = spread.rolling(lp).std()
        z = (spread - mean) / std.replace(0, np.nan)

        # A cheap vs B: long A, short B
        entry_long_a = (z < -params.entry_zscore) & ~(z.shift(1) < -params.entry_zscore)
        entry_short_b = entry_long_a.copy()
        exit_long_a = (z.abs() <= params.exit_zscore) | (z >= params.stop_loss_zscore)
        exit_short_b = exit_long_a.copy()

        all_entries_long.append(entry_long_a.rename(f"{a}/{b}_long_{a}"))
        all_exits_long.append(exit_long_a.rename(f"{a}/{b}_exit_{a}"))
        all_entries_short.append(entry_short_b.rename(f"{a}/{b}_short_{b}"))
        all_exits_short.append(exit_short_b.rename(f"{a}/{b}_exit_{b}"))
        pair_labels.append(f"{a}/{b}")

    if not all_entries_long:
        return None

    return {
        "entries_long": pd.concat(all_entries_long, axis=1),
        "exits_long": pd.concat(all_exits_long, axis=1),
        "entries_short": pd.concat(all_entries_short, axis=1),
        "exits_short": pd.concat(all_exits_short, axis=1),
        "pair_labels": pair_labels,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_summary(portfolio, strategy_name: str, start: str, end: str) -> None:
    stats = portfolio.stats(group_by=True)
    print()
    print("=" * 60)
    print(f"  pacabot Backtest — {strategy_name}")
    print(f"  Period: {start} to {end}")
    print("=" * 60)
    print(f"  Sharpe Ratio   : {stats.get('Sharpe Ratio', float('nan')):.3f}")
    print(f"  CAGR           : {stats.get('Annualized Return [%]', float('nan')):.2f}%")
    print(f"  Max Drawdown   : {stats.get('Max Drawdown [%]', float('nan')):.2f}%")
    print(f"  Win Rate       : {stats.get('Win Rate [%]', float('nan')):.2f}%")
    print(f"  Total Trades   : {int(stats.get('Total Trades', 0))}")
    print(f"  Total Return   : {stats.get('Total Return [%]', float('nan')):.2f}%")
    print("=" * 60)
    print()


def _save_outputs(portfolio, strategy_name: str, start_str: str, end_str: str) -> Path:
    _RESULTS_DIR.mkdir(exist_ok=True)
    slug = strategy_name.replace(" ", "_").replace("/", "-")
    base_name = f"{slug}_{start_str}_{end_str}"

    # HTML chart
    html_path = _RESULTS_DIR / f"{base_name}.html"
    try:
        fig = portfolio.plot(group_by=True)
        fig.write_html(str(html_path))
    except Exception as e:
        get_logger().warning("Could not generate HTML chart: %s", e)

    # CSV trade log
    csv_path = _RESULTS_DIR / f"{base_name}_trades.csv"
    try:
        portfolio.trades.records_readable.to_csv(csv_path, index=False)
    except Exception as e:
        get_logger().warning("Could not save trade log CSV: %s", e)

    # CSV returns
    returns_csv = _RESULTS_DIR / f"{base_name}_returns.csv"
    try:
        portfolio.returns().to_csv(returns_csv)
    except Exception as e:
        get_logger().warning("Could not save returns CSV: %s", e)

    return html_path


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_backtest(cfg: Config, start_str: str, end_str: str) -> None:
    logger = get_logger()
    import os

    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        print("[pacabot] Error: ALPACA_API_KEY and ALPACA_SECRET_KEY must be set.", file=sys.stderr)
        sys.exit(1)

    start_dt = datetime.fromisoformat(start_str).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(end_str).replace(tzinfo=timezone.utc)

    data_client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)

    strategy_name = cfg.strategy.name
    logger.info("Starting backtest: %s from %s to %s", strategy_name, start_str, end_str)

    if strategy_name == "cross-sectional-momentum":
        params: MomentumParameters = cfg.strategy.parameters  # type: ignore[assignment]
        universe = get_universe(cfg.strategy.universe, cfg.strategy.custom_tickers or None)
        warmup_start = start_dt - timedelta(days=int(params.lookback_period * 1.5))
        close_raw = _fetch_close(data_client, universe, warmup_start, end_dt)
        if close_raw.empty:
            logger.error("No price data returned for backtest")
            sys.exit(1)

        entries_raw, exits_raw, _, _ = _momentum_signals(
            close_raw, params, cfg.strategy.long_only or True
        )
        start_date = pd.Timestamp(start_str)
        close = close_raw.loc[close_raw.index >= start_date]
        entries = entries_raw.loc[entries_raw.index >= start_date]
        exits = exits_raw.loc[exits_raw.index >= start_date]

        portfolio = vbt.Portfolio.from_signals(
            close, entries=entries, exits=exits, freq="D", init_cash=100_000, group_by=True
        )

    elif strategy_name == "mean-reversion":
        params_mr: MeanReversionParameters = cfg.strategy.parameters  # type: ignore[assignment]
        universe = get_universe(cfg.strategy.universe, cfg.strategy.custom_tickers or None)
        if params_mr.indicator == "rsi" and params_mr.rsi:
            mr_lookback = params_mr.rsi.period
        elif params_mr.indicator == "bollinger-bands" and params_mr.bollinger_bands:
            mr_lookback = params_mr.bollinger_bands.period
        elif params_mr.indicator == "zscore" and params_mr.zscore:
            mr_lookback = params_mr.zscore.period
        else:
            mr_lookback = 100
        warmup_start = start_dt - timedelta(days=int(mr_lookback * 1.5))
        close_raw = _fetch_close(data_client, universe, warmup_start, end_dt)
        if close_raw.empty:
            logger.error("No price data returned for backtest")
            sys.exit(1)

        entries_raw, exits_raw = _mean_reversion_signals(close_raw, params_mr)
        start_date = pd.Timestamp(start_str)
        close = close_raw.loc[close_raw.index >= start_date]
        entries = entries_raw.loc[entries_raw.index >= start_date]
        exits = exits_raw.loc[exits_raw.index >= start_date]

        portfolio = vbt.Portfolio.from_signals(
            close, entries=entries, exits=exits, freq="D", init_cash=100_000, group_by=True
        )

    elif strategy_name == "pairs-trading":
        params_p: PairsParameters = cfg.strategy.parameters  # type: ignore[assignment]
        all_tickers = list({t for pair in params_p.pairs for t in pair})
        warmup_start = start_dt - timedelta(days=int(params_p.lookback_period * 1.5))
        close_raw = _fetch_close(data_client, all_tickers, warmup_start, end_dt)
        if close_raw.empty:
            logger.error("No price data returned for backtest")
            sys.exit(1)

        signals = _pairs_signals(close_raw, params_p)
        if signals is None:
            logger.error("No valid pairs found in data")
            sys.exit(1)

        start_date = pd.Timestamp(start_str)
        long_cols = [f"{p[0]}/{p[1]}_long_{p[0]}" for p in params_p.pairs if p[0] in close_raw.columns]
        close_long_raw = close_raw[[p[0] for p in params_p.pairs if p[0] in close_raw.columns]]
        close_long_raw.columns = long_cols[: len(close_long_raw.columns)]
        close_long = close_long_raw.loc[close_long_raw.index >= start_date]
        entries_long = signals["entries_long"].loc[signals["entries_long"].index >= start_date]
        exits_long = signals["exits_long"].loc[signals["exits_long"].index >= start_date]

        portfolio = vbt.Portfolio.from_signals(
            close_long,
            entries=entries_long,
            exits=exits_long,
            freq="D",
            init_cash=100_000,
            group_by=True,
        )

    else:
        logger.error("Unknown strategy: %s", strategy_name)
        sys.exit(1)

    _print_summary(portfolio, strategy_name, start_str, end_str)
    html_path = _save_outputs(portfolio, strategy_name, start_str, end_str)

    logger.info("Backtest results saved to %s", _RESULTS_DIR)

    if html_path.exists():
        logger.info("Opening results in browser: %s", html_path)
        webbrowser.open(html_path.resolve().as_uri())
