#!/usr/bin/env python3
"""pacabot — Alpaca algorithmic trading CLI."""

import argparse
import asyncio
import sys

from pacabot.account import AlpacaClient
from pacabot.backtest import run_backtest
from pacabot.config import load_config
from pacabot.execution import ExecutionManager
from pacabot.logging_setup import setup_logging
from pacabot.risk import RiskManager
from pacabot.scheduler import Scheduler


def _build_strategy(cfg, client, risk, execution):
    from pacabot.strategies.momentum import MomentumStrategy
    from pacabot.strategies.mean_reversion import MeanReversionStrategy
    from pacabot.strategies.pairs import PairsStrategy

    name = cfg.strategy.name
    if name == "cross-sectional-momentum":
        return MomentumStrategy(cfg, client, risk, execution)
    if name == "mean-reversion":
        return MeanReversionStrategy(cfg, client, risk, execution)
    if name == "pairs-trading":
        return PairsStrategy(cfg, client, risk, execution)
    print(f"[pacabot] Unknown strategy: {name}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pacabot",
        description="Alpaca algorithmic trading bot",
    )
    parser.add_argument(
        "--config", "-c",
        required=True,
        metavar="FILE",
        help="Path to TOML config file",
    )
    parser.add_argument(
        "--log-level",
        choices=["critical", "error", "warning", "info", "debug"],
        default=None,
        help="Override log level from config",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--backtest",
        action="store_true",
        help="Run backtest instead of live trading",
    )
    mode.add_argument(
        "--close-all",
        action="store_true",
        help="Emergency stop: cancel all orders and close all positions, then exit",
    )

    parser.add_argument(
        "--start",
        metavar="YYYY-MM-DD",
        help="Backtest start date (required with --backtest)",
    )
    parser.add_argument(
        "--end",
        metavar="YYYY-MM-DD",
        help="Backtest end date (required with --backtest)",
    )

    args = parser.parse_args()

    # Validate backtest args
    if args.backtest:
        if not args.start or not args.end:
            parser.error("--backtest requires --start and --end")

    # Load and validate config
    cfg = load_config(args.config)

    # Set up logging
    setup_logging(cfg.logging, level_override=args.log_level)

    from pacabot.logging_setup import get_logger
    logger = get_logger()

    # ------------------------------------------------------------------
    # Backtest mode
    # ------------------------------------------------------------------
    if args.backtest:
        run_backtest(cfg, args.start, args.end)
        return

    # ------------------------------------------------------------------
    # Emergency stop
    # ------------------------------------------------------------------
    client = AlpacaClient(paper=cfg.account.paper)

    if args.close_all:
        logger.critical("--close-all: cancelling all orders and closing all positions")
        client.cancel_all_orders()
        client.close_all_positions()
        logger.critical("--close-all complete. Exiting.")
        return

    # ------------------------------------------------------------------
    # Live / paper trading
    # ------------------------------------------------------------------
    risk = RiskManager(cfg.risk, client)
    execution = ExecutionManager(client, risk, cfg.strategy)
    strategy = _build_strategy(cfg, client, risk, execution)
    scheduler = Scheduler(cfg, client, risk, execution, strategy)

    logger.info("pacabot running — press Ctrl+C to stop")
    try:
        asyncio.run(scheduler.run())
    except KeyboardInterrupt:
        logger.info("Shutdown requested — exiting pacabot")


if __name__ == "__main__":
    main()
