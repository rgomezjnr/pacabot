"""Pairs trading strategy — OLS hedge ratio, z-score entry/exit."""

from datetime import date

import numpy as np
import pandas as pd

from pacabot.account import AlpacaClient
from pacabot.config import Config, PairsParameters
from pacabot.execution import ExecutionManager
from pacabot.risk import RiskManager
from pacabot.strategies.base import BaseStrategy


def _ols_hedge_ratio(price_a: pd.Series, price_b: pd.Series) -> float:
    """OLS regression of price_a on price_b; returns beta (hedge ratio)."""
    x = price_b.values
    y = price_a.values
    valid = ~(np.isnan(x) | np.isnan(y))
    x, y = x[valid], y[valid]
    if len(x) < 2:
        return 1.0
    beta = np.cov(y, x)[0, 1] / np.var(x)
    return float(beta)


def _spread_zscore(
    price_a: pd.Series, price_b: pd.Series, hedge_ratio: float, period: int
) -> float:
    """Compute z-score of the current spread vs. its rolling mean/std."""
    spread = price_a - hedge_ratio * price_b
    mean = spread.rolling(period).mean()
    std = spread.rolling(period).std()
    zscore = (spread - mean) / std.replace(0, np.nan)
    return float(zscore.iloc[-1]) if not zscore.empty else 0.0


