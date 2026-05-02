"""
第三层：Canyon C / E / M / F 评分系统
C — 催化评分（Catalyst）
E — 入场评分（Entry）
M — 动量评分（Momentum）
F — 基本面评分（Fundamentals）
"""
import logging
from datetime import date

import numpy as np
import pandas as pd
import yfinance as yf

from screener.config import (
    C_MID_DAYS, C_NEAR_DAYS,
    E_CROWD_THRESHOLD, E_PULLBACK_HIGH, E_PULLBACK_LOW,
    E_RR_GOOD, E_RR_OK,
    E_VOL_RATIO_HIGH, E_VOL_RATIO_LOW,
    INDUSTRY_MEDIAN_PE, LEAD_SECTORS,
    M_EXCESS_STRONG, M_VOL_AMP,
    SCORE_WORKERS,
)

logger = logging.getLogger(__name__)

# 行业 → SPDR 行业 ETF（周期错价用：判断行业景气度）
_SECTOR_ETF: dict[str, str] = {
    "Technology":             "XLK",
    "Healthcare":             "XLV",
    "Financials":             "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Staples":       "XLP",
    "Energy":                 "XLE",
    "Materials":              "XLB",
    "Industrials":            "XLI",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Communication Services": "XLC",
}

# 进程内缓存：每个 ETF 每次扫描只拉一次
_sector_etf_cache: dict[str, bool] = {}


def _sector_etf_positive(etf: str) -> bool:
    """True if the sector ETF's 1-month return is positive."""
    if etf in _sector_etf_cache:
        return _sector_etf_cache[etf]
    try:
        hist   = yf.Ticker(etf).history(period="1mo")
        result = (
            not hist.empty
            and len(hist) >= 2
            and float(hist["Close"].iloc[-1]) > float(hist["Close"].iloc[0])
        )
    except Exception:
        result = False
    _sector_etf_cache[etf] = result
    return result


# ── 工具函数 ───────────────────────────────────────────────────────────────────

def _atr(hist: pd.DataFrame, period: int = 14) -> float | None:
    try:
        h  = hist["High"].values
        lo = hist["Low"].values
        c  = hist["Close"].values
        tr = np.maximum(h[1:] - lo[1:],
             np.maximum(np.abs(h[1:] - c[:-1]),
                        np.abs(lo[1:] - c[:-1])))
        return float(np.mean(tr[-period:])) if len(tr) >= period else None
    except Exception:
        return None


# ── C 分（催化评分）────────────────────────────────────────────────────────────

