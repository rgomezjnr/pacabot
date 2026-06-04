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

    def _target_longs(self, ranked: list[str]) -> set[str]:
        return set(ranked[: self._params.top_n])

    def _target_shorts(self, ranked: list[str]) -> set[str]:
        if self._cfg.strategy.long_only:
            return set()
        return set(ranked[-self._params.top_n :])

    # ------------------------------------------------------------------
    # Rebalance
    # ------------------------------------------------------------------

    def _rebalance(self) -> None:
        self._logger.info("Running momentum rebalance...")
        ranked = self._rank_universe()
        if ranked is None:
            return

        target_longs = self._target_longs(ranked)
        target_shorts = self._target_shorts(ranked)
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