class PairsStrategy(BaseStrategy):
    def __init__(
        self,
        cfg: Config,
        client: AlpacaClient,
        risk: RiskManager,
        execution: ExecutionManager,
    ) -> None:
        super().__init__(cfg, client, risk, execution)
        self._params: PairsParameters = cfg.strategy.parameters  # type: ignore[assignment]

    def on_startup(self) -> None:
        pair_strs = [f"{a}/{b}" for a, b in self._params.pairs]
        self._logger.info(
            "Pairs trading strategy initialised — %d pair(s): %s, "
            "lookback: %dd, entry z: %.1f, exit z: %.1f, stop z: %.1f",
            len(self._params.pairs),
            ", ".join(pair_strs),
            self._params.lookback_period,
            self._params.entry_zscore,
            self._params.exit_zscore,
            self._params.stop_loss_zscore,
        )
        self._reconcile_state()

    # ------------------------------------------------------------------
    # State helpers for open pair trades
    # ------------------------------------------------------------------

    def _open_pairs(self) -> dict[str, dict]:
        """Return dict keyed by 'A/B' with trade details."""
        return self._state.get("open_pairs", {})

    def _record_pair_open(
        self, a: str, b: str, long_sym: str, short_sym: str, hedge_ratio: float
    ) -> None:
        key = f"{a}/{b}"
        self._state.setdefault("open_pairs", {})[key] = {
            "long": long_sym,
            "short": short_sym,
            "hedge_ratio": hedge_ratio,
            "date": date.today().isoformat(),
        }
        self._save_state()

    def _clear_pair(self, a: str, b: str) -> None:
        self._state.get("open_pairs", {}).pop(f"{a}/{b}", None)
        self._save_state()

    def _reconcile_state(self) -> None:
        """Drop open_pairs entries whose Alpaca positions no longer exist."""
        open_pairs = self._open_pairs()
        if not open_pairs:
            return
        configured_keys = {f"{a}/{b}" for a, b in self._params.pairs}
        positions = {p.symbol for p in self._client.get_positions()}
        for key, trade in list(open_pairs.items()):
            a, b = key.split("/")
            long_sym = trade["long"]
            short_sym = trade["short"]

            if key not in configured_keys:
                self._logger.warning(
                    "Pair %s — removed from config; closing open positions", key
                )
                self._execution.close_pair(long_sym, short_sym, reason="removed from config")
                self._clear_pair(a, b)
                continue

            if long_sym not in positions and short_sym not in positions:
                self._clear_pair(a, b)
                self._logger.info(
                    "Pair %s — positions closed externally; cleared from state", key
                )
            elif long_sym not in positions or short_sym not in positions:
                missing = long_sym if long_sym not in positions else short_sym
                self._clear_pair(a, b)
                self._logger.warning(
                    "Pair %s — orphaned leg detected (missing %s); cleared from state",
                    key, missing,
                )

    # ------------------------------------------------------------------
    # Recalculation schedule
    # ------------------------------------------------------------------

    def should_rebalance(self) -> bool:
        """Use recalculate_frequency for pairs."""
        last = self._last_rebalance()
        today = date.today()
        if last is None:
            return True
        freq = self._params.recalculate_frequency
        if freq == "daily":
            return today != last
        if freq == "weekly":
            return (today - last).days >= 7
        if freq == "monthly":
            return today.month != last.month or today.year != last.year
        return False

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def tick(self) -> None:
        if self._risk.is_halted:
            return
        if not self._risk.check_daily_loss():
            return

        margin_ok = self._risk.check_margin()
        if self._risk.is_halted:
            # Critical threshold just triggered; positions already closed by check_margin
            return

        all_tickers = list({t for pair in self._params.pairs for t in pair})
        days_needed = self._params.lookback_period
        try:
            close = self._client.get_close_prices(all_tickers, days_needed)
        except Exception as e:
            self._logger.error("Failed to fetch price data: %s", e)
            return

        if close.empty or len(close) < self._params.lookback_period:
            self._logger.warning("Insufficient data for pairs trading")
            return

        open_pairs = self._open_pairs()
        positions = {p.symbol for p in self._client.get_positions()}
        recalculate = self.should_rebalance()
        self._execution.reset_pending_entries()

        for pair in self._params.pairs:
            a, b = pair[0], pair[1]
            key = f"{a}/{b}"

            if a not in close.columns or b not in close.columns:
                self._logger.warning("Missing price data for pair %s/%s — skipping", a, b)
                continue

            common = close[[a, b]].dropna()
            price_a = common[a]
            price_b = common[b]

            hedge_ratio = _ols_hedge_ratio(price_a, price_b)
            zscore = _spread_zscore(price_a, price_b, hedge_ratio, self._params.lookback_period)

            self._logger.debug("Pair %s/%s — hedge ratio: %.4f, z-score: %.4f", a, b, hedge_ratio, zscore)

            if key in open_pairs:
                trade = open_pairs[key]
                long_sym = trade["long"]
                short_sym = trade["short"]

                # If both legs were closed externally (e.g. stop triggered), clear state
                if long_sym not in positions and short_sym not in positions:
                    self._clear_pair(a, b)
                    self._logger.info(
                        "Pair %s — positions closed externally; cleared from state", key
                    )
                    continue

                # Stop loss z-score (position-level stop loss handled by GTC orders)
                if abs(zscore) >= self._params.stop_loss_zscore:
                    self._logger.warning(
                        "Pair %s — stop-loss z-score breached (z=%.2f). Closing.", key, zscore
                    )
                    self._execution.close_pair(long_sym, short_sym, reason="z-score stop loss")
                    self._clear_pair(a, b)
                    continue

                # Exit when spread reverts
                if abs(zscore) <= self._params.exit_zscore:
                    self._logger.info(
                        "Pair %s — spread reverted (z=%.2f). Closing.", key, zscore
                    )
                    self._execution.close_pair(long_sym, short_sym, reason="spread reversion")
                    self._clear_pair(a, b)
                    continue

                # Recalculate hedge ratio on schedule
                if recalculate:
                    trade["hedge_ratio"] = hedge_ratio
                    self._save_state()
                    self._logger.debug("Updated hedge ratio for %s: %.4f", key, hedge_ratio)

            else:
                # Entry: spread is far enough from mean
                if not margin_ok:
                    continue
                if abs(zscore) >= self._params.entry_zscore:
                    # Don't enter if z-score is already at or above stop-loss threshold —
                    # the position would be closed on the very next tick, creating a churn loop.
                    if abs(zscore) >= self._params.stop_loss_zscore:
                        self._logger.debug(
                            "Pair %s/%s — z-score above stop threshold (z=%.2f), skipping entry",
                            a, b, zscore,
                        )
                        continue

                    # Positive z: A is expensive vs B → short A, long B
                    # Negative z: A is cheap vs B → long A, short B
                    if zscore > 0:
                        long_sym, short_sym = b, a
                    else:
                        long_sym, short_sym = a, b

                    # Only enter if neither leg is already in a position
                    if long_sym in positions or short_sym in positions:
                        self._logger.debug(
                            "Pair %s — signal but one leg already in position, skipping", key
                        )
                        continue

                    self._logger.info(
                        "Pair %s — entry signal (z=%.2f). Long %s, short %s",
                        key, zscore, long_sym, short_sym,
                    )
                    if self._execution.open_pair(long_sym, short_sym, hedge_ratio):
                        self._record_pair_open(a, b, long_sym, short_sym, hedge_ratio)

        if recalculate:
            self._mark_rebalanced()