def score_c(ticker: str, s0: dict, universe_data: dict) -> dict:
    d: dict = {}
    total = 0

    try:
        tk     = yf.Ticker(ticker)
        info   = tk.info
        sector = universe_data.get("sector") or info.get("sector") or "Unknown"

        # ── C1 催化距离 ────────────────────────────────────────────────────────
        days = s0.get("days_to_earnings")
        if days is not None:
            if days <= C_NEAR_DAYS:
                c1, c1_label = 3, f"近端 {days}日"
            elif days <= C_MID_DAYS:
                c1, c1_label = 2, f"中端 {days}日"
            else:
                c1, c1_label = 0, f"远端 {days}日"
        else:
            c1, c1_label = 0, "无财报日期"
        d["c1"] = c1; d["c1_label"] = c1_label
        total += c1

        # ── C2 复合错价评分（Canyon v2.2）──────────────────────────────────────
        #
        # 五种错价类型，各自独立判断：
        #   估值错价  2分  Forward PE < 行业中位数 × 0.8
        #   认知错价  2分  分析师目标价 vs 现价上涨空间 > 20%
        #   周期错价  2分  距52W低点 < 15% + 行业ETF近1月正收益
        #   结构错价  2分  市值 < $5B + 营收增速 > 20%
        #   流动性错价 1分  量能萎缩（s0量比 < 0.6）+ ROE > 0
        #
        # 得分 = max(各类型基础分) + 每个额外满足类型 × 0.5，上限4分（取整）

        price     = float(
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("previousClose")
            or 0
        )
        mktcap    = info.get("marketCap") or 0
        revg      = info.get("revenueGrowth")
        fpe       = info.get("forwardPE")
        tgt_price = info.get("targetMeanPrice")
        low52     = info.get("fiftyTwoWeekLow")
        roe       = info.get("returnOnEquity")
        median_pe = INDUSTRY_MEDIAN_PE.get(sector, INDUSTRY_MEDIAN_PE["Unknown"])

        # 向后兼容字段（notifier.py 使用）
        d["forward_pe"]  = round(fpe, 2) if fpe is not None else None
        d["industry_pe"] = median_pe
        d["pe_discount"] = None
        d["pe_note"]     = None
        if fpe is not None and fpe <= 0:
            d["pe_note"] = "PE负值/亏损"

        active:  list[str] = []   # 触发的错价类型名
        details: list[str] = []   # 对应的数值说明

        # 类型1：估值错价
        if fpe and fpe > 0 and median_pe > 0:
            discount = (median_pe - fpe) / median_pe
            d["pe_discount"] = round(discount * 100, 1)
            if discount > 0.20:
                active.append("估值错价")
                details.append(f"Forward PE折价{discount * 100:.0f}%")

        # 类型2：认知错价
        if price > 0 and tgt_price:
            upside = (tgt_price - price) / price
            d["analyst_upside_pct"] = round(upside * 100, 1)
            if upside > 0.20:
                active.append("认知错价")
                details.append(f"分析师目标价上涨{upside * 100:.0f}%")
        else:
            d["analyst_upside_pct"] = None

        # 类型3：周期错价
        if price > 0 and low52 and low52 > 0:
            near_low = (price - low52) / low52
            d["near_52w_low_pct"] = round(near_low * 100, 1)
            if near_low < 0.15:
                etf = _SECTOR_ETF.get(sector)
                if etf and _sector_etf_positive(etf):
                    active.append("周期错价")
                    details.append(f"距52周低点{near_low * 100:.0f}%")
        else:
            d["near_52w_low_pct"] = None

        # 类型4：结构错价
        if mktcap and mktcap < 5_000_000_000 and revg is not None and revg > 0.20:
            active.append("结构错价")
            details.append(f"营收增速{revg * 100:.0f}%")

        # 类型5：流动性错价（用 s0 量比作代理，避免重复拉取历史）
        vr_proxy = float(s0.get("volume_ratio_3_20") or 1.0)
        d["vol_compression_ratio"] = round(vr_proxy, 2)
        if vr_proxy < 0.6 and roe is not None and roe > 0:
            active.append("流动性错价")
            details.append(f"量比{vr_proxy:.2f}×")

        # 计分
        _base_pts = {"估值错价": 2, "认知错价": 2, "周期错价": 2, "结构错价": 2, "流动性错价": 1}
        if active:
            max_pts = max(_base_pts.get(tp, 1) for tp in active)
            c2_raw  = max_pts + (len(active) - 1) * 0.5
            c2      = min(4, int(c2_raw + 0.5))   # round-half-up, cap at 4
        else:
            c2 = 0

        d["c2"]        = c2
        d["c2_types"]  = active
        d["c2_label"]  = " + ".join(active) if active else "无错价"
        d["c2_detail"] = "（" + " + ".join(details) + "）" if details else ""
        total += c2

        # ── C3 映射纯度 ────────────────────────────────────────────────────────
        if sector in LEAD_SECTORS:
            c3, c3_label = 2, "主线行业"
        elif sector and sector != "Unknown":
            c3, c3_label = 1, "相关行业"
        else:
            c3, c3_label = 0, "未知行业"
        d["c3"] = c3; d["c3_label"] = c3_label; d["sector"] = sector
        total += c3

        # ── C4 催化可信度 ──────────────────────────────────────────────────────
        c4 = 2 if s0.get("earnings_date") else 0
        d["c4"] = c4
        total += c4

    except Exception as e:
        logger.debug(f"score_c error {ticker}: {e}")

    d["total"]  = total
    d["passed"] = total >= 5
    d["strong"] = total >= 7
    return d


# ── E 分（入场评分）────────────────────────────────────────────────────────────

def score_e(ticker: str, hist: pd.DataFrame) -> dict:
    d: dict = {}
    total = 0

    if hist is None or hist.empty or len(hist) < 20:
        d["total"] = 0; d["passed"] = False
        return d

    try:
        price  = float(hist["Close"].iloc[-1])
        high20 = float(hist["High"].iloc[-20:].max())

        # E1 位置（距20日高点回撤）
        pullback = (high20 - price) / high20 if high20 > 0 else 0.0
        d["pullback_pct"] = round(pullback * 100, 1)
        if E_PULLBACK_LOW <= pullback <= E_PULLBACK_HIGH:
            e1, e1_label = 2, "理想回撤区间"
        elif pullback < E_PULLBACK_LOW:
            e1, e1_label = 1, "接近高点"
        elif pullback <= 0.20:
            e1, e1_label = 1, "回撤适中"
        else:
            e1, e1_label = 0, "回撤过深"
        d["e1"] = e1; d["e1_label"] = e1_label
        total += e1

        # E2 盈亏比（ATR 估算）
        atr_val = _atr(hist, 14)
        d["atr14"] = round(atr_val, 3) if atr_val else None
        if atr_val and price > 0:
            rr = (atr_val * 2) / atr_val  # always 2 — kept as formula hook for future tuning
            # More meaningful: stop = 1×ATR, target = price*(1 + pullback/2 + ATR/price)
            potential = pullback * price / 2 + atr_val  # simplified upside
            risk      = atr_val
            rr        = potential / risk if risk > 0 else 0.0
            d["rr_ratio"] = round(rr, 2)
            e2 = 2 if rr >= E_RR_GOOD else (1 if rr >= E_RR_OK else 0)
        else:
            e2 = 0; d["rr_ratio"] = None
        d["e2"] = e2
        total += e2

        # E3 量价确认（3日/20日均量）
        vol3  = hist["Volume"].iloc[-3:].mean()
        vol20 = hist["Volume"].iloc[-20:].mean()
        vr    = float(vol3 / vol20) if vol20 > 0 else 0.0
        d["vol_ratio_3_20"] = round(vr, 2)
        e3 = 1 if E_VOL_RATIO_LOW <= vr <= E_VOL_RATIO_HIGH else 0
        d["e3"] = e3
        total += e3

        # E4 拥挤度（5日/60日均量）
        if len(hist) >= 60:
            vol5  = hist["Volume"].iloc[-5:].mean()
            vol60 = hist["Volume"].iloc[-60:].mean()
            cr    = float(vol5 / vol60) if vol60 > 0 else 0.0
            d["crowd_ratio"] = round(cr, 2)
            e4 = 1 if cr < E_CROWD_THRESHOLD else 0
        else:
            e4 = 1; d["crowd_ratio"] = None
        d["e4"] = e4
        total += e4

    except Exception as e:
        logger.debug(f"score_e error {ticker}: {e}")

    d["total"]  = total
    d["passed"] = total >= 5
    return d


