"""Fetch and cache universe constituent ticker lists."""

import json
import time
from pathlib import Path

import pandas as pd
import requests

from pacabot.logging_setup import get_logger

_CACHE_DIR = Path(".cache")
_CACHE_TTL = 86400  # 24 hours


def _cache_path(slug: str) -> Path:
    return _CACHE_DIR / f"{slug}.json"


def _load_cache(slug: str) -> list[str] | None:
    path = _cache_path(slug)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if time.time() - data["ts"] < _CACHE_TTL:
            return data["tickers"]
    except Exception:
        pass
    return None


def _save_cache(slug: str, tickers: list[str]) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(slug).write_text(json.dumps({"ts": time.time(), "tickers": tickers}))


def _fetch_sp500() -> list[str]:
    tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    tickers = tables[0]["Symbol"].tolist()
    return [t.replace(".", "-") for t in tickers]


def _fetch_sp100() -> list[str]:
    tables = pd.read_html("https://en.wikipedia.org/wiki/S%26P_100")
    for table in tables:
        cols = [c.lower() for c in table.columns]
        if "symbol" in cols:
            return [t.replace(".", "-") for t in table["Symbol"].tolist()]
    raise ValueError("Could not find S&P 100 ticker table on Wikipedia")


def _fetch_nasdaq100() -> list[str]:
    tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
    for table in tables:
        cols = [str(c).lower() for c in table.columns]
        if "ticker" in cols:
            return [t.replace(".", "-") for t in table["Ticker"].tolist()]
    raise ValueError("Could not find Nasdaq-100 ticker table on Wikipedia")


def _fetch_dow30() -> list[str]:
    tables = pd.read_html("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average")
    for table in tables:
        cols = [str(c).lower() for c in table.columns]
        if "symbol" in cols:
            return [t.replace(".", "-") for t in table["Symbol"].tolist()]
    raise ValueError("Could not find Dow 30 ticker table on Wikipedia")


def _fetch_ishares(etf: str) -> list[str]:
    """Fetch holdings from iShares ETF CSV (Russell 1000 = IWB, Russell 2000 = IWM)."""
    url = (
        f"https://www.ishares.com/us/products/"
        + ("239707/ISHARES-RUSSELL-1000-ETF" if etf == "IWB" else "239710/ISHARES-RUSSELL-2000-ETF")
        + "/1467271812596.ajax?tab=holdings&fileType=csv"
    )
    headers = {"User-Agent": "Mozilla/5.0 (compatible; pacabot/1.0)"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    lines = resp.text.splitlines()
    # iShares CSV has header rows before the actual data; find the Ticker column
    start = 0
    for i, line in enumerate(lines):
        if "Ticker" in line and "Name" in line:
            start = i
            break
    from io import StringIO
    df = pd.read_csv(StringIO("\n".join(lines[start:])), on_bad_lines="skip")
    if "Ticker" not in df.columns:
        raise ValueError(f"Could not parse iShares holdings CSV for {etf}")
    tickers = df["Ticker"].dropna().tolist()
    # Filter to valid ticker strings (exclude cash, futures lines)
    return [
        str(t).replace(".", "-")
        for t in tickers
        if isinstance(t, str) and t.isalpha() and len(t) <= 5
    ]


_FETCHERS = {
    "sp500": _fetch_sp500,
    "sp100": _fetch_sp100,
    "nasdaq100": _fetch_nasdaq100,
    "dow30": _fetch_dow30,
    "russell1000": lambda: _fetch_ishares("IWB"),
    "russell2000": lambda: _fetch_ishares("IWM"),
}


def get_universe(slug: str, custom_tickers: list[str] | None = None) -> list[str]:
    """Return ticker list for the given universe slug."""
    logger = get_logger()

    if slug == "custom":
        if not custom_tickers:
            raise ValueError("custom_tickers must be provided when universe = 'custom'")
        return custom_tickers

    cached = _load_cache(slug)
    if cached:
        logger.debug("Universe '%s' loaded from cache (%d tickers)", slug, len(cached))
        return cached

    logger.info("Fetching universe '%s' constituents...", slug)
    fetcher = _FETCHERS.get(slug)
    if not fetcher:
        raise ValueError(f"Unknown universe slug: '{slug}'")

    tickers = fetcher()
    if not tickers:
        raise ValueError(f"Universe '{slug}' returned empty ticker list")

    _save_cache(slug, tickers)
    logger.info("Universe '%s' fetched: %d tickers", slug, len(tickers))
    return tickers
