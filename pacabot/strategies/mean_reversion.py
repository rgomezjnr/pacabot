"""Mean reversion strategy — RSI, Bollinger Bands, or z-score."""

from datetime import date, datetime

import numpy as np
import pandas as pd

from pacabot.account import AlpacaClient
from pacabot.config import (
    BollingerBandsParameters,
    Config,
    MeanReversionParameters,
    RSIParameters,
    ZScoreParameters,
)
from pacabot.execution import ExecutionManager
from pacabot.risk import RiskManager
from pacabot.strategies.base import BaseStrategy
from pacabot.universe import get_universe


# ---------------------------------------------------------------------------
# Indicator calculations
# ---------------------------------------------------------------------------

def _calc_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _calc_bollinger(close: pd.Series, period: int, std_dev: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    mean = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mean + std_dev * std
    lower = mean - std_dev * std
    return lower, mean, upper


def _calc_zscore(close: pd.Series, period: int) -> pd.Series:
    mean = close.rolling(period).mean()
    std = close.rolling(period).std()
    return (close - mean) / std.replace(0, np.nan)


# ---------------------------------------------------------------------------
# Signal generators
# ---------------------------------------------------------------------------

def _rsi_entry_signal(close: pd.DataFrame, params: RSIParameters) -> pd.Series:
    """Return boolean Series: True where RSI < entry threshold (oversold)."""
    result = {}
    for col in close.columns:
        rsi = _calc_rsi(close[col].dropna(), params.period)
        result[col] = rsi.iloc[-1] < params.entry_threshold if len(rsi) >= params.period else False
    return pd.Series(result)


def _rsi_exit_signal(close: pd.DataFrame, params: RSIParameters) -> pd.Series:
    result = {}
    for col in close.columns:
        rsi = _calc_rsi(close[col].dropna(), params.period)
        result[col] = rsi.iloc[-1] > params.exit_threshold if len(rsi) >= params.period else False
    return pd.Series(result)


def _bb_entry_signal(close: pd.DataFrame, params: BollingerBandsParameters) -> pd.Series:
    result = {}
    for col in close.columns:
        s = close[col].dropna()
        if len(s) < params.period:
            result[col] = False
            continue
        lower, _, _ = _calc_bollinger(s, params.period, params.std_dev)
        result[col] = s.iloc[-1] < lower.iloc[-1]
    return pd.Series(result)


def _bb_exit_signal(close: pd.DataFrame, params: BollingerBandsParameters) -> pd.Series:
    result = {}
    for col in close.columns:
        s = close[col].dropna()
        if len(s) < params.period:
            result[col] = False
            continue
        lower, mean, upper = _calc_bollinger(s, params.period, params.std_dev)
        target = upper if params.exit_band == "upper" else mean
        result[col] = s.iloc[-1] > target.iloc[-1]
    return pd.Series(result)


def _zscore_entry_signal(close: pd.DataFrame, params: ZScoreParameters) -> pd.Series:
    result = {}
    for col in close.columns:
        s = close[col].dropna()
        if len(s) < params.period:
            result[col] = False
            continue
        z = _calc_zscore(s, params.period)
        result[col] = z.iloc[-1] < params.entry_threshold
    return pd.Series(result)


def _zscore_exit_signal(close: pd.DataFrame, params: ZScoreParameters) -> pd.Series:
    result = {}
    for col in close.columns:
        s = close[col].dropna()
        if len(s) < params.period:
            result[col] = False
            continue
        z = _calc_zscore(s, params.period)
        result[col] = z.iloc[-1] > params.exit_threshold
    return pd.Series(result)


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class MeanReversionStrategy(BaseStrategy):
    def __init__(
        self,
        cfg: Config,
        client: AlpacaClient,
        risk: RiskManager,
        execution: ExecutionManager,
    ) -> None:
        super().__init__(cfg, client, risk, execution)
        self._params: MeanReversionParameters = cfg.strategy.parameters  # type: ignore[assignment]
        self._universe: list[str] = []

    def _indicator_period(self) -> int:
        p = self._params
        if p.rsi:
            return p.rsi.period
        if p.bollinger_bands:
            return p.bollinger_bands.period
        if p.zscore:
            return p.zscore.period
        return 20

    # ------------------------------------------------------------------
    # Holding-day tracking (loss-only time stop)
    # ------------------------------------------------------------------

    def _record_entry(self, symbol: str, entry_price: float) -> None:
        entries = self._state.setdefault("entries", {})
        entries[symbol] = {
            "date": date.today().isoformat(),
            "price": entry_price,
        }
        self._save_state()

    def _clear_entry(self, symbol: str) -> None:
        self._state.get("entries", {}).pop(symbol, None)
        self._save_state()

    def _days_held(self, symbol: str) -> int:
        entry = self._state.get("entries", {}).get(symbol)
        if not entry:
            return 0
        try:
            entry_date = date.fromisoformat(entry["date"])
            return (date.today() - entry_date).days
        except Exception:
            return 0

    def _entry_price(self, symbol: str) -> float | None:
        entry = self._state.get("entries", {}).get(symbol)
        return float(entry["price"]) if entry else None

    def _time_stop_triggered(self, symbol: str, current_price: float) -> bool:
        """Loss-only time stop: only exit if held >= max_holding_days AND at a loss."""
        if self._params.max_holding_days is None:
            return False
        if self._days_held(symbol) < self._params.max_holding_days:
            return False
        entry_price = self._entry_price(symbol)
        if entry_price is None:
            return False
        # Only trigger if currently at a loss (long-only strategy)
        return current_price < entry_price

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _compute_entry_signals(self, close: pd.DataFrame) -> pd.Series:
        p = self._params
        if p.rsi:
            return _rsi_entry_signal(close, p.rsi)
        if p.bollinger_bands:
            return _bb_entry_signal(close, p.bollinger_bands)
        if p.zscore:
            return _zscore_entry_signal(close, p.zscore)
        return pd.Series(dtype=bool)

    def _compute_exit_signals(self, close: pd.DataFrame) -> pd.Series:
        p = self._params
        if p.rsi:
            return _rsi_exit_signal(close, p.rsi)
        if p.bollinger_bands:
            return _bb_exit_signal(close, p.bollinger_bands)
        if p.zscore:
            return _zscore_exit_signal(close, p.zscore)
        return pd.Series(dtype=bool)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def on_startup(self) -> None:
        self._universe = get_universe(
            self._cfg.strategy.universe,
            self._cfg.strategy.custom_tickers or None,
        )
        # Reconcile state: remove entries for positions no longer open
        positions = {p.symbol for p in self._client.get_positions()}
        stale = [s for s in self._state.get("entries", {}) if s not in positions]
        for s in stale:
            self._clear_entry(s)

        self._logger.info(
            "Mean reversion strategy initialised — universe: %s (%d tickers), "
            "indicator: %s",
            self._cfg.strategy.universe,
            len(self._universe),
            self._params.indicator,
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

        period = self._indicator_period()
        days_needed = period
        try:
            close = self._client.get_close_prices(self._universe, days_needed)
        except Exception as e:
            self._logger.error("Failed to fetch price data: %s", e)
            return

        if close.empty:
            return

        entry_signals = self._compute_entry_signals(close)
        exit_signals = self._compute_exit_signals(close)

        positions = {p.symbol: p for p in self._client.get_positions()}

        # --- Process exits first ---
        for symbol, pos in positions.items():
            if symbol not in self._universe:
                continue
            current_price = float(pos.current_price or pos.avg_entry_price)

            # Time stop (loss only)
            if self._time_stop_triggered(symbol, current_price):
                self._execution.close_position(symbol, reason="time stop (loss)")
                self._clear_entry(symbol)
                continue

            # Indicator exit signal
            if exit_signals.get(symbol, False):
                self._execution.close_position(symbol, reason="indicator exit")
                self._clear_entry(symbol)

        # --- Process entries ---
        positions = {p.symbol for p in self._client.get_positions()}  # refresh
        self._execution.reset_pending_entries()
        for symbol in self._universe:
            if symbol in positions:
                continue
            if not entry_signals.get(symbol, False):
                continue
            if self._execution.open_position(symbol, long=True):
                # Record entry price for time stop tracking
                try:
                    quotes = self._client.get_latest_quotes([symbol])
                    price = quotes.get(symbol, 0)
                    if price:
                        self._record_entry(symbol, price)
                except Exception:
                    pass
