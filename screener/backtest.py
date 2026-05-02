"""
Canyon Screener 周度回测 + v2.2 校准协议

流程：
  1. 批量下载过去6个月 OHLCV（一次请求）
  2. 并行缓存 info + earnings_dates（只做一次）
  3. 对每个历史交易日重放筛选漏斗（纯内存）
  4. 计算每笔入场的5日 forward return
  5. 统计绩效 → 推送回测报告
  6. 分类每笔信号（真成功/运气成功/纪律成功/真失败/惯性失败）
  7. 检测参数阈值漂移 → 推送校准建议
  8. 将校准建议以注释形式写入 config.py

已知数据局限（yfinance 免费数据）：
  • forwardPE / shortRatio 使用当前值近似（4周误差可接受）
  • 财报日期通过 stock.earnings_dates 精确还原历史节点
  • 宇宙池成分使用当前指数（4周变动可忽略）
"""
from __future__ import annotations

import logging
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from screener.canyon_score import classify
from screener.config import (
    C_MID_DAYS, C_NEAR_DAYS,
    C_PE_DISCOUNT_HIGH, C_PE_DISCOUNT_LOW,
    E_CROWD_THRESHOLD, E_PULLBACK_HIGH, E_PULLBACK_LOW,
    E_RR_GOOD, E_RR_OK, E_VOL_RATIO_HIGH, E_VOL_RATIO_LOW,
    INDUSTRY_MEDIAN_PE, LEAD_SECTORS,
    M_EXCESS_STRONG, M_VOL_AMP,
    S0_EARNINGS_WINDOW, S0_MOMENTUM_THRESHOLD,
    S0_SHORT_RATIO, S0_VOLUME_RATIO,
)
from screener.notifier import _send_raw, send_error
from screener.universe import get_ticker_list

logger = logging.getLogger(__name__)

# ── 工具 ──────────────────────────────────────────────────────────────────────

def _calc_atr(hist: pd.DataFrame, period: int = 14) -> float | None:
    try:
        h = hist["High"].values
        l = hist["Low"].values
        c = hist["Close"].values
        tr = np.maximum(h[1:] - l[1:],
             np.maximum(np.abs(h[1:] - c[:-1]),
                        np.abs(l[1:] - c[:-1])))
        return float(np.mean(tr[-period:])) if len(tr) >= period else None
    except Exception:
        return None


def _trading_dates_from_index(idx: pd.DatetimeIndex) -> list[date]:
    return sorted({d.date() for d in idx})


def _busdays_between(a: date, b: date) -> int:
    try:
        return max(0, int(np.busday_count(a, b)))
    except Exception:
        return max(0, (b - a).days)


# ── 数据加载 ───────────────────────────────────────────────────────────────────

def _bulk_download(tickers: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]:
    if not tickers:
        return {}
    logger.info(f"Bulk downloading {len(tickers)} tickers (period={period})…")
    try:
        raw = yf.download(
            tickers, period=period, group_by="ticker",
            auto_adjust=True, progress=False, threads=True,
        )
        result: dict[str, pd.DataFrame] = {}
        if isinstance(raw.columns, pd.MultiIndex):
            for t in tickers:
                try:
                    df = raw[t].dropna(how="all")
                    if not df.empty and len(df) >= 30:
                        result[t] = df
                except KeyError:
                    pass
        elif len(tickers) == 1:
            df = raw.dropna(how="all")
            if len(df) >= 30:
                result[tickers[0]] = df
        logger.info(f"Bulk download: {len(result)}/{len(tickers)}")
        return result
    except Exception as e:
        logger.error(f"Bulk download failed: {e}")
        return {}


def _fetch_one_info(ticker: str) -> tuple[str, dict]:
    for attempt in range(2):
        try:
            info = yf.Ticker(ticker).info
            return ticker, {
                "name":           info.get("shortName") or ticker,
                "sector":         info.get("sector") or "Unknown",
                "forwardPE":      info.get("forwardPE"),
                "shortRatio":     info.get("shortRatio"),
                "debtToEquity":   info.get("debtToEquity"),
                "returnOnEquity": info.get("returnOnEquity"),
                "revenueGrowth":  info.get("revenueGrowth"),
            }
        except Exception:
            if attempt == 0:
                time.sleep(1)
    return ticker, {"name": ticker, "sector": "Unknown"}


def _fetch_one_earnings(ticker: str) -> tuple[str, list[date]]:
    try:
        stock = yf.Ticker(ticker)
        ed = stock.earnings_dates
        if ed is not None and not ed.empty:
            return ticker, sorted([d.date() for d in ed.index])
        cal = stock.calendar
        if isinstance(cal, dict):
            raw = cal.get("Earnings Date") or []
            return ticker, sorted([pd.Timestamp(d).date() for d in raw if d])
    except Exception as e:
        logger.debug(f"earnings failed {ticker}: {e}")
    return ticker, []


