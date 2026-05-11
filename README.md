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
python scripts/bt_v33.py
```

或在看板点 **📈 策略回测** Tab，第一次会自动加载。

---

## 🔥 跑真实数据完整流程（推荐每周一上午做一次）

```bash
cd C:\Users\hongh\Documents\Claude\Projects\StockChaser

# 1. 抓 yfinance 实盘价格 (24 个月历史 + 最新行情)
python scripts/fetch_data.py
# 输出: data/prices.json 和 data/history.parquet
# 期望看到: "Fetching 113 tickers..." 然后 "SPY 4-week: +X.XX%   above SMA50: True/False"

# 2. 计算 Conviction 信号 + Framework 标签
python scripts/engine_v32.py
# 输出: data/signals.json
# 期望: "Action distribution: {'STRONG_BUY': N, ...}" + "Priority Top-8" 列表

# 3. 跑 6 套策略历史回测 (含 ★ S6 Hybrid)
python scripts/bt_v33.py
# 输出: data/backtest_results.json
# 期望: 横评表，★ Hybrid 6M+Conv 行有数据

# 4. 打开看板 (双击或浏览器打开)
start web/index.html
```

> **首次跑会比较慢** (yfinance 抓 113 只 × 24 个月 ≈ 30-60 秒)
> 后续每周只需要重跑 `fetch_data.py` 和 `engine_v32.py`，几秒钟搞定

---

## 策略框架（StockChaser v3.2）

### Conviction Engine — 三轴评分

```
Trend Strength (TS) 40% + Entry Quality (EQ) 30% + Fundamental (FC) 30%
       × Regime Modifier (0.35-1.10)
       = CONVICTION (0-100)
```

- **TS** 趋势强度: 均线位置 / 距 4W 高 / RS / 多周期同向 / **6 月月均动量**
- **EQ** 入场质量: 周涨甜区 (3-8%) / 量能确认 / ATR / 是否过度追高
- **FC** 基本面: ★评级 + 角色 (龙头/二线/概念) + **下游传导加成** + **新节点加成**
- **RM** Regime: 渐变 0.35-1.10，非二元开关

### 三个产业链框架 (Framework Tags)

| 标签 | 触发条件 | 含义 |
|---|---|---|
| 🌊 F1 下游传导 | L10/L11/L12 + 综合分 ≥ 50 | 12-18 月资金传导期 |
| 🧮 F2 财报真空 | 距 52W 高 -10~-30% + SMA50 上 + 综合 ≥ 70 | 长底+健康，黎明前 |
| 💡 F3 新节点 | 16 只白名单 (SNDK/ALAB/CRDO/...) | 凭空多出来的位置 |
| 📅 6M-Mom | 6 月月均 ≥ 3% | 持续长期动量 |

### 5 档行动建议

| Conviction | 动作 | 仓位 | 默认 % |
|---:|---|---|---:|
| ≥ 85 | 🔥 强力买入 | 重仓 | 10% |
| 72-84 | 🟢 买入 | 中仓 | 7% |
| 58-71 | 🟡 试仓 | 小仓 | 4% |
| 42-57 | 👀 观察 | — | 0 |
| < 42 | ⚪ 回避 | — | 0 |

> STRONG_BUY 额外需要 TS ≥ 80 AND EQ ≥ 65（不允许有短板）
> 每层最多 2 个 STRONG_BUY（强制跨层分散）
> 顶部 8 只标 🔥 PRIORITY = 这周实际开仓清单

### 回测结果（mock 50 周）

| 策略 | 总收益 | Sharpe | MaxDD |
|---|---:|---:|---:|
| ★ **S6 Hybrid 6M+Conv** | +15.45% | **1.76** | **-4.00%** |
| S3 Comp Top10 | +30.62% | 2.44 | -5.65% |
| S2 6M-Mom Top3 | +52.12% | 1.70 | -10.94% |
| S5 Conviction | +2.49% | 0.41 | -6.14% |
| S1 B&H SPY | +3.02% | 0.27 | -10.71% |

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
