# StockChaser · AI 链追涨看板

> 个人用，本地 Flask 服务 + 静态 HTML，准实时（yfinance 15-20 分钟延迟）。

## 安装

```bash
pip install yfinance pandas numpy openpyxl flask pyarrow
```

## 启动方式

### 方式 A：本地服务（推荐，能用刷新按钮、自动定时拉取）

```bash
python serve.py            # 真实数据，自动每 5 分钟（盘中）/ 30 分钟（盘后）刷新
python serve.py --mock     # 离线开发，使用合成数据
python serve.py --no-auto  # 关掉后台调度
```

然后打开 **http://localhost:8000**

### 方式 B：手动一次性更新（不开服务）

```bash
python update.py            # 真实数据
python update.py --mock     # 合成数据
```

然后双击 `web/index.html` 即可。

### 跑回测

```bash
python scripts/backtest.py
```

或在看板点 **📈 策略回测** Tab，第一次会自动加载。

---

## 策略框架（StockChaser v2）

### 三道闸门（必须全过才标 ✅ 可入场）

| 闸门 | 内容 |
|---|---|
| **Regime** 大盘环境 | SPY 在 SMA50 上方 |
| **Startup** 个股启动 (2/3) | 突破 4 周高 / 周涨 ≥ +3% / 5 日量 ≥ 20 日量 × 1.2 |
| **Quality** 质量底线 | 综合分 ≥ 80 + RS vs SPY ≥ 0pp + 距 52W 高 ≥ -25% |

### 状态分类

| 标签 | 含义 |
|---|---|
| ✅ READY | 三关全过，可入场 |
| 🔵 HOLD | 大盘风险关闭但个股仍强（已持有可继续） |
| ⚠️ WARN | 跌破 SMA20 或 RS 落后 ≥ -5pp |
| ⚠️ EXTENDED | 距 SMA20 ≥ +10%，短期超买 |

### ATR 仓位与止损（每行自动算）

```
ATR(20)        = 20 日平均真实波幅
建议止损       = 当前价 - 1.5 × ATR
建议目标价     = 当前价 + 3.0 × ATR (1:2 R:R)
建议仓位       = 风险预算(1%) / 止损距离 ，封顶 25%
```

→ 越波动的股票仓位自动越小，**组合最大回撤被锁死**。

### 五种离场（任一触发）

1. **硬止损** — 跌破 -1.5 × ATR
2. **移动止损** — 盈利 ≥ 1.5R 后跟在 SMA10 下方
3. **时间止损** — 10 个交易日没到 +1R
4. **信号反转** — 综合分跌破 60
5. **大盘转向** — SPY 跌破 SMA50

---

## 文件结构

```
StockChaser/
├── AI_Chain_Watchlist.xlsx   ← 股票池（手动维护，108 只 AI 链美股）
├── update.py                 ← 一次性更新（不开服务）
├── serve.py                  ← Flask 本地服务（推荐）
├── README.md
├── data/
│   ├── universe.json
│   ├── prices.json
│   ├── history.parquet/csv   ← 24 个月日线（用于回测）
│   ├── signals.json          ← 综合分 + 闸门 + 止损建议
│   └── backtest_results.json ← 4 套策略对比
├── scripts/
│   ├── extract_universe.py
│   ├── fetch_data.py         ← yfinance 真实数据
│   ├── fetch_mock.py         ← 合成数据
│   ├── signal.py             ← 三道闸门 + 综合分
│   └── backtest.py           ← 回测引擎
└── web/
    └── index.html            ← 看板前端
```

> 💡 注意：脚本文件名是 `signal.py` / `fetch_mock.py`（不是 `compute_signals.py` / `fetch_data_mock.py`）—— 上一版的 .pyc 缓存有冲突，改了名。

---

## 看板 Tab 一览

- **🌐 全部** — 全部 108 只
- **✅ 可入场** — 三道闸门全过
- **🎯 建仓核心池** — ★★★★★ + 核心配置
- **🚀 突破4周高** — 接近或破 4 周高点
- **📊 本周+3% / 📈 今日+3%** — 短期强势
- **🎯 趋势回调** — 在 SMA20 上 + 距 4W 高 -3% ~ -7% 的低吸点
- **🚂 多周期同向 / 💪 RS领涨** — 趋势确认信号

点击任一行展开详情：核心逻辑 + 三道闸门状态 + ATR + 止损 / 目标 / 仓位建议。

---

## 回测对比 4 套策略

| 策略 | 含义 |
|---|---|
| **S1** B&H SPY | 基准，买入持有 SPY |
| **S2** 6 月均值动量 Top 3 | 复刻你看到那篇文章的策略 |
| **S3** 综合分 Top 10 | 我们的综合分排名（无闸门） |
| **★ S4** 三道闸门 + ATR | 你正在用的实际策略 |

每个回测期生成净值曲线 + Sharpe + MaxDD + 周胜率。**S4 在大盘 risk-off 时不会建仓**（这是设计目标，不是 bug）。

---

## TODO（你后续可以加的）

- [ ] **Telegram 推送**（已 backlog）：满足"突破 4 周新高 + 综合分 > 100"自动推送
- [ ] 加入财报日历字段（yfinance 的 `Ticker.calendar`）
- [ ] 加入机构 EPS 上修（需 Refinitiv/FactSet）
- [ ] Windows 任务计划程序设定每周一 09:00 自动跑 `serve.py`
- [ ] 升级数据源到 Polygon/EOD（去掉 yfinance 15min 延迟）