def _build_caches(
    tickers: list[str], workers: int = 20
) -> tuple[dict[str, dict], dict[str, list[date]]]:
    info_cache: dict[str, dict] = {}
    earn_cache: dict[str, list[date]] = {}
    logger.info(f"Building info + earnings caches for {len(tickers)} tickers…")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        info_futs = {ex.submit(_fetch_one_info, t): t for t in tickers}
        earn_futs = {ex.submit(_fetch_one_earnings, t): t for t in tickers}
        for fut in as_completed(info_futs):
            t, info = fut.result(); info_cache[t] = info
        for fut in as_completed(earn_futs):
            t, dates = fut.result(); earn_cache[t] = dates
    logger.info(f"Caches built — info: {len(info_cache)}, earnings: {len(earn_cache)}")
    return info_cache, earn_cache


# ── 历史评分函数 ───────────────────────────────────────────────────────────────

def _hist_m0(
    ticker: str, hist_slice: pd.DataFrame,
    qqq_5d: float, spy_5d: float,
    earn_dates: list[date], info: dict, as_of: date,
) -> dict:
    r: dict = {
        "ticker": ticker,
        "s0_1": False, "s0_2": False, "s0_3": False, "s0_4": False,
        "excess_return_5d": None, "volume_ratio_3_20": None,
        "days_to_earnings": None, "earnings_date": None,
    }
    if len(hist_slice) < 22:
        r["passed"] = False; return r

    if len(hist_slice) >= 6:
        c = hist_slice["Close"]
        excess = float((c.iloc[-1] - c.iloc[-6]) / c.iloc[-6]) - max(qqq_5d, spy_5d)
        r["excess_return_5d"] = excess
        r["s0_1"] = excess > S0_MOMENTUM_THRESHOLD

    future_earn = [d for d in earn_dates if d > as_of]
    if future_earn:
        next_earn = min(future_earn)
        days = _busdays_between(as_of, next_earn)
        r["earnings_date"] = next_earn; r["days_to_earnings"] = days
        r["s0_2"] = 0 < days <= S0_EARNINGS_WINDOW
    elif earn_dates:
        r["days_to_earnings"] = None

    if len(hist_slice) >= 20:
        v3  = hist_slice["Volume"].iloc[-3:].mean()
        v20 = hist_slice["Volume"].iloc[-20:].mean()
        vr  = float(v3 / v20) if v20 > 0 else 0.0
        r["volume_ratio_3_20"] = vr
        r["s0_3"] = vr > S0_VOLUME_RATIO

    sr = float(info.get("shortRatio") or 0)
    r["s0_4"] = sr > S0_SHORT_RATIO
    r["passed"] = any([r["s0_1"], r["s0_2"], r["s0_3"], r["s0_4"]])
    return r


def _hist_score_c(s0: dict, info: dict) -> dict:
    d: dict = {}; total = 0
    sector = info.get("sector") or "Unknown"
    days = s0.get("days_to_earnings")
    if days is not None:
        if days <= C_NEAR_DAYS:   c1, c1l = 3, f"近端 {days}日"
        elif days <= C_MID_DAYS:  c1, c1l = 2, f"中端 {days}日"
        else:                      c1, c1l = 0, f"远端 {days}日"
    else:
        c1, c1l = 0, "无财报"
    d["c1"] = c1; d["c1_label"] = c1l; total += c1

    fpe    = info.get("forwardPE")
    med_pe = INDUSTRY_MEDIAN_PE.get(sector, INDUSTRY_MEDIAN_PE["Unknown"])
    d["forward_pe"]  = round(fpe, 2) if fpe is not None else None
    d["industry_pe"] = med_pe; d["pe_discount"] = None; d["pe_note"] = None
    if fpe is not None and fpe <= 0:
        c2 = 0; d["pe_note"] = "PE负值/亏损"
    elif fpe and fpe > 0 and med_pe > 0:
        disc = (med_pe - fpe) / med_pe
        d["pe_discount"] = round(disc * 100, 1)
        c2 = 2 if disc > C_PE_DISCOUNT_HIGH else (1 if disc > C_PE_DISCOUNT_LOW else 0)
    else:
        c2 = 0
    d["c2"] = c2; total += c2

    if sector in LEAD_SECTORS:   c3, c3l = 2, "主线行业"
    elif sector != "Unknown":    c3, c3l = 1, "相关行业"
    else:                         c3, c3l = 0, "未知"
    d["c3"] = c3; d["c3_label"] = c3l; d["sector"] = sector; total += c3

    c4 = 2 if s0.get("earnings_date") else 0
    d["c4"] = c4; total += c4
    d["total"] = total; d["passed"] = total >= 5; d["strong"] = total >= 7
    return d


