"""24/7 asyncio scheduler — market hours detection, strategy tick loop."""

import asyncio
from datetime import datetime, time, timezone

from pacabot.account import AlpacaClient
from pacabot.config import Config
from pacabot.execution import ExecutionManager
from pacabot.logging_setup import get_logger
from pacabot.reporting import generate_eod_report
from pacabot.risk import RiskManager
from pacabot.strategies.base import BaseStrategy

_TICK_INTERVAL = 60        # seconds between strategy ticks during market hours
_SLEEP_POLL = 300          # seconds between clock polls when market is closed
_MARGIN_CHECK_INTERVAL = 300  # seconds between margin checks


class Scheduler:
    def __init__(
        self,
        cfg: Config,
        client: AlpacaClient,
        risk: RiskManager,
        execution: ExecutionManager,
        strategy: BaseStrategy,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._risk = risk
        self._execution = execution
        self._strategy = strategy
        self._logger = get_logger()
        self._last_margin_check: float = 0.0
        self._eod_reported = False
        self._last_open_date: str = ""

    # ------------------------------------------------------------------
    # Market hours helpers
    # ------------------------------------------------------------------

    def _within_trading_window(self) -> bool:
        """Check if current Eastern time is within the configured trading window."""
        exec_cfg = self._cfg.execution
        if exec_cfg.trading_start is None and exec_cfg.trading_end is None:
            return True

        # Use UTC and offset for Eastern time approximation
        now_utc = datetime.now(tz=timezone.utc)
        # Eastern = UTC-5 (EST) / UTC-4 (EDT); use UTC-4 as conservative estimate
        now_eastern_hour = (now_utc.hour - 4) % 24
        now_eastern_min = now_utc.minute
        now_t = time(now_eastern_hour, now_eastern_min)

        if exec_cfg.trading_start:
            h, m = map(int, exec_cfg.trading_start.split(":"))
            if now_t < time(h, m):
                return False

        if exec_cfg.trading_end:
            h, m = map(int, exec_cfg.trading_end.split(":"))
            if now_t > time(h, m):
                return False

        return True

    def _window_sleep_secs(self) -> float:
        """Return seconds to sleep when outside the trading window, and log why."""
        now_utc = datetime.now(tz=timezone.utc)
        eastern_h = (now_utc.hour - 4) % 24
        eastern_m = now_utc.minute
        eastern_s = now_utc.second
        now_secs = eastern_h * 3600 + eastern_m * 60 + eastern_s

        exec_cfg = self._cfg.execution
        if exec_cfg.trading_start:
            ts_h, ts_m = map(int, exec_cfg.trading_start.split(":"))
            window_open_secs = ts_h * 3600 + ts_m * 60
            if now_secs < window_open_secs:
                secs = min(window_open_secs - now_secs, _SLEEP_POLL)
                self._logger.info(
                    "Before trading window (%02d:%02d ET) — sleeping %.0fs until %s ET",
                    eastern_h, eastern_m, secs, exec_cfg.trading_start,
                )
                return secs

        if exec_cfg.trading_end:
            self._logger.debug(
                "After trading window (%02d:%02d ET, end was %s ET)",
                eastern_h, eastern_m, exec_cfg.trading_end,
            )

        return _TICK_INTERVAL

    async def _get_clock(self):
        return await asyncio.to_thread(self._client.get_clock)

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------

    async def _startup(self) -> None:
        self._logger.info(
            "pacabot starting — account: %s (%s), strategy: %s",
            self._cfg.account.name,
            "paper" if self._cfg.account.paper else "live",
            self._cfg.strategy.name,
        )
        await asyncio.to_thread(self._risk.initialise_day)
        await asyncio.to_thread(self._execution.reconcile_stop_orders)
        await asyncio.to_thread(self._strategy.on_startup)
        self._logger.info("Startup reconciliation complete")

    # ------------------------------------------------------------------
    # Periodic margin check
    # ------------------------------------------------------------------

    async def _maybe_check_margin(self) -> None:
        import time as _time
        now = _time.monotonic()
        if now - self._last_margin_check >= _MARGIN_CHECK_INTERVAL:
            await asyncio.to_thread(self._risk.check_margin)
            await asyncio.to_thread(self._risk.check_daily_loss)
            self._last_margin_check = now

    # ------------------------------------------------------------------
    # End-of-day report
    # ------------------------------------------------------------------

    async def _maybe_eod_report(self, clock) -> None:
        if not clock.is_open and not self._eod_reported:
            # Market just closed — generate report
            today_str = str(clock.timestamp.date()) if hasattr(clock, "timestamp") else ""
            if today_str != self._last_open_date:
                return  # Haven't traded today
            await asyncio.to_thread(generate_eod_report, self._cfg, self._client, self._risk)
            self._eod_reported = True

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        await self._startup()

        while True:
            if self._risk.is_halted:
                self._logger.critical(
                    "Bot is halted. Fix the account issue and restart pacabot."
                )
                await asyncio.sleep(3600)
                continue

            try:
                clock = await self._get_clock()
            except Exception as e:
                self._logger.error("Failed to get market clock: %s — retrying in 60s", e)
                await asyncio.sleep(60)
                continue

            if clock.is_open:
                today_str = str(clock.timestamp.date()) if hasattr(clock, "timestamp") else ""
                if today_str != self._last_open_date:
                    # New trading day
                    await asyncio.to_thread(self._risk.initialise_day)
                    self._last_open_date = today_str
                    self._eod_reported = False

                if self._within_trading_window():
                    try:
                        await asyncio.to_thread(self._strategy.tick)
                    except Exception as e:
                        self._logger.error("Strategy tick error: %s", e)

                    await self._maybe_check_margin()
                    await asyncio.sleep(_TICK_INTERVAL)
                else:
                    await asyncio.sleep(self._window_sleep_secs())

            else:
                await self._maybe_eod_report(clock)

                # Sleep until next open (capped at _SLEEP_POLL to check periodically)
                try:
                    next_open = clock.next_open
                    now_utc = datetime.now(tz=timezone.utc)
                    if hasattr(next_open, "tzinfo") and next_open.tzinfo is None:
                        next_open = next_open.replace(tzinfo=timezone.utc)
                    seconds_until_open = max(0, (next_open - now_utc).total_seconds())
                    sleep_secs = min(seconds_until_open, _SLEEP_POLL)
                    self._logger.info(
                        "Market closed — sleeping %.0fs (next open: %s)",
                        sleep_secs,
                        next_open.strftime("%Y-%m-%d %H:%M ET") if hasattr(next_open, "strftime") else "unknown",
                    )
                except Exception:
                    sleep_secs = _SLEEP_POLL

                await asyncio.sleep(sleep_secs)
