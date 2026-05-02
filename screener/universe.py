"""
第一层：宇宙池构建
- S&P 500 + Nasdaq 100 + Russell 2000 + 自定义 watchlist
- 过滤条件：市值 > $200M，日均成交额 > $3M
"""
import io
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import yfinance as yf

from screener.config import (
    CUSTOM_WATCHLIST,
    UNIVERSE_WORKERS,
)

logger = logging.getLogger(__name__)

# 第一层过滤阈值（模块级，独立于 config.py 的回测阈值）
_MIN_MARKET_CAP        = 200_000_000   # $200M （扩展中小盘）
_MIN_AVG_DOLLAR_VOLUME =   3_000_000   # $3M


def _get_sp500_tickers() -> list[str]:
    try:
        url    = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url, header=0)
        df     = tables[0]
        col    = next(c for c in df.columns if "symbol" in c.lower() or "ticker" in c.lower())
        return df[col].str.replace(".", "-", regex=False).str.strip().tolist()
    except Exception as e:
        logger.warning(f"S&P 500 Wikipedia fetch failed: {e}")
        return []


def _get_nasdaq100_tickers() -> list[str]:
    try:
        url    = "https://en.wikipedia.org/wiki/Nasdaq-100"
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


def _get_russell2000_tickers() -> list[str]:
    """Download IWM holdings CSV and extract Russell 2000 equity ticker symbols."""
    url = (
        "https://www.ishares.com/us/products/239710/"
        "ishares-russell-2000-etf/1467271812596.ajax"
        "?fileType=csv&fileName=IWM_holdings&dataType=fund"
    )
    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (compatible; CanyonScreener/1.0)",
            "Referer":    "https://www.ishares.com/",
        })
        resp.raise_for_status()

        lines      = resp.text.splitlines()
        header_idx = None
        for i, line in enumerate(lines):
            parts = [p.strip().lower() for p in line.split(",")]
            if "ticker" in parts and ("name" in parts or "asset class" in parts):
                header_idx = i
                break

        if header_idx is None:
            logger.warning("Russell 2000: header row not found in IWM CSV")
            return []

        df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), thousands=",")
        ticker_col = next(
            (c for c in df.columns if c.strip().lower() == "ticker"), None
        )
        if ticker_col is None:
            return []

        raw   = df[ticker_col].dropna().str.strip().tolist()
        valid = [t for t in raw if re.match(r"^[A-Z]{1,5}$", t)]
        logger.info(f"Russell 2000 (IWM): {len(valid)} tickers loaded")
        return valid
    except Exception as e:
        logger.warning(f"Russell 2000 IWM fetch failed (universe will use SP500+NDX100 only): {e}")
        return []


def _check_ticker(ticker: str) -> tuple[str, bool, dict]:
    """Fetch fast_info and return (ticker, passed, data)."""
    for attempt in range(2):
        try:
            stock = yf.Ticker(ticker)
            fi    = stock.fast_info

            market_cap          = getattr(fi, "market_cap", None) or 0
            three_month_avg_vol = getattr(fi, "three_month_average_volume", None) or 0
            last_price          = (
                getattr(fi, "last_price", None)
                or getattr(fi, "previous_close", None)
                or 0
            )
            avg_dollar_volume = three_month_avg_vol * last_price

            if market_cap < _MIN_MARKET_CAP or avg_dollar_volume < _MIN_AVG_DOLLAR_VOLUME:
                return ticker, False, {}

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
    """Return the raw combined ticker list (SP500 + NDX100 + Russell2000 + custom)."""
    sp500   = _get_sp500_tickers()
    nasdaq  = _get_nasdaq100_tickers()
    russell = _get_russell2000_tickers()
    return list(dict.fromkeys(sp500 + nasdaq + russell + CUSTOM_WATCHLIST))


def build_universe() -> dict[str, dict]:
    """Build and return the Layer 1 universe pool."""
    logger.info("Building universe pool…")

    sp500   = _get_sp500_tickers()
    nasdaq  = _get_nasdaq100_tickers()
    russell = _get_russell2000_tickers()
    combined = list(dict.fromkeys(sp500 + nasdaq + russell + CUSTOM_WATCHLIST))
    logger.info(
        f"Raw ticker pool: {len(combined)} "
        f"(SP500={len(sp500)}, NDX100={len(nasdaq)}, "
        f"Russell2000={len(russell)}, custom={len(CUSTOM_WATCHLIST)})"
    )

    universe: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=UNIVERSE_WORKERS) as ex:
        futures = {ex.submit(_check_ticker, t): t for t in combined}
        for fut in as_completed(futures):
            ticker, passed, data = fut.result()
            if passed:
                universe[ticker] = data

    logger.info(f"Universe after Layer 1 filter: {len(universe)} stocks")
    return universe