def _hist_score_e(hist_slice: pd.DataFrame) -> dict:
    d: dict = {}; total = 0
    if len(hist_slice) < 20:
        return {"total": 0, "passed": False}
    try:
        price  = float(hist_slice["Close"].iloc[-1])
        high20 = float(hist_slice["High"].iloc[-20:].max())
        pb     = (high20 - price) / high20 if high20 > 0 else 0.0
        d["pullback_pct"] = round(pb * 100, 1)
        if E_PULLBACK_LOW <= pb <= E_PULLBACK_HIGH: e1, lbl = 2, "理想区间"
        elif pb < E_PULLBACK_LOW:                    e1, lbl = 1, "接近高点"
        elif pb <= 0.20:                              e1, lbl = 1, "回撤适中"
        else:                                         e1, lbl = 0, "回撤过深"
        d["e1"] = e1; d["e1_label"] = lbl; total += e1

        atr_val = _calc_atr(hist_slice, 14)
        d["atr14"] = round(atr_val, 3) if atr_val else None
        if atr_val and atr_val > 0:
            rr = (pb * price / 2 + atr_val) / atr_val
            d["rr_ratio"] = round(rr, 2)
            e2 = 2 if rr >= E_RR_GOOD else (1 if rr >= E_RR_OK else 0)
        else:
            e2 = 0; d["rr_ratio"] = None
        d["e2"] = e2; total += e2

        v3  = hist_slice["Volume"].iloc[-3:].mean()
        v20 = hist_slice["Volume"].iloc[-20:].mean()
        vr  = float(v3 / v20) if v20 > 0 else 0.0
        d["vol_ratio_3_20"] = round(vr, 2)
        e3 = 1 if E_VOL_RATIO_LOW <= vr <= E_VOL_RATIO_HIGH else 0
        d["e3"] = e3; total += e3

        if len(hist_slice) >= 60:
            v5  = hist_slice["Volume"].iloc[-5:].mean()
            v60 = hist_slice["Volume"].iloc[-60:].mean()
            cr  = float(v5 / v60) if v60 > 0 else 0.0
            d["crowd_ratio"] = round(cr, 2)
            e4 = 1 if cr < E_CROWD_THRESHOLD else 0
        else:
            e4 = 1; d["crowd_ratio"] = None
        d["e4"] = e4; total += e4
    except Exception as ex:
        logger.debug(f"_hist_score_e: {ex}")
    d["total"] = total; d["passed"] = total >= 5
    return d


def _hist_score_m(s0: dict) -> dict:
    d: dict = {}; total = 0
    excess = s0.get("excess_return_5d") or 0.0
    vr     = s0.get("volume_ratio_3_20") or 0.0
    days   = s0.get("days_to_earnings")
    m1 = 2 if excess > M_EXCESS_STRONG else (1 if excess > 0 else 0)
    d["m1"] = m1; d["excess_return_pct"] = round(excess * 100, 2); total += m1
    m2 = 1 if vr > M_VOL_AMP else 0
    d["m2"] = m2; total += m2
    m3 = 1 if (days is not None and 0 < days <= 10) else 0
    d["m3"] = m3; total += m3
    d["total"] = total; d["strong"] = total >= 2 and excess > 0
    return d


def _hist_score_f(info: dict) -> dict:
    parts = count = 0
    d2e  = info.get("debtToEquity")
    roe  = info.get("returnOnEquity")
    revg = info.get("revenueGrowth")
    if d2e is not None:
        count += 1
        if d2e < 30: parts += 5
        elif d2e < 60: parts += 4
        elif d2e < 100: parts += 3
        elif d2e < 150: parts += 2
        else: parts += 1

    def _rate(v, ths):
        nonlocal parts, count
        if v is None: return
        count += 1
        for t, p in ths:
            if v >= t: parts += p; return
        parts += 1
    _rate(roe,  [(0.25,5),(0.15,4),(0.08,3),(0.0,2)])
    _rate(revg, [(0.25,5),(0.15,4),(0.05,3),(0.0,2)])
    return {"tier": max(1, min(5, round(parts/count))) if count > 0 else 3}


# ── 单日历史筛选 ───────────────────────────────────────────────────────────────

def _hist_slice(hist: pd.DataFrame, as_of: date) -> pd.DataFrame | None:
    sl = hist.loc[hist.index.date <= as_of]
    return sl if len(sl) >= 22 else None


def _bench_ret5(hist: pd.DataFrame, as_of: date) -> float:
    sl = _hist_slice(hist, as_of)
    if sl is None or len(sl) < 6: return 0.0
    c = sl["Close"]
    return float((c.iloc[-1] - c.iloc[-6]) / c.iloc[-6])


def _screen_one_day(
    as_of: date,
    hist_map:   dict[str, pd.DataFrame],
    qqq_hist:   pd.DataFrame,
    spy_hist:   pd.DataFrame,
    info_cache: dict[str, dict],
    earn_cache: dict[str, list[date]],
) -> list[dict]:
    qqq5 = _bench_ret5(qqq_hist, as_of)
    spy5 = _bench_ret5(spy_hist, as_of)
    entries: list[dict] = []
    for ticker, hist in hist_map.items():
        sl = _hist_slice(hist, as_of)
        if sl is None: continue
        info  = info_cache.get(ticker, {"name": ticker, "sector": "Unknown"})
        earn  = earn_cache.get(ticker, [])
        s0    = _hist_m0(ticker, sl, qqq5, spy5, earn, info, as_of)
        if not s0["passed"]: continue
        c     = _hist_score_c(s0, info)
        e     = _hist_score_e(sl)
        m     = _hist_score_m(s0)
        f     = _hist_score_f(info)
        pool  = classify(c, e, m)
        if pool == "exclude": continue
        entries.append({
            "ticker":          ticker,
            "name":            info.get("name", ticker),
            "pool":            pool,
            "c":               c,
            "e":               e,
            "m":               m,
            "f":               f,
            "as_of":           as_of,
            "days_to_earnings": s0.get("days_to_earnings"),
        })
    return entries


