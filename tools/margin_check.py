#!/usr/bin/env python3
"""Show current margin utilization for an Alpaca account."""

import argparse
import os
import sys

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient


def get_credentials() -> tuple[str, str]:
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        print("Error: ALPACA_API_KEY and ALPACA_SECRET_KEY must be set.", file=sys.stderr)
        sys.exit(1)
    return api_key, secret_key


def get_client_and_account(api_key: str, secret_key: str):
    for paper in (True, False):
        client = TradingClient(api_key=api_key, secret_key=secret_key, paper=paper)
        try:
            acct = client.get_account()
            return paper, acct
        except APIError:
            continue
    print("Error: credentials rejected by both paper and live endpoints.", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    argparse.ArgumentParser(description="Show Alpaca account margin utilization.").parse_args()
    api_key, secret_key = get_credentials()
    paper, acct = get_client_and_account(api_key, secret_key)

    equity = float(acct.equity)
    initial_margin = float(acct.initial_margin)
    maintenance_margin = float(acct.maintenance_margin)
    buying_power = float(acct.buying_power)

    utilization = initial_margin / equity if equity > 0 else 1.0

    account_type = "paper" if paper else "live"
    print(f"Account:             {acct.account_number} ({account_type})")
    print(f"Equity:              ${equity:,.2f}")
    print(f"Buying power:        ${buying_power:,.2f}")
    print(f"Initial margin:      ${initial_margin:,.2f}")
    print(f"Maintenance margin:  ${maintenance_margin:,.2f}")
    print(f"Margin utilization:  {utilization * 100:.1f}%")


if __name__ == "__main__":
    main()