# ── M 分（动量评分）────────────────────────────────────────────────────────────

def score_m(s0: dict) -> dict:
    d: dict = {}
    total = 0

    excess = s0.get("excess_return_5d") or 0.0
    vr     = s0.get("volume_ratio_3_20") or 0.0
    days   = s0.get("days_to_earnings")

    # M1 价格动量
    if excess > M_EXCESS_STRONG:
        m1 = 2
    elif excess > 0:
        m1 = 1
    else:
        m1 = 0
    d["m1"] = m1; d["excess_return_pct"] = round(excess * 100, 2)
    total += m1

    # M2 成交放大
    m2 = 1 if vr > M_VOL_AMP else 0
    d["m2"] = m2
    total += m2

    # M3 近端财报催化
    m3 = 1 if (days is not None and 0 < days <= 10) else 0
    d["m3"] = m3
    total += m3

    d["total"]  = total
    # Bug-fix: 负超额收益不能被判为强动量（即便 m2+m3 凑够2分）
    d["strong"] = total >= 2 and excess > 0
    return d


# ── F 分（基本面分层）──────────────────────────────────────────────────────────

def score_f(ticker: str) -> dict:
    d: dict = {}
    parts, count = 0, 0

    try:
        info = yf.Ticker(ticker).info

        def _add(val, thresholds: list[tuple[float, int]]):
            nonlocal parts, count
            if val is None:
                return
            count += 1
            for threshold, pts in thresholds:
                if val >= threshold:
                    parts += pts
                    return
            parts += 1

        d2e  = info.get("debtToEquity")
        roe  = info.get("returnOnEquity")
        revg = info.get("revenueGrowth")

        d["debt_to_equity"] = round(d2e * 1, 1) if d2e is not None else None
        d["roe_pct"]        = round(roe * 100, 1) if roe is not None else None
        d["rev_growth_pct"] = round(revg * 100, 1) if revg is not None else None

        # Debt/Equity: lower is better
        if d2e is not None:
            count += 1
            if d2e < 30:    parts += 5
            elif d2e < 60:  parts += 4
            elif d2e < 100: parts += 3
            elif d2e < 150: parts += 2
            else:            parts += 1

        _add(roe,  [(0.25, 5), (0.15, 4), (0.08, 3), (0.0, 2)])
        _add(revg, [(0.25, 5), (0.15, 4), (0.05, 3), (0.0, 2)])

    except Exception as e:
        logger.debug(f"score_f error {ticker}: {e}")

    tier = max(1, min(5, round(parts / count))) if count > 0 else 3
    d["tier"] = tier
    return d


# ── 分类逻辑 ───────────────────────────────────────────────────────────────────

def classify(c: dict, e: dict, m: dict) -> str:
    if c["passed"] and e["passed"]:
        return "buy"
    if m["strong"] and c["passed"] and not e["passed"]:
        return "review"
    if c["strong"] and not m["strong"]:
        return "watch"
    return "exclude"


# ── 公开入口 ───────────────────────────────────────────────────────────────────

def score_ticker(ticker: str, s0: dict, universe_data: dict, hist: pd.DataFrame) -> dict | None:
    try:
        c = score_c(ticker, s0, universe_data)
        e = score_e(ticker, hist)
        m = score_m(s0)
        f = score_f(ticker)

        cls = classify(c, e, m)
        return {
            "ticker":           ticker,
            "name":             universe_data.get("name", ticker),
            "c":                c,
            "e":                e,
            "m":                m,
            "f":                f,
            "classification":   cls,
            "days_to_earnings": s0.get("days_to_earnings"),
            "earnings_date":    s0.get("earnings_date"),
        }
    except Exception as ex:
        logger.error(f"score_ticker error {ticker}: {ex}")
        return None
