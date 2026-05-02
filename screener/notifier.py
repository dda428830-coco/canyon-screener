"""
Telegram 推送模块
"""
import logging
import time
from datetime import datetime

import requests

from screener.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_POOL_EMOJI = {
    "buy":    "🟢 当前可买",
    "review": "🟡 强势复核",
    "watch":  "🔵 潜伏观察",
}


def _safe(val, fmt="{}", fallback="N/A") -> str:
    if val is None:
        return fallback
    try:
        return fmt.format(val)
    except Exception:
        return fallback


def format_stock_message(r: dict) -> str:
    pool_label = _POOL_EMOJI.get(r["classification"], "⚪ 其他")
    ticker     = r["ticker"]
    name       = r.get("name", ticker)
    c = r["c"]; e = r["e"]; m = r["m"]; f = r["f"]

    # 催化
    days = r.get("days_to_earnings")
    earn_str = f"{days}个交易日" if days is not None else "未知"
    c1_label = c.get("c1_label", "")

    # 错价
    fpe       = _safe(c.get("forward_pe"), "{:.1f}")
    ind_pe    = _safe(c.get("industry_pe"), "{}")
    discount  = c.get("pe_discount")
    pe_note   = c.get("pe_note")
    c2_label  = c.get("c2_label", "")
    c2_detail = c.get("c2_detail", "")
    if c2_label and c2_label != "无错价":
        pe_str = f"{c2_label}{c2_detail}"
    elif pe_note:
        pe_str = f"Forward PE {fpe}（{pe_note}，错价分=0）"
    elif discount is not None:
        pe_str = f"Forward PE {fpe} vs 行业中位数 {ind_pe}（折价{discount}%）"
    else:
        pe_str = f"Forward PE {fpe} vs 行业中位数 {ind_pe}"

    # 位置
    pullback = _safe(e.get("pullback_pct"), "{:.1f}%")

    # 动量
    excess   = _safe(m.get("excess_return_pct"), "{:+.2f}%")
    rr       = _safe(e.get("rr_ratio"), "{:.1f}")

    # 建议
    if r["classification"] == "buy":
        advice = "初始仓位 5-10% / 突破或回调买入"
    elif r["classification"] == "review":
        advice = "初始仓位 3-5% / 等待 E 评分确认"
    else:
        advice = "观察仓位 0-2% / 潜伏等待"

    sector = c.get("sector", "Unknown")
    c3_label = c.get("c3_label", "")

    lines = [
        f"{pool_label}",
        f"${ticker} — {name}",
        f"C: {c.get('total',0)}分 | E: {e.get('total',0)}分 | M: {m.get('total',0)}分 | F: {f.get('tier',3)}档",
        f"催化：财报在 {earn_str}后 ({c1_label})",
        f"错价：{pe_str}",
        f"位置：距20日高点回撤 {pullback}",
        f"动量：5日超额收益 {excess} | 盈亏比 {rr}",
        f"行业：{sector} ({c3_label})",
        f"建议：{advice}",
        "---",
    ]
    return "\n".join(lines)


def _send_raw(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)   # fallback to console when no credentials
        return True

    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}

    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                logger.warning(f"Telegram rate-limited, sleeping {retry_after}s")
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"Telegram send attempt {attempt+1} failed: {e}")
            time.sleep(2)
    return False


def send_summary(results_by_pool: dict[str, list[dict]]) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    buy_n    = len(results_by_pool.get("buy", []))
    review_n = len(results_by_pool.get("review", []))
    watch_n  = len(results_by_pool.get("watch", []))
    total    = buy_n + review_n + watch_n

    header = (
        f"📊 Canyon 筛选系统 — {today}\n"
        f"🟢 当前可买: {buy_n} 只\n"
        f"🟡 强势复核: {review_n} 只\n"
        f"🔵 潜伏观察: {watch_n} 只\n"
        f"{'─'*28}"
    )
    _send_raw(header)
    time.sleep(0.5)

    for pool in ("buy", "review", "watch"):
        for r in results_by_pool.get(pool, []):
            _send_raw(format_stock_message(r))
            time.sleep(0.3)   # stay under Telegram 30-msg/sec limit

    if total == 0:
        _send_raw(
            "今日 Canyon 扫描无符合条件的股票。\n"
            "可能原因：市场整体调整，或数据获取问题。\n"
            "建议保持观望，下个交易日再次扫描。"
        )


def send_error(msg: str) -> None:
    _send_raw(f"⚠️ Canyon 筛选系统错误\n{msg}")
