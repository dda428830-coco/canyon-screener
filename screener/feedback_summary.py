"""
周度反馈汇总

读取 data/feedback.json，分析过去4周的群组反馈，
生成 Telegram 汇总报告，并检测系统信号与用户行为的分歧。
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from screener.notifier import _send_raw

logger = logging.getLogger(__name__)

DATA_DIR      = Path(__file__).parent.parent / "data"
FEEDBACK_FILE = DATA_DIR / "feedback.json"


def _load_recent(weeks_back: int = 4) -> list[dict]:
    if not FEEDBACK_FILE.exists():
        return []
    try:
        all_entries: list[dict] = json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to read feedback.json: {e}")
        return []

    cutoff = (date.today() - timedelta(weeks=weeks_back)).isoformat()
    return [e for e in all_entries if e.get("date", "") >= cutoff]


def _format_summary(entries: list[dict]) -> str:
    today_str = date.today().isoformat()
    n = len(entries)

    if n == 0:
        return (
            f"📋 Canyon 反馈周报 — {today_str}\n"
            "过去4周暂无群组反馈记录。\n"
            "提示：向群组发送交易心得，系统将自动解析并收录。"
        )

    # 高频ticker
    ticker_counts = Counter(
        e["ticker"] for e in entries if e.get("ticker")
    )
    top_tickers = ticker_counts.most_common(5)

    # 信号质量统计
    quality_counts = Counter(e.get("signal_quality", "ok") for e in entries)

    # 行动统计
    action_counts = Counter(e.get("action", "unknown") for e in entries)

    # 结果统计
    result_counts = Counter(e.get("result", "unknown") for e in entries)

    # 分歧检测：跳过信号 ≥2 次的 ticker（系统推荐但用户跳过）
    skip_tickers = Counter(
        e["ticker"] for e in entries
        if e.get("action") == "skip" and e.get("ticker")
    )
    divergence_skips = [t for t, c in skip_tickers.items() if c >= 2]

    # 分歧检测：信号质量差但结果是盈利（lucky）
    lucky_entries = [
        e for e in entries
        if e.get("signal_quality") == "poor" and e.get("result") == "profit"
    ]

    lines = [
        f"📋 Canyon 反馈周报 — {today_str}",
        f"过去4周共收录反馈 {n} 条",
        "─" * 28,
        "",
        "📊 高频关注标的（Top 5）",
    ]
    if top_tickers:
        for t, c in top_tickers:
            lines.append(f"  ${t}: {c} 条反馈")
    else:
        lines.append("  暂无数据")

    lines += [
        "",
        "🎯 信号质量分布",
        f"  好信号: {quality_counts.get('good', 0)} 条",
        f"  一般:   {quality_counts.get('ok', 0)} 条",
        f"  差信号: {quality_counts.get('poor', 0)} 条",
        "",
        "⚡ 行动分布",
        f"  买入: {action_counts.get('buy', 0)} | 卖出: {action_counts.get('sell', 0)} | "
        f"跳过: {action_counts.get('skip', 0)} | 持有: {action_counts.get('hold', 0)}",
        "",
        "💰 结果分布",
        f"  盈利: {result_counts.get('profit', 0)} | 亏损: {result_counts.get('loss', 0)} | "
        f"持仓中: {result_counts.get('pending', 0)} | 未知: {result_counts.get('unknown', 0)}",
    ]

    if divergence_skips:
        lines += [
            "",
            "⚠️ 分歧信号（系统推荐但反复跳过）",
        ]
        for t in divergence_skips:
            lines.append(f"  ${t}: 被跳过 {skip_tickers[t]} 次")

    if lucky_entries:
        lines += [
            "",
            f"🍀 低质量信号但盈利（{len(lucky_entries)} 例）",
            "  建议复盘：运气还是规律？",
        ]

    lines += [
        "",
        "─" * 28,
        "💬 最新3条原始反馈",
    ]
    for e in entries[-3:]:
        ticker_str = f"${e['ticker']} " if e.get("ticker") else ""
        lines.append(
            f"  [{e.get('date','')}] {ticker_str}{e.get('action','?')} → "
            f"{e.get('result','?')} | {e.get('reason', '')[:40]}"
        )

    return "\n".join(lines)


def run_feedback_summary() -> str:
    entries = _load_recent(weeks_back=4)
    msg = _format_summary(entries)
    _send_raw(msg)
    logger.info(f"Feedback summary sent ({len(entries)} entries)")
    return msg


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    print(run_feedback_summary())