# ── 收益计算 ───────────────────────────────────────────────────────────────────

def _forward_return(hist: pd.DataFrame, entry: date, days: int = 5) -> float | None:
    if hist is None or hist.empty: return None
    future = [d.date() for d in hist.index if d.date() > entry]
    if len(future) < days: return None
    exit_date = future[days - 1]
    try:
        ep = float(hist.loc[hist.index.date == entry,     "Close"].iloc[0])
        xp = float(hist.loc[hist.index.date == exit_date, "Close"].iloc[0])
        return (xp - ep) / ep if ep > 0 else None
    except Exception:
        return None


# ── 基础统计 ───────────────────────────────────────────────────────────────────

def _pool_stats(entries: list[dict]) -> dict:
    rets = [e["return_5d"] for e in entries if e.get("return_5d") is not None]
    if not rets:
        return {"count": 0, "avg_return": None, "win_rate": None,
                "max_loss": None, "wins": 0, "max_loss_ticker": ""}
    wins = sum(1 for r in rets if r > 0)
    return {
        "count":           len(rets),
        "avg_return":      float(np.mean(rets)),
        "win_rate":        wins / len(rets),
        "max_loss":        float(min(rets)),
        "max_loss_ticker": min(entries, key=lambda x: x.get("return_5d") or 0)["ticker"],
        "wins":            wins,
    }


def _failure_reason(entry: dict) -> str:
    e, c = entry["e"], entry["c"]
    if e.get("e4") == 0:   return "拥挤度超标"
    if e.get("e1") == 0:   return "入场位置不佳"
    if e.get("e2") == 0:   return "盈亏比不足"
    if c.get("c3") == 2:   return "主线板块转弱"
    if c.get("c2") == 0:   return "错价幅度不足"
    return "综合评分边界"


def _lucky_reason(entry: dict) -> str:
    e = entry["e"]
    if e.get("e4") == 0:       return "拥挤度超标但强势突破"
    if not e.get("passed"):    return "E评分未达标但动量持续"
    return "强势复核转强"


def _find_notable(
    buy_entries:    list[dict],
    review_entries: list[dict],
    max_fail: int = 5,
    max_lucky: int = 3,
) -> tuple[list[dict], list[dict]]:
    failures = sorted(
        [e for e in buy_entries if (e.get("return_5d") or 0) < -0.02],
        key=lambda x: x.get("return_5d") or 0,
    )[:max_fail]
    lucky = sorted(
        [e for e in review_entries if (e.get("return_5d") or 0) > 0.05],
        key=lambda x: -(x.get("return_5d") or 0),
    )[:max_lucky]
    for e in failures: e["reason"] = _failure_reason(e)
    for e in lucky:    e["reason"] = _lucky_reason(e)
    return failures, lucky


# ══════════════════════════════════════════════════════════════════════════════
# Canyon v2.2 校准协议
# ══════════════════════════════════════════════════════════════════════════════

# ── 惯性失败检测 ───────────────────────────────────────────────────────────────

def _check_inertia(
    entry:    dict,
    hist_map: dict[str, pd.DataFrame],
    qqq_hist: pd.DataFrame,
    spy_hist:  pd.DataFrame,
) -> bool:
    """
    检测"惯性失败"：入场时动量为正，但第3交易日后超额收益已转为显著负值，
    说明主线已转弱但系统未触发退出。
    条件：原始超额收益 > 0 AND 入场后第3日的超额收益 < -2%
    """
    if entry.get("return_5d", 0) is None or (entry.get("return_5d") or 0) >= 0:
        return False
    orig_excess = (entry["m"].get("excess_return_pct") or 0) / 100
    if orig_excess <= 0:
        return False

    ticker = entry["ticker"]
    as_of  = entry["as_of"]
    hist   = hist_map.get(ticker)
    if hist is None:
        return False

    future = [d.date() for d in hist.index if d.date() > as_of]
    if len(future) < 3:
        return False

    day3 = future[2]

    def _ret5_at(h: pd.DataFrame, ref: date) -> float:
        sl = h.loc[h.index.date <= ref]
        if len(sl) < 6: return 0.0
        c = sl["Close"]
        return float((c.iloc[-1] - c.iloc[-6]) / c.iloc[-6])

    stock_ret3 = _ret5_at(hist, day3)
    qqq_ret3   = _ret5_at(qqq_hist, day3)
    spy_ret3   = _ret5_at(spy_hist, day3)
    excess_day3 = stock_ret3 - max(qqq_ret3, spy_ret3)

    return excess_day3 < -0.02  # 超额收益在第3日已低于 -2%


# ── 样本分类 ───────────────────────────────────────────────────────────────────

_SAMPLE_TYPES = ("true_success", "discipline_success", "lucky_success",
                 "true_failure", "inertia_failure", "unknown")


