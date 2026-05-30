"""Abstract base class for all trading strategies."""

import json
from abc import ABC, abstractmethod
from datetime import date, datetime
from pathlib import Path

from pacabot.account import AlpacaClient
from pacabot.config import Config
from pacabot.execution import ExecutionManager
from pacabot.logging_setup import get_logger
from pacabot.risk import RiskManager

_STATE_DIR = Path(".state")


class BaseStrategy(ABC):
    def __init__(
        self,
        cfg: Config,
        client: AlpacaClient,
        risk: RiskManager,
        execution: ExecutionManager,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._risk = risk
        self._execution = execution
        self._logger = get_logger()
        self._state: dict = {}
        self._load_state()

    # ------------------------------------------------------------------
    # State persistence (survives restarts)
    # ------------------------------------------------------------------

    def _state_path(self) -> Path:
        _STATE_DIR.mkdir(exist_ok=True)
        safe_name = self._cfg.account.name.replace(" ", "_")
        return _STATE_DIR / f"{safe_name}.json"

    def _load_state(self) -> None:
        path = self._state_path()
        if path.exists():
            try:
                self._state = json.loads(path.read_text())
                self._logger.debug("Strategy state loaded from %s", path)
            except Exception as e:
                self._logger.warning("Failed to load strategy state: %s", e)
                self._state = {}
        else:
            self._state = {}

    def _save_state(self) -> None:
        try:
            self._state_path().write_text(json.dumps(self._state, default=str))
        except Exception as e:
            self._logger.error("Failed to save strategy state: %s", e)

    # ------------------------------------------------------------------
    # Rebalance schedule
    # ------------------------------------------------------------------

    def _last_rebalance(self) -> date | None:
        ts = self._state.get("last_rebalance")
        if ts:
            try:
                return date.fromisoformat(ts)
            except Exception:
                pass
        return None

    def _mark_rebalanced(self) -> None:
        self._state["last_rebalance"] = date.today().isoformat()
        self._save_state()

    def should_rebalance(self) -> bool:
        freq = getattr(self._cfg.strategy.parameters, "rebalance_frequency", None) or \
               getattr(self._cfg.strategy.parameters, "recalculate_frequency", None)
        if not freq:
            return False

        last = self._last_rebalance()
        today = date.today()

        if last is None:
            return True

        if freq == "daily":
            return last < today
        if freq == "weekly":
            return (today - last).days >= 7
        if freq == "monthly":
            return today.month != last.month or today.year != last.year

        return False

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def on_startup(self) -> None:
        """Called once at startup after GTC reconciliation."""

    @abstractmethod
    def tick(self) -> None:
        """Called on each market-hours tick (typically every minute)."""
