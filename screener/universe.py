"""
第一层：宇宙池构建
- S&P 500 + Nasdaq 100 + 自定义 watchlist
- 过滤条件：市值 > $500M，日均成交额 > $10M
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf

from screener.config import (
    CUSTOM_WATCHLIST,
    MIN_AVG_DOLLAR_VOLUME,
    MIN_MARKET_CAP,
    UNIVERSE_WORKERS,
)

logger = logging.getLogger(__name__)


def _get_sp500_tickers() -> list[str]:
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url, header=0)
        df = tables[0]
        col = next(c for c in df.columns if "symbol" in c.lower() or "ticker" in c.lower())
        return df[col].str.replace(".", "-", regex=False).str.strip().tolist()
    except Exception as e:
        logger.warning(f"S&P 500 Wikipedia fetch failed: {e}")
        return []


def _get_nasdaq100_tickers() -> list[str]:
    try:
        url = "https://en.wikipedia.org/wiki/Nasdaq-100"
        tables = pd.read_html(url, header=0)
        for df in tables:
            cols_lower = [c.lower() for c in df.columns]
            if "ticker" in cols_lower or "symbol" in cols_lower:
                col = df.columns[
                    next(i for i, c in enumerate(cols_lower) if c in ("ticker", "symbol"))
                ]
                return df[col].str.strip().tolist()
        return []
    except Exception as e:
        logger.warning(f"Nasdaq 100 Wikipedia fetch failed: {e}")
        return []


def _check_ticker(ticker: str) -> tuple[str, bool, dict]:
    """Fetch fast_info and return (ticker, passed, data)."""
    for attempt in range(2):
        try:
            stock = yf.Ticker(ticker)
            fi = stock.fast_info

            market_cap        = getattr(fi, "market_cap", None) or 0
            three_month_avg_vol = getattr(fi, "three_month_average_volume", None) or 0
            last_price        = getattr(fi, "last_price", None) or getattr(fi, "previous_close", None) or 0
            avg_dollar_volume = three_month_avg_vol * last_price

            if market_cap < MIN_MARKET_CAP or avg_dollar_volume < MIN_AVG_DOLLAR_VOLUME:
                return ticker, False, {}

            # Only fetch full info for tickers that pass basic filter
            info   = stock.info
            sector = info.get("sector") or "Unknown"
            name   = info.get("shortName") or ticker

            return ticker, True, {
                "market_cap":        market_cap,
                "avg_dollar_volume": avg_dollar_volume,
                "price":             last_price,
                "sector":            sector,
                "industry":          info.get("industry") or "Unknown",
                "name":              name,
            }
        except Exception as e:
            if attempt == 0:
                time.sleep(1)
            else:
                logger.debug(f"universe check failed for {ticker}: {e}")
    return ticker, False, {}


def get_ticker_list() -> list[str]:
    """Return the raw combined ticker list (S&P 500 + Nasdaq 100 + custom) without filtering."""
    sp500  = _get_sp500_tickers()
    nasdaq = _get_nasdaq100_tickers()
    return list(dict.fromkeys(sp500 + nasdaq + CUSTOM_WATCHLIST))


def build_universe() -> dict[str, dict]:
    """Build and return the Layer 1 universe pool."""
    logger.info("Building universe pool…")

    sp500    = _get_sp500_tickers()
    nasdaq   = _get_nasdaq100_tickers()
    combined = list(dict.fromkeys(sp500 + nasdaq + CUSTOM_WATCHLIST))  # preserve order, dedupe
    logger.info(f"Raw ticker pool: {len(combined)}")

    universe: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=UNIVERSE_WORKERS) as ex:
        futures = {ex.submit(_check_ticker, t): t for t in combined}
        for fut in as_completed(futures):
            ticker, passed, data = fut.result()
            if passed:
                universe[ticker] = data

    logger.info(f"Universe after Layer 1 filter: {len(universe)} stocks")
    return universe