def _classify_sample(entry: dict) -> str:
    """
    真成功:   C/E/M 全部强达标（≥6/≥6/≥2）+ 正收益
    纪律成功: 规则恰好达标（C=5 or E=5）+ 正收益（未偷步）
    运气成功: E<5 或拥挤度≥2x 但结果正收益
    惯性失败: 入场时动量为正，第3日后超额已转负，系统未退出
    真失败:   规则错误 + 负收益（非惯性）
    """
    ret     = entry.get("return_5d")
    if ret is None: return "unknown"

    c, e, m = entry["c"], entry["e"], entry["m"]
    pool    = entry["pool"]

    # 运气成功判断条件
    e_weak    = not e.get("passed", False)
    crowd_bad = (e.get("crowd_ratio") or 0.0) >= 2.0
    has_defect = e_weak or crowd_bad

    all_strong = (
        pool == "buy"
        and c.get("total", 0) >= 6
        and e.get("total", 0) >= 6
        and m.get("total", 0) >= 2
    )
    rule_met = pool == "buy" and c.get("passed", False) and e.get("passed", False)

    if ret > 0:
        if has_defect:  return "lucky_success"
        if all_strong:  return "true_success"
        if rule_met:    return "discipline_success"
        return "lucky_success"  # review/watch 池正收益

    if ret < 0:
        return "inertia_failure" if entry.get("_inertia") else "true_failure"

    return "unknown"


def _tag_inertia_and_classify(
    all_entries: list[dict],
    hist_map:    dict[str, pd.DataFrame],
    qqq_hist:    pd.DataFrame,
    spy_hist:    pd.DataFrame,
) -> None:
    """原地修改 all_entries，添加 _inertia 标记和 sample_type 字段。"""
    for entry in all_entries:
        entry["_inertia"] = _check_inertia(entry, hist_map, qqq_hist, spy_hist)
        entry["sample_type"] = _classify_sample(entry)


