# StockChaser v3.7 重构总结

## 新目录结构

```
StockChaser/
├── engine/                       ✨ 业务逻辑核心 package
│   ├── __init__.py                  公开 API
│   ├── scoring.py                   TS / EQ / FC / Conviction + 信号检测
│   ├── regime.py                    SPY 基础 × VIX 阻尼层
│   ├── framework.py                 F1/F2/F3 + 6M-Mom 标签
│   ├── action.py                    action 映射 + PRIORITY + 仓位建议
│   ├── stops.py                     HWM 追踪止损阶梯
│   ├── alerts.py                    触发检测 + 7-day sticky suppress
│   ├── playbook.py                  cash-aware 操作手册
│   └── pipeline.py                  整合编排
│
├── scripts/                      ← 瘦身, 只放 entry 点
│   ├── run_pipeline.py              ✨ 替代 engine_v35.py + dynamic_stops.py + alerts.py
│   ├── fetch_data.py                yfinance 数据抓取
│   ├── fetch_mock.py                离线开发用 mock 数据
│   ├── backtest.py                  从 bt_v36.py 改名 (跑历史回测)
│   ├── notify_telegram.py
│   ├── telegram_bot.py
│   ├── sync_fidelity.py             持仓同步工具
│   ├── missed_entry_advisor.py
│   └── extract_universe.py
│
├── serve.py                      ✏️ 改 import: from engine import run_pipeline, ...
└── web/, data/, data_seed/       不变
```

## 命令对照

| v3.6 之前 | v3.7 之后 |
|---|---|
| `python3 scripts/engine_v35.py` | `python3 scripts/run_pipeline.py` |
| `python3 scripts/dynamic_stops.py` | (集成到 run_pipeline) |
| `python3 scripts/alerts.py` | (集成到 run_pipeline) |
| `python3 scripts/bt_v36.py` | `python3 scripts/backtest.py` |

## 已删除文件

**重复回测** (留 bt_v36 改名 backtest.py)：
- `scripts/bt.py`
- `scripts/bt_v351.py`
- `scripts/bt_analyze_periods.py`
- `scripts/bt_exit_variants.py`

**旧版本 engine**：
- `scripts/engine_v31.py`
- `scripts/engine_v35.py` (→ engine/ 包)
- `scripts/dynamic_stops.py` (→ engine/stops.py)
- `scripts/alerts.py` (→ engine/alerts.py)
- `scripts/playbook.py` (→ engine/playbook.py)

**一次性 patch 脚本**:
- `scripts/patch_dashboard_advisory.py`
- `scripts/patch_index_html.py`
- `scripts/patch_reduce_profit.py`
- `scripts/patch_strategy_aware_ui.py`

**一次性 setup 脚本**:
- `scripts/mark_high_conviction.py`
- `scripts/set_strategy_labels.py`

**未使用**:
- `scripts/signal.py`
- `scripts/parquet_to_csv.py`

## 编程接口

```python
# 完整 pipeline
from engine import run_pipeline, Paths
run_pipeline(Paths())   # 默认相对 repo 根

# 单独使用
from engine import active_alerts, dismiss_alert, compute_stop
paths = Paths()
alerts = active_alerts(paths.signals, paths.holdings, paths.pending)
```

## 测试通过

- TS=92, EQ=81 on test row ✓
- 强 risk-on + VIX 17 → regime 1.10 ✓
- 所有 engine 模块 import OK ✓
- serve.py 编译 OK ✓
