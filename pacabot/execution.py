"""Order execution: entry, exit, stop loss management, GTC reconciliation."""

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Order, Position

from pacabot.account import AlpacaClient
from pacabot.config import StrategyConfig
from pacabot.logging_setup import get_logger
from pacabot.risk import RiskManager


class ExecutionManager:
    def __init__(
        self,
        client: AlpacaClient,
        risk: RiskManager,
        strategy_cfg: StrategyConfig,
    ) -> None:
        self._client = client
        self._risk = risk
        self._strategy = strategy_cfg
        self._logger = get_logger()
        self._pending_entries: int = 0

    def reset_pending_entries(self) -> None:
        """Reset the pending-entries counter at the start of each tick's entry phase."""
        self._pending_entries = 0

    # ------------------------------------------------------------------
    # GTC order reconciliation
    # ------------------------------------------------------------------

    def reconcile_stop_orders(self) -> None:
        """
        On startup: ensure every open position has a GTC stop order.
        Add missing stops; cancel duplicate stops.
        """
        if self._risk._cfg.stop_loss is None:
            return

        positions = {p.symbol: p for p in self._client.get_positions()}
        open_orders = self._client.get_open_orders()

        # Map symbol -> list of stop orders
        stops_by_symbol: dict[str, list[Order]] = {}
        for order in open_orders:
            if order.order_type.value == "stop" and order.time_in_force.value == "gtc":
                stops_by_symbol.setdefault(order.symbol, []).append(order)

        for symbol, position in positions.items():
            qty = abs(float(position.qty))
            avg_price = float(position.avg_entry_price)
            side = "long" if float(position.qty) > 0 else "short"

            existing = stops_by_symbol.get(symbol, [])
            if len(existing) > 1:
                for extra in existing[1:]:
                    self._client.cancel_order(str(extra.id))
                    self._logger.warning("Cancelled duplicate stop order for %s", symbol)
                existing = existing[:1]

            if not existing:
                self._logger.info("Reinstating missing stop order for %s", symbol)
                self._place_protective_stop(symbol, qty, side, avg_price)

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def _order_side_entry(self, long: bool) -> OrderSide:
        return OrderSide.BUY if long else OrderSide.SELL

    def _place_protective_stop(
        self, symbol: str, qty: float, side: str, fill_price: float
    ) -> None:
        """Submit a GTC protective stop for a freshly opened position.

        The stop is computed from the actual fill price (not the pre-trade
        quote) and validated against the live market price before submission.
        Alpaca rejects a sell stop that is not strictly below the current price
        (and a buy stop not strictly above it); for fast-moving entries the
        price can pass the intended stop level before the stop is placed. When
        that happens the stop has effectively already been hit, so we exit at
        market rather than leaving the position unprotected.
        """
        stop_price = self._risk.stop_loss_price(fill_price, side)
        if stop_price is None:
            return

        try:
            live = self._client.get_latest_quotes([symbol]).get(symbol)
        except Exception as e:
            self._logger.warning(
                "Could not fetch live quote to validate stop for %s: %s", symbol, e
            )
            live = None

        if live is not None and live > 0:
            breached = (side == "long" and stop_price >= live) or (
                side == "short" and stop_price <= live
            )
            if breached:
                self._logger.warning(
                    "Stop level $%.2f for %s already breached (market $%.2f) — "
                    "exiting at market instead of leaving position unprotected",
                    stop_price,
                    symbol,
                    live,
                )
                self.close_position(symbol, reason="stop level breached at entry")
                return

        stop_side = OrderSide.SELL if side == "long" else OrderSide.BUY
        try:
            self._client.submit_stop_order(symbol, qty, stop_side, stop_price)
        except Exception as e:
            self._logger.error("Stop order failed for %s: %s", symbol, e)

    def open_position(self, symbol: str, long: bool = True) -> bool:
        """
        Open a new position. Returns True if order was submitted.
        """
        if not self._risk.can_open_position():
            return False
        if not self._risk.can_add_position(self._pending_entries):
            self._logger.warning(
                "Skipping %s — max concurrent positions (%d) reached",
                symbol,
                self._risk._cfg.max_positions,
            )
            return False

        # Get current price
        try:
            quotes = self._client.get_latest_quotes([symbol])
            price = quotes.get(symbol)
            if not price or price <= 0:
                self._logger.error("Could not get quote for %s", symbol)
                return False
        except Exception as e:
            self._logger.error("Quote fetch failed for %s: %s", symbol, e)
            return False

        shares = self._risk.calculate_shares(price)
        if shares <= 0:
            self._logger.warning(
                "Skipping %s — calculated position size is 0 (price $%.2f)", symbol, price
            )
            return False

        side = self._order_side_entry(long)
        order_type = self._strategy.order_type

        try:
            if order_type == "market":
                entry_order = self._client.submit_market_order(symbol, shares, side, TimeInForce.DAY)
            else:
                entry_order = self._client.submit_limit_order(symbol, shares, side, price, TimeInForce.DAY)
        except Exception as e:
            self._logger.error("Order submission failed for %s: %s", symbol, e)
            return False

        self._pending_entries += 1

        if self._risk._cfg.stop_loss is not None:
            filled = self._client.wait_for_fill(str(entry_order.id))
            self._pending_entries -= 1  # resolved: now in position_count() or failed
            if filled is not None:
                fill_price = float(filled.filled_avg_price or price)
                self._place_protective_stop(
                    symbol, shares, "long" if long else "short", fill_price
                )

        return True

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def close_position(self, symbol: str, reason: str = "signal") -> None:
        """Close an open position and cancel its GTC stop order."""
        self._logger.info("Closing position: %s (reason: %s)", symbol, reason)

        # Cancel associated GTC stop orders first
        open_orders = self._client.get_open_orders()
        for order in open_orders:
            if (
                order.symbol == symbol
                and order.order_type.value == "stop"
                and order.time_in_force.value == "gtc"
            ):
                self._client.cancel_order(str(order.id))

        self._client.close_position(symbol)

    def close_all(self) -> None:
        """Emergency: cancel all orders and close all positions."""
        self._logger.critical("EMERGENCY STOP: closing all positions")
        self._client.cancel_all_orders()
        self._client.close_all_positions()

    # ------------------------------------------------------------------
    # Pairs-specific helpers
    # ------------------------------------------------------------------

    def open_pair(self, long_symbol: str, short_symbol: str, hedge_ratio: float) -> bool:
        """Open a pairs trade: long leg + short leg scaled by hedge ratio."""
        if not self._risk.can_open_position():
            return False
        if not self._risk.can_add_position(self._pending_entries + 1):
            self._logger.warning("Skipping pair — max positions reached")
            return False

        try:
            quotes = self._client.get_latest_quotes([long_symbol, short_symbol])
        except Exception as e:
            self._logger.error("Quote fetch failed for pair %s/%s: %s", long_symbol, short_symbol, e)
            return False

        long_price = quotes.get(long_symbol, 0)
        short_price = quotes.get(short_symbol, 0)
        if not long_price or not short_price:
            return False

        long_shares = self._risk.calculate_shares(long_price)
        if long_shares <= 0:
            return False

        short_shares_raw = max(int(long_shares * hedge_ratio), 1)
        max_val = self._risk.max_position_value()
        if short_shares_raw * short_price > max_val:
            short_shares = max(int(max_val / short_price), 1)
            long_shares = max(int(short_shares / hedge_ratio), 1)
        else:
            short_shares = short_shares_raw

        if long_shares <= 0 or short_shares <= 0:
            return False

        order_type = self._strategy.order_type

        try:
            if order_type == "market":
                long_order = self._client.submit_market_order(long_symbol, long_shares, OrderSide.BUY, TimeInForce.DAY)
                short_order = self._client.submit_market_order(short_symbol, short_shares, OrderSide.SELL, TimeInForce.DAY)
            else:
                long_order = self._client.submit_limit_order(long_symbol, long_shares, OrderSide.BUY, long_price, TimeInForce.DAY)
                short_order = self._client.submit_limit_order(short_symbol, short_shares, OrderSide.SELL, short_price, TimeInForce.DAY)
        except Exception as e:
            self._logger.error("Pair order failed %s/%s: %s", long_symbol, short_symbol, e)
            return False

        self._pending_entries += 2

        if self._risk._cfg.stop_loss is not None:
            for order, sym, price, direction, qty in [
                (long_order, long_symbol, long_price, "long", long_shares),
                (short_order, short_symbol, short_price, "short", short_shares),
            ]:
                filled = self._client.wait_for_fill(str(order.id))
                self._pending_entries -= 1  # resolved: now in position_count() or failed
                if filled is not None:
                    fill_price = float(filled.filled_avg_price or price)
                    self._place_protective_stop(sym, qty, direction, fill_price)

        return True

    def close_pair(self, long_symbol: str, short_symbol: str, reason: str = "signal") -> None:
        self.close_position(long_symbol, reason)
        self.close_position(short_symbol, reason)
