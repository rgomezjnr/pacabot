"""Fetch and cache universe constituent ticker lists."""

import json
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

from pacabot.logging_setup import get_logger

_CACHE_DIR = Path(".cache")
_CACHE_TTL = 86400  # 24 hours
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; pacabot/1.0)"}


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


def _wiki_tables(url: str) -> list[pd.DataFrame]:
    resp = requests.get(url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return pd.read_html(StringIO(resp.text))


def _fetch_sp500() -> list[str]:
    tables = _wiki_tables("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    return tables[0]["Symbol"].tolist()


def _fetch_sp100() -> list[str]:
    tables = _wiki_tables("https://en.wikipedia.org/wiki/S%26P_100")
    for table in tables:
        cols = [str(c).lower() for c in table.columns]
        if "symbol" in cols:
            return table["Symbol"].tolist()
    raise ValueError("Could not find S&P 100 ticker table on Wikipedia")


def _fetch_nasdaq100() -> list[str]:
    tables = _wiki_tables("https://en.wikipedia.org/wiki/Nasdaq-100")
    for table in tables:
        cols = [str(c).lower() for c in table.columns]
        if "ticker" in cols:
            return table["Ticker"].tolist()
    raise ValueError("Could not find Nasdaq-100 ticker table on Wikipedia")


def _fetch_dow30() -> list[str]:
    tables = _wiki_tables("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average")
    for table in tables:
        cols = [str(c).lower() for c in table.columns]
        if "symbol" in cols:
            return table["Symbol"].tolist()
    raise ValueError("Could not find Dow 30 ticker table on Wikipedia")


def _fetch_russell1000() -> list[str]:
    tables = _wiki_tables("https://en.wikipedia.org/wiki/Russell_1000_Index")
    for table in tables:
        cols = [str(c).lower() for c in table.columns]
        if "symbol" in cols:
            return table["Symbol"].tolist()
    raise ValueError("Could not find Russell 1000 ticker table on Wikipedia")


_FETCHERS = {
    "sp500": _fetch_sp500,
    "sp100": _fetch_sp100,
    "nasdaq100": _fetch_nasdaq100,
    "dow30": _fetch_dow30,
    "russell1000": _fetch_russell1000,
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