def _count_sample_types(all_entries: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {t: 0 for t in _SAMPLE_TYPES}
    for e in all_entries:
        st = e.get("sample_type", "unknown")
        counts[st] = counts.get(st, 0) + 1
    return counts


# ── 参数阈值漂移检测 ───────────────────────────────────────────────────────────

def _calib_stat(entries: list[dict], qqq_avg: float | None) -> dict:
    """基础统计辅助：给定一组 entry，返回 avg_return / win_rate / loss_rate。"""
    rets = [e["return_5d"] for e in entries if e.get("return_5d") is not None]
    if not rets:
        return {"n": 0, "avg": None, "win_rate": None, "loss_rate": None}
    wins  = sum(1 for r in rets if r > 0)
    losses= sum(1 for r in rets if r < 0)
    return {
        "n":         len(rets),
        "avg":       float(np.mean(rets)),
        "win_rate":  wins  / len(rets),
        "loss_rate": losses / len(rets),
    }


def _calibrate_params(
    all_entries: list[dict],
    qqq_avg:     float | None,
) -> list[dict]:
    """
    对4个关键阈值的边界区间进行数据驱动检测，返回校准建议列表。
    每条建议格式：
      { param, current, suggested, action: "up"|"down"|"maintain",
        detail, n, var_name }
    """
    results: list[dict] = []
    entries_w = [e for e in all_entries if e.get("return_5d") is not None]
    if not entries_w:
        return results

    # ── 1. C分达标阈值（当前=5）─────────────────────────────────────────────
    # 边界：C total 恰好==5 的入场
    c_border = [e for e in entries_w if e["c"].get("total", 0) == 5]
    if c_border:
        s = _calib_stat(c_border, qqq_avg)
        if s["avg"] is not None and s["avg"] < 0:
            action, sug = "up", 6
            detail = f"C=5样本均收益 {s['avg']*100:+.1f}%<0，胜率 {s['win_rate']*100:.0f}%"
        else:
            action, sug = "maintain", 5
            detail = f"C=5样本胜率 {(s['win_rate'] or 0)*100:.0f}%，均收益 {(s['avg'] or 0)*100:+.1f}%"
        results.append({
            "param": "C分达标阈值", "var_name": "SUGGESTED_C_THRESHOLD",
            "current": 5, "suggested": sug,
            "action": action, "detail": detail, "n": s["n"],
        })

    # ── 2. 盈亏比阈值 E_RR_OK（当前=1.5:1）──────────────────────────────────
    # 边界：rr_ratio 在 [1.5, 2.0)
    rr_border = [e for e in entries_w
                 if 1.5 <= (e["e"].get("rr_ratio") or 0.0) < 2.0]
    if rr_border:
        s = _calib_stat(rr_border, qqq_avg)
        if s["win_rate"] is not None and s["win_rate"] < 0.5:
            action, sug = "up", 2.0
            detail = f"1.5-2区间胜率 {s['win_rate']*100:.0f}%<50%"
        else:
            action, sug = "maintain", 1.5
            detail = f"1.5-2区间胜率 {(s['win_rate'] or 0)*100:.0f}%"
        results.append({
            "param": "盈亏比阈值(E_RR_OK)", "var_name": "SUGGESTED_E_RR_OK",
            "current": 1.5, "suggested": sug,
            "action": action, "detail": detail, "n": s["n"],
        })

    # ── 3. 成交放大倍数 S0_VOLUME_RATIO（当前=1.5x）─────────────────────────
    # 边界：vol_ratio_3_20 在 [1.5, 2.0)
    vol_border = [e for e in entries_w
                  if 1.5 <= (e["e"].get("vol_ratio_3_20") or 0.0) < 2.0]
    if vol_border:
        s = _calib_stat(vol_border, qqq_avg)
        qqq = qqq_avg or 0.0
        if s["avg"] is not None and s["avg"] < qqq:
            action, sug = "up", 2.0
            detail = f"1.5-2x区间均收益 {s['avg']*100:+.1f}% 跑输 QQQ {qqq*100:+.1f}%"
        else:
            action, sug = "maintain", 1.5
            detail = f"1.5-2x区间均收益 {(s['avg'] or 0)*100:+.1f}%"
        results.append({
            "param": "成交放大阈值(S0_VOLUME_RATIO)", "var_name": "SUGGESTED_S0_VOLUME_RATIO",
            "current": 1.5, "suggested": sug,
            "action": action, "detail": detail, "n": s["n"],
        })

    # ── 4. 拥挤度阈值 E_CROWD_THRESHOLD（当前=2x）───────────────────────────
    # 边界：crowd_ratio 在 [1.5, 2.0)（当前"通过"的部分）
    crowd_border = [e for e in entries_w
                    if 1.5 <= (e["e"].get("crowd_ratio") or 0.0) < 2.0]
    if crowd_border:
        s = _calib_stat(crowd_border, qqq_avg)
        if s["loss_rate"] is not None and s["loss_rate"] > 0.5:
            action, sug = "down", 1.5
            detail = f"1.5-2x区间亏损率 {s['loss_rate']*100:.0f}%>50%"
        else:
            action, sug = "maintain", 2.0
            detail = f"1.5-2x区间亏损率 {(s['loss_rate'] or 0)*100:.0f}%"
        results.append({
            "param": "拥挤度阈值(E_CROWD_THRESHOLD)", "var_name": "SUGGESTED_E_CROWD_THRESHOLD",
            "current": 2.0, "suggested": sug,
            "action": action, "detail": detail, "n": s["n"],
        })

    return results


# ── 将校准建议写回 config.py ───────────────────────────────────────────────────

_CALIB_MARKER = "# === 上周校准建议（待确认）==="


def _update_config_calibration(
    suggestions: list[dict],
    week_label:  str,
) -> None:
    """
    在 config.py 末尾追加校准建议注释。
    每次调用会替换上一次写入的块，不会累积重复。
    """
    config_path = Path(__file__).parent / "config.py"
    try:
        text = config_path.read_text(encoding="utf-8")

        # 移除旧的校准块
        if _CALIB_MARKER in text:
            text = text[:text.index(_CALIB_MARKER)].rstrip() + "\n"

        lines = [
            "",
            _CALIB_MARKER,
            f'# CALIBRATION_DATE = "{week_label}"',
        ]

        adjustments = [s for s in suggestions if s["action"] != "maintain"]
        if adjustments:
            for s in adjustments:
                direction = "上调" if s["action"] == "up" else "下调"
                lines.append(
                    f"# {s['var_name']} = {s['suggested']}"
                    f"  # 原值 {s['current']}，建议{direction}，原因: {s['detail']}"
                )
            lines += ["#", "# 下周建议执行口径："]
            for s in adjustments:
                direction = "上调" if s["action"] == "up" else "下调"
                lines.append(f"#   {s['param']}: {direction}至 {s['suggested']}（{s['detail']}）")
        else:
            lines.append("#   所有参数维持当前值，无需调整")

        lines.append("# ==============================")

        config_path.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")
        logger.info(f"Config calibration block updated ({len(adjustments)} suggestions)")
    except Exception as e:
        logger.warning(f"Failed to update config calibration: {e}")


# ── 校准报告格式化 ─────────────────────────────────────────────────────────────

def format_calibration(
    sample_counts: dict[str, int],
    suggestions:   list[dict],
    failure_samples: list[dict],
    week_label:    str,
    total_n:       int,
) -> str:
    SEP = "━" * 17
    adjustments = [s for s in suggestions if s["action"] != "maintain"]

    lines = [
        "",
        "🔧 系统校准建议",
        SEP,
        "双盲一致率: 本周无法测（单实例运行）",
        "",
        f"样本分类（过去4周共 {total_n} 笔）：",
        (
            f"真成功: {sample_counts.get('true_success', 0)}笔  |  "
            f"纪律成功: {sample_counts.get('discipline_success', 0)}笔  |  "
            f"运气成功: {sample_counts.get('lucky_success', 0)}笔"
        ),
        (
            f"真失败: {sample_counts.get('true_failure', 0)}笔  |  "
            f"惯性失败: {sample_counts.get('inertia_failure', 0)}笔"
        ),
        "",
        "参数校准：",
    ]

    if suggestions:
        for s in suggestions:
            icon   = "✅" if s["action"] == "maintain" else "⚠️"
            n_note = f"（{s['n']}样本）" if s.get("n") else ""
            if s["action"] == "maintain":
                lines.append(f"  {icon} {s['param']}={s['current']}：维持（{s['detail']}）{n_note}")
            elif s["action"] == "up":
                lines.append(
                    f"  {icon} {s['param']}：建议从 {s['current']} 上调至 {s['suggested']}"
                    f"（{s['detail']}）{n_note}"
                )
            else:
                lines.append(
                    f"  {icon} {s['param']}：建议从 {s['current']} 下调至 {s['suggested']}"
                    f"（{s['detail']}）{n_note}"
                )
    else:
        lines.append("  暂无足够样本支撑校准分析")

    # 失败样本库
    new_failures = [e for e in failure_samples
                    if e.get("sample_type") in ("true_failure", "inertia_failure")]
    if new_failures:
        lines += ["", f"失败样本库新增（{len(new_failures)}笔）："]
        for e in new_failures:
            err_type = "纪律错误" if e.get("sample_type") == "inertia_failure" else "系统错误"
            reason   = e.get("reason", "综合判断失误")
            lines.append(f"  ${e['ticker']} — {reason} — {err_type}")

    # 下周调整口径
    if adjustments:
        lines += ["", "下周执行口径调整："]
        for s in adjustments:
            direction = "上调" if s["action"] == "up" else "下调"
            lines.append(f"  - {s['param']}{direction}至 {s['suggested']}（{s['detail']}）")
    else:
        lines += ["", "下周执行口径：维持当前参数，无需调整"]

    lines.append(SEP)
    return "\n".join(lines)


# ── 主回测报告格式化（不变）──────────────────────────────────────────────────

def _fmt_ret(r: float | None) -> str:
    return "N/A" if r is None else f"{r * 100:+.1f}%"


def _fmt_pct(r: float | None) -> str:
    return "N/A" if r is None else f"{r * 100:.0f}%"


def _iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def format_report(
    buy_stats:    dict,
    review_stats: dict,
    watch_stats:  dict,
    qqq_avg:      float | None,
    failures:     list[dict],
    lucky:        list[dict],
    week_label:   str,
    n_days:       int,
    n_buy_in_rule:  int,
    n_buy_out_rule: int,
    in_rule_avg:    float | None,
    out_rule_avg:   float | None,
) -> str:
    SEP = "━" * 17
    lines = [
        f"📊 Canyon 周度回测报告 {week_label}",
        SEP,
        f"回测周期: 过去4周（{n_days}个交易日）",
        "",
        "🟢 当前可买池",
    ]
    if buy_stats["count"] > 0:
        qqq_str = f" vs QQQ {_fmt_ret(qqq_avg)}" if qqq_avg is not None else ""
        lines += [
            f"  样本量:     {buy_stats['count']} 次入场",
            f"  平均5日收益: {_fmt_ret(buy_stats['avg_return'])}{qqq_str}",
            f"  胜率:       {_fmt_pct(buy_stats['win_rate'])} ({buy_stats['wins']}/{buy_stats['count']})",
            f"  最大亏损:   {_fmt_ret(buy_stats['max_loss'])} (${buy_stats.get('max_loss_ticker','')})",
        ]
    else:
        lines.append("  本期无入场信号")

    lines += ["", "🟡 强势复核池"]
    if review_stats["count"] > 0:
        lines += [
            f"  样本量:     {review_stats['count']} 次入场",
            f"  平均5日收益: {_fmt_ret(review_stats['avg_return'])}",
            f"  胜率:       {_fmt_pct(review_stats['win_rate'])}",
        ]
    else:
        lines.append("  本期无信号")

    lines += ["", "🔵 潜伏观察池"]
    if watch_stats["count"] > 0:
        lines += [
            f"  样本量:     {watch_stats['count']} 次",
            f"  平均5日收益: {_fmt_ret(watch_stats['avg_return'])}",
        ]
    else:
        lines.append("  本期无信号")

    if n_buy_in_rule + n_buy_out_rule > 0:
        lines += [
            "", "📐 规则严守 vs 宽松对比（仅可买池）",
            f"  C+E严格达标: {n_buy_in_rule} 只  均收益 {_fmt_ret(in_rule_avg)}",
            f"  边界入场:     {n_buy_out_rule} 只  均收益 {_fmt_ret(out_rule_avg)}",
        ]
    if failures:
        lines += ["", f"❌ 失败样本（{len(failures)}只，亏损>2%）"]
        for f in failures:
            lines.append(f"  ${f['ticker']} — {f['reason']}, {_fmt_ret(f.get('return_5d'))}")
    if lucky:
        lines += ["", f"⚡ 漏网强势股（复核池，涨幅>5%）"]
        for lk in lucky:
            lines.append(f"  ${lk['ticker']} — {lk['reason']}, {_fmt_ret(lk.get('return_5d'))}")
    lines.append(SEP)
    return "\n".join(lines)


# ── 主入口 ─────────────────────────────────────────────────────────────────────

def run_backtest() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger.info("=" * 50)
    logger.info("Canyon Backtest + Calibration starting")
    logger.info("=" * 50)

    try:
        # ── 1. 数据下载 ──────────────────────────────────────────────────────
        all_tickers = get_ticker_list()
        bench_hist  = _bulk_download(["QQQ", "SPY"], period="6mo")
        qqq_hist    = bench_hist.get("QQQ")
        spy_hist    = bench_hist.get("SPY")
        if qqq_hist is None or spy_hist is None:
            raise RuntimeError("无法下载 QQQ/SPY 基准数据")

        hist_map = _bulk_download(all_tickers, period="6mo")
        valid    = list(hist_map.keys())
        logger.info(f"Valid tickers: {len(valid)}")

        # ── 2. 缓存 info + earnings ──────────────────────────────────────────
        info_cache, earn_cache = _build_caches(valid, workers=20)

        # ── 3. 回测日期 ──────────────────────────────────────────────────────
        today      = date.today()
        end_date   = today - timedelta(days=8)
        start_date = today - timedelta(days=35)
        all_td     = _trading_dates_from_index(qqq_hist.index)
        bt_dates   = [d for d in all_td if start_date <= d <= end_date]
        if not bt_dates:
            raise RuntimeError("回测日期范围内无交易日数据")
        logger.info(f"Backtest: {len(bt_dates)} days ({bt_dates[0]} → {bt_dates[-1]})")

        # ── 4. 逐日筛选 ──────────────────────────────────────────────────────
        all_entries: list[dict] = []
        for d in bt_dates:
            all_entries.extend(
                _screen_one_day(d, hist_map, qqq_hist, spy_hist, info_cache, earn_cache)
            )
        logger.info(f"Total screened entries: {len(all_entries)}")

        # ── 5. Forward return ─────────────────────────────────────────────────
        for entry in all_entries:
            t = entry["ticker"]; d = entry["as_of"]
            entry["return_5d"]     = _forward_return(hist_map.get(t), d, 5)
            entry["qqq_return_5d"] = _forward_return(qqq_hist, d, 5)

        # ── 6. 样本分类（校准协议第一步）────────────────────────────────────
        _tag_inertia_and_classify(all_entries, hist_map, qqq_hist, spy_hist)
        sample_counts = _count_sample_types(all_entries)
        logger.info(f"Sample types: {sample_counts}")

        # ── 7. 分组统计 ──────────────────────────────────────────────────────
        buy_entries    = [e for e in all_entries if e["pool"] == "buy"]
        review_entries = [e for e in all_entries if e["pool"] == "review"]
        watch_entries  = [e for e in all_entries if e["pool"] == "watch"]

        buy_stats    = _pool_stats(buy_entries)
        review_stats = _pool_stats(review_entries)
        watch_stats  = _pool_stats(watch_entries)

        qqq_rets = [e["qqq_return_5d"] for e in buy_entries if e.get("qqq_return_5d") is not None]
        qqq_avg  = float(np.mean(qqq_rets)) if qqq_rets else None

        strict       = [e for e in buy_entries if e["c"].get("total",0)>=6 and e["e"].get("total",0)>=6]
        border       = [e for e in buy_entries if e not in strict]
        in_rule_avg  = float(np.mean([e["return_5d"] for e in strict  if e.get("return_5d") is not None])) if strict  else None
        out_rule_avg = float(np.mean([e["return_5d"] for e in border  if e.get("return_5d") is not None])) if border  else None

        failures, lucky = _find_notable(buy_entries, review_entries)

        # ── 8. 参数校准分析（校准协议第二步）────────────────────────────────
        week_label   = _iso_week(today)
        suggestions  = _calibrate_params(all_entries, qqq_avg)
        logger.info(f"Calibration suggestions: {[(s['param'], s['action']) for s in suggestions]}")

        # ── 9. 将校准建议写入 config.py ──────────────────────────────────────
        _update_config_calibration(suggestions, week_label)

        # ── 10. 推送主回测报告 ────────────────────────────────────────────────
        main_report = format_report(
            buy_stats, review_stats, watch_stats,
            qqq_avg, failures, lucky,
            week_label, len(bt_dates),
            len(strict), len(border),
            in_rule_avg, out_rule_avg,
        )
        logger.info(f"\n{main_report}")
        _send_raw(main_report)

        # ── 11. 推送校准报告（追加在主报告之后）─────────────────────────────
        calib_report = format_calibration(
            sample_counts, suggestions,
            failures,        # 把 buy pool 失败样本传入做归因
            week_label, len(all_entries),
        )
        logger.info(f"\n{calib_report}")
        _send_raw(calib_report)

        # ── 12. 反馈汇总（非致命）────────────────────────────────────────────
        try:
            from screener.feedback_summary import run_feedback_summary
            run_feedback_summary()
        except Exception as _fb_err:
            logger.warning(f"Feedback summary failed (non-fatal): {_fb_err}")

        logger.info("=== Canyon Backtest done ===")

    except Exception:
        tb = traceback.format_exc()
        logger.error(f"Backtest fatal:\n{tb}")
        send_error(f"回测异常终止:\n{tb[-800:]}")
        sys.exit(1)


if __name__ == "__main__":
    run_backtest()
