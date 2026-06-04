"""Alpaca client wrappers for trading and market data."""

import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.models import Order, Position
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)

from pacabot.logging_setup import get_logger


def _get_credentials() -> tuple[str, str]:
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        print(
            "[pacabot] Error: ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables must be set.",
            file=sys.stderr,
        )
        sys.exit(1)
    return api_key, secret_key


class AlpacaClient:
    def __init__(self, paper: bool) -> None:
        api_key, secret_key = _get_credentials()
        self.trading = TradingClient(api_key=api_key, secret_key=secret_key, paper=paper)
        self.data = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        self._paper = paper
        self._logger = get_logger()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account(self):
        return self.trading.get_account()

    def get_clock(self):
        return self.trading.get_clock()

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> list[Position]:
        return self.trading.get_all_positions()

    def get_position(self, symbol: str) -> Position | None:
        try:
            return self.trading.get_open_position(symbol)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def get_open_orders(self) -> list[Order]:
        req = GetOrdersRequest(status="open")
        return self.trading.get_orders(filter=req)

    def cancel_all_orders(self) -> None:
        self.trading.cancel_orders()
        self._logger.info("All open orders cancelled")

    def cancel_order(self, order_id: str) -> None:
        self.trading.cancel_order_by_id(order_id)

    def submit_market_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        tif: TimeInForce = TimeInForce.DAY,
    ) -> Order:
        req = MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=tif)
        order = self.trading.submit_order(req)
        self._logger.info(
            "Market order submitted: %s %s x%.4f [%s]", side.value, symbol, qty, tif.value
        )
        return order

    def submit_limit_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        limit_price: float,
        tif: TimeInForce = TimeInForce.DAY,
    ) -> Order:
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=round(limit_price, 2),
            time_in_force=tif,
        )
        order = self.trading.submit_order(req)
        self._logger.info(
            "Limit order submitted: %s %s x%.4f @ $%.2f [%s]",
            side.value, symbol, qty, limit_price, tif.value,
        )
        return order

    def wait_for_fill(self, order_id: str, timeout: float = 10.0) -> bool:
        """Poll until the order is filled or timeout is reached. Runs synchronously (call from a thread)."""
        import time as _time
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            order = self.trading.get_order_by_id(order_id)
            status = order.status.value
            if status == "filled":
                return True
            if status in ("canceled", "expired", "rejected", "done_for_day"):
                self._logger.warning("Order %s ended with status '%s'; stop skipped", order_id, status)
                return False
            _time.sleep(0.5)
        self._logger.warning("Order %s not filled within %.0fs; stop skipped", order_id, timeout)
        return False

    def submit_stop_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        stop_price: float,
    ) -> Order:
        req = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            stop_price=round(stop_price, 2),
            time_in_force=TimeInForce.GTC,
        )
        order = self.trading.submit_order(req)
        self._logger.info(
            "Stop order submitted: %s %s x%.4f stop @ $%.2f [GTC]",
            side.value, symbol, qty, stop_price,
        )
        return order

    def close_position(self, symbol: str) -> None:
        try:
            self.trading.close_position(symbol)
            self._logger.info("Position closed: %s", symbol)
        except Exception as e:
            self._logger.error("Failed to close position %s: %s", symbol, e)

    def close_all_positions(self) -> None:
        self.trading.close_all_positions(cancel_orders=True)
        self._logger.critical("All positions closed and orders cancelled")

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_bars(
        self,
        symbols: list[str],
        days: int,
        timeframe: TimeFrame = TimeFrame.Day,
    ) -> pd.DataFrame:
        """Return a DataFrame of OHLCV bars with MultiIndex (symbol, timestamp)."""
        end = datetime.now(tz=timezone.utc)
        # days is in trading days; multiply by 365/252 to get calendar days, plus a
        # fixed 10-day buffer for holidays/gaps at the edges of the window.
        calendar_days = int(days * 365 / 252) + 10
        start = end - timedelta(days=calendar_days)
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=timeframe,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        bars = self.data.get_stock_bars(req)
        return bars.df

    def get_latest_quotes(self, symbols: list[str]) -> dict[str, float]:
        """Return latest ask price for each symbol."""
        from alpaca.data.requests import StockLatestQuoteRequest
        req = StockLatestQuoteRequest(symbol_or_symbols=symbols)
        quotes = self.data.get_stock_latest_quote(req)
        return {sym: float(q.ask_price) for sym, q in quotes.items()}

    def get_close_prices(self, symbols: list[str], days: int) -> pd.DataFrame:
        """Return a DataFrame of close prices with symbols as columns, dates as index."""
        bars_df = self.get_bars(symbols, days)
        if bars_df.empty:
            return pd.DataFrame()
        close = bars_df["close"].unstack(level=0)
        close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
        return close.sort_index()
