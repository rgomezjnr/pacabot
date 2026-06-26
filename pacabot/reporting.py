"""End-of-day P&L summary reporting."""

from datetime import date, datetime
from pathlib import Path

from pacabot.account import AlpacaClient
from pacabot.config import Config
from pacabot.logging_setup import get_logger
from pacabot.risk import RiskManager

_REPORTS_DIR = Path("reports")


def generate_eod_report(cfg: Config, client: AlpacaClient, risk: RiskManager) -> None:
    logger = get_logger()
    today = date.today()
    account = client.get_account()
    positions = client.get_positions()

    equity = float(account.equity)
    daily = risk._daily
    start_equity = daily.starting_equity if daily else equity
    day_pnl = equity - start_equity
    day_pnl_pct = (day_pnl / start_equity * 100) if start_equity else 0.0

    margin_util = risk.margin_utilization()

    lines = [
        "=" * 60,
        f"  pacabot — End-of-Day Report: {today}",
        "=" * 60,
        f"  Account   : {cfg.account.name} ({'paper' if cfg.account.paper else 'live'})",
        f"  Strategy  : {cfg.strategy.name}",
        f"  Universe  : {cfg.strategy.universe}",
        "",
        "  EQUITY",
        f"    Start of day : ${start_equity:>12,.2f}",
        f"    End of day   : ${equity:>12,.2f}",
        f"    Day P&L      : ${day_pnl:>+12,.2f}  ({day_pnl_pct:+.2f}%)",
        "",
        f"  MARGIN UTILIZATION: {margin_util * 100:.1f}%",
        "",
        f"  OPEN POSITIONS ({len(positions)})",
    ]

    if positions:
        for pos in positions:
            unrealised = float(pos.unrealized_pl)
            lines.append(
                f"    {pos.symbol:<8} qty={pos.qty:<8} "
                f"entry=${float(pos.avg_entry_price):.2f}  "
                f"unrealised P&L=${unrealised:+.2f}"
            )
    else:
        lines.append("    (none)")

    lines += ["", "=" * 60, ""]

    report_text = "\n".join(lines)

    # Emit through the logger so the report reaches both the console and the
    # log file (print() bypasses logging and is lost when stdout is detached).
    logger.info("End-of-day report:\n%s", report_text)

    # Save to file
    _REPORTS_DIR.mkdir(exist_ok=True)
    report_file = _REPORTS_DIR / f"report_{today}.txt"
    report_file.write_text(report_text, encoding="utf-8")
    logger.info("End-of-day report saved to %s", report_file)
