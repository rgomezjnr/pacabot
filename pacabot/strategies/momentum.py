"""Cross-sectional momentum strategy."""

import pandas as pd

from pacabot.account import AlpacaClient
from pacabot.config import Config, MomentumParameters
from pacabot.execution import ExecutionManager
from pacabot.risk import RiskManager
from pacabot.strategies.base import BaseStrategy
from pacabot.universe import get_universe


class MomentumStrategy(BaseStrategy):
    def __init__(
        self,
        cfg: Config,
        client: AlpacaClient,
        risk: RiskManager,
        execution: ExecutionManager,
    ) -> None:
        super().__init__(cfg, client, risk, execution)
        self._params: MomentumParameters = cfg.strategy.parameters  # type: ignore[assignment]
        self._universe: list[str] = []

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def on_startup(self) -> None:
        self._universe = get_universe(
            self._cfg.strategy.universe,
            self._cfg.strategy.custom_tickers or None,
        )
        self._logger.info(
            "Momentum strategy initialised — universe: %s (%d tickers), "
            "lookback: %dd, top-N: %d, rebalance: %s",
            self._cfg.strategy.universe,
            len(self._universe),
            self._params.lookback_period,
            self._params.top_n,
            self._params.rebalance_frequency,
        )
        if self.should_rebalance():
            self._logger.info("Rebalance due — will run at next tick")
        else:
            self._logger.info(
                "Already rebalanced today (%s) — monitoring positions",
                self._last_rebalance(),
            )

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def _rank_universe(self) -> list[str] | None:
        """
        Return tickers ranked by trailing return (best first).
        Returns None if data is insufficient.
        """
        days_needed = self._params.lookback_period
        try:
            close = self._client.get_close_prices(self._universe, days_needed)
        except Exception as e:
            self._logger.error("Failed to fetch price data: %s", e)
            return None

        if close.empty or len(close) < self._params.lookback_period:
            self._logger.warning("Insufficient price history for momentum ranking")
            return None

        returns = close.pct_change(self._params.lookback_period).iloc[-1]
        returns = returns.dropna()
        ranked = returns.sort_values(ascending=False)
        return ranked.index.tolist()

    def _select_sized_targets(
        self, candidates: list[str], n: int, exclude: set[str] | None = None
    ) -> set[str]:
        """Return the first n symbols from candidates that can be sized to at least 1 share.

        Fetches quotes for a pool of 2*n candidates and skips any whose price
        exceeds the per-position dollar cap, falling through to the next-best
        ranked symbol. This prevents the portfolio from being short by one
        position when a high-priced stock tops the rankings.
        """
        pool = [s for s in candidates if s not in (exclude or set())][:n * 2]
        try:
            quotes = self._client.get_latest_quotes(pool)
        except Exception as e:
            self._logger.warning(
                "Pre-fetch for target selection failed: %s — using strict top-%d", e, n
            )
            return set(pool[:n])

        max_val = self._risk.max_position_value()
        targets: set[str] = set()
        for symbol in pool:
            if len(targets) >= n:
                break
            price = quotes.get(symbol, 0)
            if price > 0 and price > max_val:
                self._logger.warning(
                    "Excluding %s from target — price $%.2f exceeds max position value $%.2f; "
                    "trying next ranked candidate",
                    symbol, price, max_val,
                )
                continue
            targets.add(symbol)
        return targets

    # ------------------------------------------------------------------
    # Rebalance
    # ------------------------------------------------------------------

    def _rebalance(self) -> None:
        self._logger.info("Running momentum rebalance...")
        ranked = self._rank_universe()
        if ranked is None:
            return

        target_longs = self._select_sized_targets(ranked, self._params.top_n)
        target_shorts = (
            set()
            if self._cfg.strategy.long_only
            else self._select_sized_targets(
                list(reversed(ranked)), self._params.top_n, exclude=target_longs
            )
        )
        target_all = target_longs | target_shorts

        positions = {p.symbol: p for p in self._client.get_positions()}
        current_symbols = set(positions.keys())

        # Close positions no longer in target
        to_close = current_symbols - target_all
        for symbol in to_close:
            pos = positions[symbol]
            is_long = float(pos.qty) > 0
            expected_long = symbol in target_longs
            expected_short = symbol in target_shorts
            if (is_long and not expected_long) or (not is_long and not expected_short):
                self._execution.close_position(symbol, reason="momentum rebalance")

        # Open new positions
        self._execution.reset_pending_entries()
        for symbol in target_longs - current_symbols:
            self._execution.open_position(symbol, long=True)

        for symbol in target_shorts - current_symbols:
            self._execution.open_position(symbol, long=False)

        self._mark_rebalanced()
        self._logger.info(
            "Rebalance complete — target longs: %d, target shorts: %d",
            len(target_longs),
            len(target_shorts),
        )

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def tick(self) -> None:
        if self._risk.is_halted:
            return
        if not self._risk.check_margin():
            return
        if not self._risk.check_daily_loss():
            return
        if self.should_rebalance():
            self._rebalance()
        else:
            self._logger.debug(
                "Tick — no rebalance due (last: %s)", self._last_rebalance()
            )
