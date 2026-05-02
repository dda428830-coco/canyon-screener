# Canyon Screener

基于 **Canyon 分析法 v2.2** 的美股自动筛选系统，每个交易日盘前通过 GitHub Actions 自动运行，结果推送至 Telegram。

---

## 筛选架构

```
宇宙池（~500 只）
  S&P 500 + Nasdaq 100 + 自定义 Watchlist
  └─ 市值 > $500M，日均成交额 > $10M
          │
          ▼
    Module 0（~100 只）
    S0-1 价格动量：5日超额收益 vs QQQ/SPY > 3%
    S0-2 事件驱动：10 交易日内有财报
    S0-3 成交放大：3日均量/20日均量 > 1.5×
    S0-4 空头挤压：short ratio > 5
          │
          ▼
    Canyon C/E/M/F 评分（~10-20 只）
    ├─ 🟢 当前可买：C达标(≥5) + E通过(≥5)
    ├─ 🟡 强势复核：M强(≥2) + C达标 + E未通过
    └─ 🔵 潜伏观察：C强(≥7) + M弱
```

---

## 一、配置 Telegram Bot

### 1.1 创建 Bot

1. 在 Telegram 中搜索 **@BotFather**，发送 `/newbot`
2. 按提示输入 Bot 名称和用户名（用户名必须以 `bot` 结尾）
3. BotFather 返回一个 **API Token**，格式类似：`1234567890:ABCDEFGxxx...`
4. 保存这个 Token

### 1.2 获取 Chat ID

**方式一（推荐）：个人频道**
1. 给你的 Bot 发一条消息（任意内容）
2. 浏览器访问：`https://api.telegram.org/bot<你的TOKEN>/getUpdates`
3. 在返回的 JSON 中找到 `"chat":{"id":123456789}`，这个数字就是你的 Chat ID

**方式二：群组**
1. 将 Bot 加入群组，发送 `/start@你的bot用户名`
2. 同样通过 `getUpdates` 获取 Chat ID（群组 ID 是负数，如 `-1001234567890`）

**方式三：Channel**
1. 将 Bot 设为 Channel 管理员
2. Chat ID 格式为 `@channel_username` 或负数 ID

### 1.3 测试 Bot

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -H "Content-Type: application/json" \
  -d '{"chat_id": "<CHAT_ID>", "text": "Canyon Screener 测试消息"}'
```

返回 `"ok":true` 表示配置正确。

---

## 二、配置 GitHub Secrets

1. 进入你的 GitHub 仓库页面
2. 点击 **Settings → Secrets and variables → Actions → New repository secret**
3. 添加以下两个 Secret：

| Secret 名称          | 值                   |
|----------------------|----------------------|
| `TELEGRAM_BOT_TOKEN` | Bot 的 API Token     |
| `TELEGRAM_CHAT_ID`   | 你的 Chat ID（数字） |

---

## 三、部署到 GitHub

```bash
# 初始化仓库（将代码推送到 GitHub）
cd canyon-screener
git init
git add .
git commit -m "initial: canyon screener v2.2"

# 在 GitHub 上创建同名仓库后
git remote add origin https://github.com/<你的用户名>/canyon-screener.git
git push -u origin main
```

推送后，Actions 会按 `cron: "0 13 * * 1-5"`（UTC 13:00，对应 ET 9:00 AM）自动运行。

### 手动触发测试

GitHub 仓库页 → **Actions → Canyon Stock Screener → Run workflow**

---

## 四、本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 设置环境变量（或直接在 config.py 填入）
export TELEGRAM_BOT_TOKEN="你的Token"
export TELEGRAM_CHAT_ID="你的ChatID"

# 运行
python -m screener.main
```

若未设置 Telegram 环境变量，结果会直接打印到控制台，方便本地调试。

---

## 五、自定义配置

编辑 `screener/config.py`：

| 参数                  | 说明                                 | 默认值   |
|-----------------------|--------------------------------------|----------|
| `CUSTOM_WATCHLIST`    | 额外关注的股票列表                   | 见文件   |
| `MIN_MARKET_CAP`      | 最低市值过滤                         | $500M    |
| `MIN_AVG_DOLLAR_VOLUME` | 最低日均成交额                     | $10M     |
| `S0_MOMENTUM_THRESHOLD` | M0 超额收益阈值                    | 3%       |
| `S0_SHORT_RATIO`      | 空头挤压阈值                         | 5        |
| `LEAD_SECTORS`        | 主线行业（映射纯度高）               | 科技/通信/消费 |
| `INDUSTRY_MEDIAN_PE`  | 各行业 Forward PE 中位数参考值       | 见文件   |

---

## 六、Telegram 推送示例

```
📊 Canyon 筛选系统 — 2026-05-02
🟢 当前可买: 3 只
🟡 强势复核: 5 只
🔵 潜伏观察: 8 只
────────────────────────────

🟢 当前可买
$NVDA — NVIDIA Corporation
C: 7分 | E: 5分 | M: 3分 | F: 5档
催化：财报在 6个交易日后 (近端 6日)
错价：Forward PE 24.5 vs 行业中位数 25（折价2.0%）
位置：距20日高点回撤 8.3%
动量：5日超额收益 +6.80% | 盈亏比 2.3
行业：Technology (主线行业)
建议：初始仓位 5-10% / 突破或回调买入
---
```

---

## 七、评分规则速查

### C 分（满分 9 分，≥5 达标，≥7 强）

| 维度       | 规则                                          | 分值 |
|------------|-----------------------------------------------|------|
| 催化距离   | 财报 ≤10日/11-60日/其他                       | 3/2/0 |
| 错价幅度   | Forward PE 折价 >20%/10-20%/其他             | 2/1/0 |
| 映射纯度   | 主线行业/相关/未知                            | 2/1/0 |
| 催化可信度 | 有确认财报日/无                               | 2/0  |

### E 分（满分 8 分，≥5 通过）

| 维度       | 规则                                          | 分值 |
|------------|-----------------------------------------------|------|
| 位置       | 回撤 5-15%/接近高点或25%内/其他               | 2/1/0 |
| 盈亏比     | ATR估算 ≥2:1 / 1.5-2 / <1.5                  | 2/1/0 |
| 量价确认   | 3日/20日均量在 0.8-1.5 之间                   | 1/0  |
| 拥挤度     | 5日/60日均量 < 2倍                            | 1/0  |

### M 分（满分 4 分，≥2 为强）

| 维度       | 规则                                          | 分值 |
|------------|-----------------------------------------------|------|
| 价格动量   | 超额 >5% / 0-5% / 负                         | 2/1/0 |
| 成交放大   | 3日/20日均量 > 1.5×                          | 1/0  |
| 近端催化   | 10交易日内有财报                              | 1/0  |

### F 分（1-5 档）

基于 `debtToEquity`、`returnOnEquity`、`revenueGrowth` 三项指标综合评分。

---

## 八、注意事项

- **数据来源**：yfinance 为 Yahoo Finance 非官方封装，可能因 Yahoo 接口变更而出现数据异常
- **财报日期**：部分股票的财报日期可能未更新，请自行核实重要个股
- **盘前运行**：Actions cron 设置的是 9:00 AM ET，但实际执行可能因队列延迟有几分钟误差
- **频率限制**：yfinance 存在频率限制，扫描 500+ 股票约需 5-15 分钟
- **投资风险**：本系统仅供参考，不构成投资建议，实际交易请结合自身判断
