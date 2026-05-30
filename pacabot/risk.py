"""Risk management: margin monitoring, position sizing, circuit breakers."""

import sys
from dataclasses import dataclass, field
from datetime import date

from pacabot.account import AlpacaClient
from pacabot.config import RiskConfig
from pacabot.logging_setup import get_logger


@dataclass
class DailyPnL:
    date: date
    starting_equity: float
    current_equity: float = 0.0

    @property
    def pnl_pct(self) -> float:
        if self.starting_equity == 0:
            return 0.0
        return (self.current_equity - self.starting_equity) / self.starting_equity


class RiskManager:
    def __init__(self, cfg: RiskConfig, client: AlpacaClient) -> None:
        self._cfg = cfg
        self._client = client
        self._logger = get_logger()
        self._daily: DailyPnL | None = None
        self._halted = False

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_halted(self) -> bool:
        return self._halted

    def initialise_day(self) -> None:
        account = self._client.get_account()
        equity = float(account.equity)
        today = date.today()
        if self._daily is None or self._daily.date != today:
            self._daily = DailyPnL(date=today, starting_equity=equity, current_equity=equity)
            self._logger.info("Day initialised — starting equity: $%.2f", equity)

    def update_equity(self) -> None:
        account = self._client.get_account()
        if self._daily:
            self._daily.current_equity = float(account.equity)

    # ------------------------------------------------------------------
    # Margin checks
    # ------------------------------------------------------------------

    def margin_utilization(self) -> float:
        account = self._client.get_account()
        equity = float(account.equity)
        if equity == 0:
            return 1.0
        initial_margin = float(account.initial_margin)
        return initial_margin / equity

    def check_margin(self) -> bool:
        """
        Check margin utilization. Returns True if safe to continue.
        Logs warning and halts on threshold breach.
        """
        util = self.margin_utilization()
        cfg = self._cfg

        if util >= cfg.margin_critical_threshold:
            self._logger.critical(
                "Margin critical threshold breached (%.1f%% >= %.1f%%). "
                "Flattening all positions and halting. Manual restart required.",
                util * 100,
                cfg.margin_critical_threshold * 100,
            )
            self._client.close_all_positions()
            self._halted = True
            return False

        if util >= cfg.margin_warning_threshold:
            self._logger.warning(
                "Margin warning threshold reached (%.1f%% >= %.1f%%). "
                "No new positions will be opened.",
                util * 100,
                cfg.margin_warning_threshold * 100,
            )
            return False  # block new positions but don't halt

        return True

    def can_open_position(self) -> bool:
        """Return True if a new position may be opened."""
        if self._halted:
            return False
        util = self.margin_utilization()
        if util >= self._cfg.margin_warning_threshold:
            self._logger.warning(
                "Skipping new position — margin at %.1f%%", util * 100
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Daily circuit breaker
    # ------------------------------------------------------------------

    def check_daily_loss(self) -> bool:
        """Returns True if within daily loss limit. Halts if breached."""
        if self._cfg.daily_loss_limit is None or self._daily is None:
            return True

        self.update_equity()
        pnl = self._daily.pnl_pct

        if pnl <= -self._cfg.daily_loss_limit:
            self._logger.critical(
                "Daily loss limit breached (%.2f%% loss >= %.2f%% limit). "
                "Halting all trading. Manual restart required.",
                abs(pnl) * 100,
                self._cfg.daily_loss_limit * 100,
            )
            self._halted = True
            return False

        return True

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def max_position_value(self) -> float:
        """Return the maximum dollar value for a single new position."""
        account = self._client.get_account()
        equity = float(account.equity)
        by_pct = equity * self._cfg.max_position_size
        by_usd = self._cfg.max_position_size_usd
        return min(by_pct, by_usd)

    def position_count(self) -> int:
        return len(self._client.get_positions())

    def can_add_position(self) -> bool:
        """Check max concurrent positions limit."""
        if self._cfg.max_positions == 0:
            return True
        return self.position_count() < self._cfg.max_positions

    def calculate_shares(self, price: float) -> int:
        """Return integer share count for a new position at given price."""
        if price <= 0:
            return 0
        max_val = self.max_position_value()
        # Also constrain to margin utilization cap
        account = self._client.get_account()
        buying_power = float(account.buying_power)
        max_from_margin = buying_power * self._cfg.margin_utilization_cap
        max_val = min(max_val, max_from_margin)
        shares = int(max_val / price)
        return max(shares, 0)

    # ------------------------------------------------------------------
    # Stop loss price
    # ------------------------------------------------------------------

    def stop_loss_price(self, entry_price: float, side: str) -> float | None:
        """Return stop loss trigger price for an entry, or None if disabled."""
        if self._cfg.stop_loss is None:
            return None
        if side == "long":
            return round(entry_price * (1 - self._cfg.stop_loss), 2)
        else:  # short
            return round(entry_price * (1 + self._cfg.stop_loss), 2)
