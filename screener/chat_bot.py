"""
Canyon Chat Bot — 群组智能助手（本地长驻运行）

用法：
  python screener/chat_bot.py

依赖环境变量（与 feedback_bot 共用）：
  TELEGRAM_BOT_TOKEN  Bot Token
  GROUP_CHAT_ID       目标群组 Chat ID（负整数字符串）
  CLAUDE_API_KEY      Anthropic API Key
  ALLOWED_USER_IDS    留空=所有人；否则读 config.py 里的列表

Ctrl+C 退出。
"""
from __future__ import annotations

import json
import logging
import re

import anthropic
import pandas as pd
import yfinance as yf
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from screener.canyon_score import classify, score_c, score_e, score_f, score_m
from screener.config import (
    ALLOWED_USER_IDS,
    CLAUDE_API_KEY,
    GROUP_CHAT_ID,
    TELEGRAM_BOT_TOKEN,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
你是一个交易群的智能助手，专注美股和 Canyon 分析法。
判断以下消息是否需要你回复。

需要回复的情况：
- 提到具体股票代码或公司名
- 问市场/行业/宏观相关问题
- 问 Canyon 评分/分析相关
- 直接提问句式

不需要回复的情况：
- 纯闲聊（今天吃什么、天气好）
- 表情包/语气词（哈哈、好的、收到）
- 成员之间的私人对话
- 已经有人回答了的问题

回复风格要求：
- 中文，简洁直接，3-5行
- 不要暴露自己是 AI，像群里懂交易的朋友自然说话
- 安静比聒噪好，宁可不回也别废话

返回 JSON：
{
  "should_reply": true/false,
  "reason": "简短说明",
  "reply": "如果 should_reply=true，这里是回复内容（中文，简洁3-5行）"
}

只返回 JSON，不要其他任何文字。
"""


# ── 工具函数 ───────────────────────────────────────────────────────────────────

def _extract_tickers(text: str) -> list[str]:
    """Extract $TICKER patterns from message text."""
    found = re.findall(r'\$([A-Za-z]{1,5})', text)
    return list(dict.fromkeys(t.upper() for t in found))


def _needs_canyon(text: str) -> bool:
    keywords = ["canyon", "c分", "e分", "m分", "f分", "评分", "打分", "几分", "分析下", "分析一下"]
    tl = text.lower()
    return any(k in tl for k in keywords)


def _quick_info(ticker: str) -> str:
    """Fetch current price + basic stats for a ticker."""
    try:
        t    = yf.Ticker(ticker)
        info = t.info
        hist = t.history(period="5d")

        price = float(hist["Close"].iloc[-1]) if not hist.empty else None
        prev  = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
        chg   = (price - prev) / prev * 100 if price and prev else None

        lines = [f"${ticker} ({info.get('shortName') or ticker})"]
        if price:
            chg_str = f" ({chg:+.1f}%)" if chg is not None else ""
            lines.append(f"  现价: ${price:.2f}{chg_str}")
        mktcap = info.get("marketCap")
        if mktcap:
            lines.append(f"  市值: ${mktcap / 1e9:.1f}B")
        pe = info.get("trailingPE") or info.get("forwardPE")
        if pe:
            lines.append(f"  PE: {pe:.1f}")
        lo, hi = info.get("fiftyTwoWeekLow"), info.get("fiftyTwoWeekHigh")
        if lo and hi:
            lines.append(f"  52W: ${lo:.2f} – ${hi:.2f}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"quick_info {ticker}: {e}")
        return f"${ticker}: 数据获取失败"


def _canyon_score_text(ticker: str) -> str:
    """Run Canyon C/E/M/F scoring and return a short text summary."""
    try:
        t    = yf.Ticker(ticker)
        info = t.info
        hist = t.history(period="3mo")

        if hist.empty or len(hist) < 20:
            return f"${ticker}: 历史数据不足，无法评分"

        # Compute excess_return_5d vs QQQ
        price    = float(hist["Close"].iloc[-1])
        price_5d = float(hist["Close"].iloc[-6]) if len(hist) >= 6 else price
        ret_5d   = (price - price_5d) / price_5d if price_5d > 0 else 0.0
        try:
            bench     = yf.Ticker("QQQ").history(period="10d")
            b_now     = float(bench["Close"].iloc[-1])
            b_5d      = float(bench["Close"].iloc[-6]) if len(bench) >= 6 else b_now
            bench_ret = (b_now - b_5d) / b_5d if b_5d > 0 else 0.0
            excess    = ret_5d - bench_ret
        except Exception:
            excess = ret_5d

        vol3  = hist["Volume"].iloc[-3:].mean()
        vol20 = hist["Volume"].iloc[-20:].mean()
        vr    = float(vol3 / vol20) if vol20 > 0 else 1.0

        # Days to earnings
        days_to_earn: int | None = None
        try:
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                today  = pd.Timestamp.now(tz="UTC").normalize()
                future = ed.index[ed.index > today]
                if len(future) > 0:
                    days_to_earn = max(0, (future[0] - today).days)
        except Exception:
            pass

        s0 = {
            "excess_return_5d":  excess,
            "volume_ratio_3_20": vr,
            "days_to_earnings":  days_to_earn,
            "earnings_date":     days_to_earn is not None,
        }
        universe_data = {
            "sector": info.get("sector", "Unknown"),
            "name":   info.get("shortName", ticker),
        }

        c   = score_c(ticker, s0, universe_data)
        e   = score_e(ticker, hist)
        m   = score_m(s0)
        f   = score_f(ticker)
        cls = classify(c, e, m)

        emoji    = {"buy": "🟢", "review": "🟡", "watch": "🔵", "exclude": "⚪"}.get(cls, "⚪")
        earn_str = f"{days_to_earn}个交易日" if days_to_earn is not None else "未知"

        return (
            f"${ticker} Canyon 评分\n"
            f"C={c['total']} E={e['total']} M={m['total']} F={f['tier']}档 → {emoji} {cls}\n"
            f"财报距今: {earn_str} | 超额收益5日: {excess * 100:+.1f}%"
        )
    except Exception as e:
        logger.warning(f"canyon_score {ticker}: {e}")
        return f"${ticker}: 评分失败（{e}）"


# ── Claude 决策 ────────────────────────────────────────────────────────────────

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic | None:
    global _client
    if _client is None and CLAUDE_API_KEY:
        _client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    return _client


def _ask_claude(enriched: str) -> dict | None:
    c = _get_client()
    if not c:
        return None
    try:
        resp = c.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": enriched}],
        )
        text = resp.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logger.warning(f"Claude call failed: {e}")
        return None


# ── Telegram 消息处理 ──────────────────────────────────────────────────────────

async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text:
        return

    # Group filter
    if GROUP_CHAT_ID and str(msg.chat.id) != str(GROUP_CHAT_ID):
        return

    # Whitelist filter
    user    = msg.from_user
    user_id = user.id if user else 0
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return

    text    = msg.text
    tickers = _extract_tickers(text)

    # Build enriched context for Claude
    parts: list[str] = [text]

    if tickers:
        stock_ctx = "\n".join(_quick_info(tk) for tk in tickers[:3])
        parts.append(f"[实时行情]\n{stock_ctx}")

    if _needs_canyon(text) and tickers:
        score_ctx = "\n\n".join(_canyon_score_text(tk) for tk in tickers[:2])
        parts.append(f"[Canyon 评分]\n{score_ctx}")

    enriched = "\n\n---\n".join(parts)

    decision = _ask_claude(enriched)
    if not decision:
        return

    if decision.get("should_reply"):
        reply = (decision.get("reply") or "").strip()
        if reply:
            await msg.reply_text(reply)
            logger.info(
                f"Replied [{user.username or user_id}] "
                f"{text[:50]!r} → {reply[:60]!r}"
            )
    else:
        logger.debug(f"Skip ({decision.get('reason','')}) — {text[:60]!r}")


# ── 入口 ───────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set — aborting")
        return
    if not CLAUDE_API_KEY:
        logger.warning("CLAUDE_API_KEY not set — bot will fetch data but never reply")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))

    group_str     = f" group={GROUP_CHAT_ID}" if GROUP_CHAT_ID else " (all chats)"
    whitelist_str = f" whitelist={ALLOWED_USER_IDS}" if ALLOWED_USER_IDS else " whitelist=all"
    logger.info(f"Canyon Chat Bot ready —{group_str}{whitelist_str}")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
