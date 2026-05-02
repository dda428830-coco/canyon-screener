import os

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── 自定义 Watchlist（在这里添加你额外关注的股票）────────────────────────────
CUSTOM_WATCHLIST = [
    "NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "TSLA",
    "AMD",  "AVGO", "ORCL", "CRM",   "ADBE", "NFLX", "UBER",
    "ARM",  "PLTR", "SMCI", "MU",    "QCOM", "SNOW", "NET",
    "COIN", "MSTR", "HOOD", "RIVN",  "LCID", "SOFI", "AFRM",
]

# ── 第一层：宇宙池过滤阈值 ────────────────────────────────────────────────────
MIN_MARKET_CAP         = 500_000_000   # $500M
MIN_AVG_DOLLAR_VOLUME  = 10_000_000    # $10M 日均成交额

# ── 第二层：Module 0 阈值 ─────────────────────────────────────────────────────
S0_MOMENTUM_THRESHOLD  = 0.03   # 超额收益 > 3%
S0_VOLUME_RATIO        = 1.5    # 3日/20日均量 > 1.5x
S0_SHORT_RATIO         = 5.0    # short ratio > 5
S0_EARNINGS_WINDOW     = 10     # 财报在 10 个交易日以内

# ── 第三层：C 分阈值 ──────────────────────────────────────────────────────────
C_NEAR_DAYS            = 10     # 近端财报（≤10 日）
C_MID_DAYS             = 60     # 中端财报（11-60 日）
C_PE_DISCOUNT_HIGH     = 0.20   # Forward PE 折价 > 20% → 2分
C_PE_DISCOUNT_LOW      = 0.10   # Forward PE 折价 10-20% → 1分

# ── 第三层：E 分阈值 ──────────────────────────────────────────────────────────
E_PULLBACK_LOW         = 0.05   # 距20日高点回撤 5%
E_PULLBACK_HIGH        = 0.15   # 距20日高点回撤 15%
E_RR_GOOD              = 2.0    # 盈亏比 ≥ 2:1
E_RR_OK                = 1.5    # 盈亏比 1.5-2
E_VOL_RATIO_LOW        = 0.8    # 量价正常区间下限
E_VOL_RATIO_HIGH       = 1.5    # 量价正常区间上限
E_CROWD_THRESHOLD      = 2.0    # 拥挤度阈值

# ── 第三层：M 分阈值 ──────────────────────────────────────────────────────────
M_EXCESS_STRONG        = 0.05   # 5日超额收益 > 5% 为强
M_VOL_AMP              = 1.5    # 成交放大倍数

# ── 行业 Forward PE 中位数参考值（可按市场环境调整）────────────────────────────
INDUSTRY_MEDIAN_PE = {
    "Technology":             25,
    "Healthcare":             20,
    "Financials":             13,
    "Consumer Discretionary": 22,
    "Consumer Staples":       18,
    "Energy":                 14,
    "Materials":              15,
    "Industrials":            18,
    "Utilities":              14,
    "Real Estate":            19,
    "Communication Services": 20,
    "Unknown":                18,
}

# ── 主线行业（映射纯度高）────────────────────────────────────────────────────
LEAD_SECTORS = {"Technology", "Communication Services", "Consumer Discretionary"}

# ── 并发控制 ──────────────────────────────────────────────────────────────────
UNIVERSE_WORKERS = 20   # 宇宙池并发数
MODULE0_WORKERS  = 15   # Module 0 并发数
SCORE_WORKERS    = 10   # 评分并发数

# ── 反馈系统 ──────────────────────────────────────────────────────────────────
ALLOWED_USER_IDS: list[int] = []   # Telegram 用户 ID 白名单；空列表 = 允许所有人
CLAUDE_API_KEY   = os.environ.get("CLAUDE_API_KEY", "")
GROUP_CHAT_ID    = os.environ.get("GROUP_CHAT_ID", "")   # 群组 Chat ID（负整数字符串）

# === 上周校准建议（待确认）===
# CALIBRATION_DATE = "尚未运行回测"
# （每次周五回测运行后，backtest.py 会自动更新此处的校准建议）
# 手动确认无误后，把对应变量值更新到上方对应参数处。
# 例：若建议 SUGGESTED_E_RR_OK = 2.0，则把 E_RR_OK = 1.5 改为 E_RR_OK = 2.0
# ==============================
