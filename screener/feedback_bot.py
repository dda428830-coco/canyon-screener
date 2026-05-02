"""
群组反馈收集 Bot

Mode A（默认）：单次轮询，适合 GitHub Actions cron
Mode B：长驻轮询，适合 VPS / Railway

通过环境变量 FEEDBACK_BOT_MODE 切换（A / B）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

from screener.config import (
    ALLOWED_USER_IDS,
    CLAUDE_API_KEY,
    GROUP_CHAT_ID,
    TELEGRAM_BOT_TOKEN,
)

logger = logging.getLogger(__name__)

DATA_DIR      = Path(__file__).parent.parent / "data"
FEEDBACK_FILE = DATA_DIR / "feedback.json"

_SYSTEM_PROMPT = """\
你是一个股票交易日志解析器。用户会发送他们的真实交易经历或观察（中文或英文）。

你的任务：判断这条消息是否是一条有价值的交易反馈，并以 JSON 格式返回解析结果。

如果消息与交易/股票无关（如问候、闲聊、系统消息），返回：
{"relevant": false}

如果消息包含交易反馈，返回如下 JSON（所有字段均为小写英文）：
{
  "relevant": true,
  "ticker": "股票代码（大写，如 NVDA；如无明确代码则为 null）",
  "action": "buy / sell / skip / hold / watch（用户做了什么或决定不做什么）",
  "result": "profit / loss / flat / pending / unknown",
  "reason": "用户描述的原因或背景（简短总结，英文，50字以内）",
  "signal_quality": "good / ok / poor（用户对信号质量的评价；如未提及则为 ok）"
}

注意：
- ticker 尽量从消息中提取，可以是 $NVDA 或 NVDA 或 "英伟达" 等形式
- action 如果用户说"没买"/"放弃"/"观望"等，统一用 skip
- result 如果交易还未结束或结果不明，用 pending 或 unknown
- 只返回 JSON，不要其他任何文字
"""


def _parse_with_claude(message: str) -> dict | None:
    if not CLAUDE_API_KEY:
        logger.warning("CLAUDE_API_KEY not set, skipping parse")
        return None

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": message}],
        )
        text = resp.content[0].text.strip()
        # strip markdown code fences if present
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logger.warning(f"Claude parse failed: {e}")
        return None


def _append_feedback(entry: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing: list = []
    if FEEDBACK_FILE.exists():
        try:
            existing = json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    existing.append(entry)
    FEEDBACK_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def _process_message(text: str, user_id: int, username: str, message_date: datetime) -> None:
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return

    parsed = _parse_with_claude(text)
    if not parsed or not parsed.get("relevant"):
        return

    entry = {
        "date":           message_date.strftime("%Y-%m-%d"),
        "user":           username or str(user_id),
        "ticker":         parsed.get("ticker"),
        "action":         parsed.get("action"),
        "result":         parsed.get("result"),
        "reason":         parsed.get("reason"),
        "signal_quality": parsed.get("signal_quality", "ok"),
        "raw_message":    text[:500],
    }
    _append_feedback(entry)
    logger.info(f"Saved feedback: {entry['ticker']} / {entry['action']} from {entry['user']}")


# ── Mode A：单次轮询（GitHub Actions 兼容）─────────────────────────────────────

async def _run_mode_a(hours_back: int = 26) -> None:
    from telegram import Bot

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    updates = await bot.get_updates(limit=100, timeout=10)
    logger.info(f"Mode A: fetched {len(updates)} updates")

    for update in updates:
        msg = update.message
        if not msg or not msg.text:
            continue
        if GROUP_CHAT_ID and str(msg.chat.id) != str(GROUP_CHAT_ID):
            continue
        if msg.date < cutoff:
            continue

        user    = msg.from_user
        user_id  = user.id if user else 0
        username = user.username or (user.full_name if user else "") or str(user_id)
        _process_message(msg.text, user_id, username, msg.date)


# ── Mode B：长驻轮询（VPS / Railway）──────────────────────────────────────────

async def _message_handler(update, context) -> None:
    msg = update.message
    if not msg or not msg.text:
        return
    if GROUP_CHAT_ID and str(msg.chat.id) != str(GROUP_CHAT_ID):
        return

    user     = msg.from_user
    user_id  = user.id if user else 0
    username = user.username or (user.full_name if user else "") or str(user_id)
    _process_message(msg.text, user_id, username, msg.date)


def _run_mode_b() -> None:
    from telegram.ext import Application, MessageHandler, filters

    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _message_handler))
    logger.info("Mode B: starting long-polling...")
    app.run_polling(drop_pending_updates=True)


# ── 入口 ───────────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mode = os.environ.get("FEEDBACK_BOT_MODE", "A").upper()
    if mode == "B":
        _run_mode_b()
    else:
        asyncio.run(_run_mode_a())


if __name__ == "__main__":
    main()
