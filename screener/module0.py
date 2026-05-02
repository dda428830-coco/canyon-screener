"""
第二层：Module 0 扫描器
S0-1 价格动量   — 5日超额收益 vs QQQ/SPY > 3%
S0-2 事件驱动   — 未来 10 个交易日内有财报
S0-3 成交放大   — 3日均量 / 20日均量 > 1.5x
S0-4 空头挤压   — short ratio > 5
满足任意一条即进入第三层。
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import numpy as np
import pandas as pd
import yfinance as yf

from screener.config import (
    MODULE0_WORKERS,
    S0_EARNINGS_WINDOW,
    S0_MOMENTUM_THRESHOLD,
    S0_SHORT_RATIO,
    S0_VOLUME_RATIO,
)

logger = logging.getLogger(__name__)


# ── 交易日计算 ─────────────────────────────────────────────────────────────────

def _trading_days_between(start: date, end: date) -> int:
    """Approximate NYSE trading days between two dates (no external library needed)."""
    if end <= start:
        return 0
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        schedule = nyse.schedule(start_date=start, end_date=end)
        return max(0, len(schedule) - 1)
    except Exception:
        # Fallback: business days approximation
        bdays = np.busday_count(start, end)
        return max(0, int(bdays))


def _next_earnings_date(ticker: str) -> date | None:
    """Return the next earnings date or None."""
    try:
        stock = yf.Ticker(ticker)

        # Approach 1: calendar (dict or DataFrame)
        cal = stock.calendar
        if cal is not None:
            if isinstance(cal, dict):
                dates = cal.get("Earnings Date") or []
                if dates:
                    d = dates[0]
                    return pd.Timestamp(d).date() if not isinstance(d, date) else d
            elif isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
                val = cal.loc["Earnings Date"].iloc[0]
                return pd.Timestamp(val).date()

        # Approach 2: earnings_dates (newer yfinance)
        ed = stock.earnings_dates
        if ed is not None and not ed.empty:
            today = date.today()
            future = ed[ed.index.date > today]
            if not future.empty:
                return future.index[-1].date()  # earliest future date
    except Exception as e:
        logger.debug(f"earnings date fetch failed for {ticker}: {e}")
    return None


# ── 预取基准数据 ───────────────────────────────────────────────────────────────

def _fetch_benchmarks() -> tuple[pd.Series, pd.Series]:
    """Return (qqq_ret, spy_ret) 5-day returns."""
    try:
        bench = yf.download(["QQQ", "SPY"], period="1mo",
                            auto_adjust=True, progress=False)
        close = bench["Close"]
        qqq_ret = float((close["QQQ"].iloc[-1] - close["QQQ"].iloc[-6]) / close["QQQ"].iloc[-6])
        spy_ret = float((close["SPY"].iloc[-1] - close["SPY"].iloc[-6]) / close["SPY"].iloc[-6])
        return qqq_ret, spy_ret
    except Exception as e:
        logger.warning(f"Benchmark fetch failed: {e}")
        return 0.0, 0.0


# ── 批量下载价格数据 ───────────────────────────────────────────────────────────

def _bulk_download(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Download 3-month OHLCV for all tickers at once, return per-ticker DataFrames."""
    if not tickers:
        return {}
    try:
        raw = yf.download(
            tickers, period="3mo",
            group_by="ticker", auto_adjust=True,
            progress=False, threads=True,
        )
        result: dict[str, pd.DataFrame] = {}

        if isinstance(raw.columns, pd.MultiIndex):
            for t in tickers:
                try:
                    df = raw[t].dropna(how="all")
                    if not df.empty:
                        result[t] = df
                except KeyError:
                    pass
        else:
            # Single ticker returned (shouldn't happen in bulk, but handle it)
            if len(tickers) == 1:
                result[tickers[0]] = raw.dropna(how="all")

        return result
    except Exception as e:
        logger.warning(f"Bulk download failed: {e}")
        return {}


# ── 单票 S0 扫描 ───────────────────────────────────────────────────────────────

def _scan_one(
    ticker: str,
    hist: pd.DataFrame,
    qqq_5d: float,
    spy_5d: float,
) -> dict | None:
    result = {
        "ticker":           ticker,
        "s0_1_momentum":    False,
        "s0_2_earnings":    False,
        "s0_3_volume":      False,
        "s0_4_short":       False,
        "excess_return_5d": None,
        "volume_ratio_3_20": None,
        "short_ratio":      None,
        "days_to_earnings": None,
        "earnings_date":    None,
    }

    if hist is None or len(hist) < 22:
        return None

    try:
        # S0-1 价格动量
        if len(hist) >= 6:
            ret_5d = float((hist["Close"].iloc[-1] - hist["Close"].iloc[-6]) / hist["Close"].iloc[-6])
            bench  = max(qqq_5d, spy_5d)
            excess = ret_5d - bench
            result["excess_return_5d"] = excess
            result["s0_1_momentum"]    = excess > S0_MOMENTUM_THRESHOLD

        # S0-3 成交放大
        if len(hist) >= 20:
            vol_3d  = hist["Volume"].iloc[-3:].mean()
            vol_20d = hist["Volume"].iloc[-20:].mean()
            ratio   = float(vol_3d / vol_20d) if vol_20d > 0 else 0.0
            result["volume_ratio_3_20"] = ratio
            result["s0_3_volume"]       = ratio > S0_VOLUME_RATIO

        # S0-2 财报事件（需要网络请求，放后面减少 rate-limit 压力）
        earn_date = _next_earnings_date(ticker)
        if earn_date:
            days = _trading_days_between(date.today(), earn_date)
            result["earnings_date"]    = earn_date
            result["days_to_earnings"] = days
            result["s0_2_earnings"]    = 0 < days <= S0_EARNINGS_WINDOW

        # S0-4 空头挤压
        try:
            info        = yf.Ticker(ticker).info
            short_ratio = info.get("shortRatio") or 0.0
            result["short_ratio"]   = float(short_ratio)
            result["s0_4_short"]    = float(short_ratio) > S0_SHORT_RATIO
        except Exception:
            pass

    except Exception as e:
        logger.debug(f"scan_one error {ticker}: {e}")
        return None

    result["passed"] = any([
        result["s0_1_momentum"],
        result["s0_2_earnings"],
        result["s0_3_volume"],
        result["s0_4_short"],
    ])
    return result


# ── 公开入口 ───────────────────────────────────────────────────────────────────

def run_module0(universe: dict[str, dict]) -> list[dict]:
    """Run the Module 0 scanner and return all passing stocks."""
    logger.info(f"Module 0 scanning {len(universe)} stocks…")

    qqq_ret, spy_ret = _fetch_benchmarks()
    logger.info(f"Benchmarks — QQQ 5d: {qqq_ret:.2%}, SPY 5d: {spy_ret:.2%}")

    tickers   = list(universe.keys())
    hist_map  = _bulk_download(tickers)
    logger.info(f"Bulk download returned data for {len(hist_map)}/{len(tickers)} tickers")

    passed: list[dict] = []

    def _worker(t: str) -> dict | None:
        return _scan_one(t, hist_map.get(t), qqq_ret, spy_ret)

    with ThreadPoolExecutor(max_workers=MODULE0_WORKERS) as ex:
        futures = {ex.submit(_worker, t): t for t in tickers}
        for fut in as_completed(futures):
            r = fut.result()
            if r and r["passed"]:
                passed.append(r)

    logger.info(f"Module 0 passed: {len(passed)} stocks")
    return passed
