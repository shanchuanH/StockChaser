"""Personal playbook generator — turn signals into 傻瓜式 operation plans.

For each stock with action >= TRY_BUY, generates a complete playbook:
  - 3-batch entry plan (with exact prices & share counts)
  - 4-step stop ladder (initial → breakeven → +5% → trail SMA20)
  - 3-tier profit taking (+1R / +2R / +3R)
  - 5 exit triggers

Reads:
  data/portfolio.json   (user config — cash, risk %, style)

Used by:
  engine_v34.py — attaches a 'playbook' dict to each row
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "portfolio.json"


def load_config():
    if not CONFIG_PATH.exists():
        return DEFAULT_CONFIG.copy()
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_CONFIG.copy()


DEFAULT_CONFIG = {
    "portfolio_cash_usd": 10000,
    "max_positions": 5,
    "cash_buffer_pct": 25,
    "risk_per_trade_pct": 1.5,
    "max_position_size_pct": 20,
    "entry_batches": [
        {"step": 1, "weight_pct": 7, "trigger": "立即买入 (信号触发)"},
        {"step": 2, "weight_pct": 7, "trigger": "涨 +3% 且未跌破 SMA10"},
        {"step": 3, "weight_pct": 6, "trigger": "突破 4W 高"},
    ],
    "stop_ladder": [
        {"after_gain_pct": 0,  "stop_logic": "initial_atr",  "desc": "入场价 - 1.5 ATR"},
        {"after_gain_pct": 5,  "stop_logic": "breakeven",    "desc": "保本"},
        {"after_gain_pct": 10, "stop_logic": "entry_plus_5", "desc": "入场价 + 5%"},
        {"after_gain_pct": 20, "stop_logic": "trail_sma20",  "desc": "动态 SMA20"},
    ],
    "profit_taking": [
        {"r_multiple": 1, "reduce_fraction": 0.25, "desc": "+1R 减 1/4"},
        {"r_multiple": 2, "reduce_fraction": 0.33, "desc": "+2R 减 1/3"},
        {"r_multiple": 3, "reduce_fraction": 0.00, "desc": "+3R 骑趋势, SMA20 追踪"},
    ],
    "exit_triggers": [
        "触发硬止损 → 全部清仓",
        "Conviction 跌破 45 → 减半仓",
        "SPY 跌破 SMA50 → 减半仓",
        "时间止损: 4 周无 +1R → 减半仓",
        "单日跌幅 > 8% → 减至 30%",
    ],
}


def _shares(cash_amount, price):
    """Whole-share count given budget and price."""
    if price <= 0:
        return 0
    return int(cash_amount // price)


def build_playbook(row, config=None):
    """Generate complete playbook for one stock row.

    row keys needed: ticker, latest_price, atr_20, action, conviction_score,
                     high_4w, sma_20, sma_50
    Returns dict suitable for JSON serialization, or None if action is too weak.
    """
    if config is None:
        config = load_config()
    action = row.get("action", "AVOID")
    if action in ("AVOID", "WATCH"):
        return None

    price = row.get("latest_price") or 0
    atr = row.get("atr_20") or 0
    if price <= 0 or atr <= 0:
        return None

    cash = float(config.get("portfolio_cash_usd", 10000))
    risk_pct = float(config.get("risk_per_trade_pct", 1.5)) / 100
    max_pos_pct = float(config.get("max_position_size_pct", 20)) / 100

    # Map action → target position size %
    action_size = {
        "STRONG_BUY": max_pos_pct,         # full 20%
        "BUY":        max_pos_pct * 0.75,   # 15%
        "TRY_BUY":    max_pos_pct * 0.40,   # 8%
    }.get(action, max_pos_pct * 0.40)

    # Risk-budget cap — if ATR is huge, reduce position so single trade ≤ risk_pct
    stop_distance = 1.5 * atr
    if stop_distance > 0:
        risk_limited_pct = (cash * risk_pct) / (stop_distance * cash / price) / cash
        # The above simplifies to: position_pct = risk_pct * price / stop_distance
        risk_limited_pct = risk_pct * price / stop_distance
        target_pct = min(action_size, risk_limited_pct)
    else:
        target_pct = action_size

    target_pct = round(target_pct, 4)
    total_cash = cash * target_pct
    total_shares = _shares(total_cash, price)

    # Entry plan — split target across configured batches
    batches = config.get("entry_batches", DEFAULT_CONFIG["entry_batches"])
    total_weight = sum(b["weight_pct"] for b in batches)
    entry_plan = []
    high_4w = row.get("high_4w") or price * 1.05
    for i, b in enumerate(batches):
        frac = b["weight_pct"] / total_weight
        batch_cash = total_cash * frac
        if b["step"] == 1:
            batch_price = price
            trigger = b["trigger"]
        elif b["step"] == 2:
            batch_price = round(price * 1.03, 2)
            trigger = f"涨至 ${batch_price:.2f} (+3%) 且未跌破 SMA10"
        else:
            batch_price = round(max(high_4w, price * 1.06), 2)
            trigger = f"突破 ${batch_price:.2f} (4W 高 或 +6%)"
        entry_plan.append({
            "step": b["step"],
            "trigger": trigger,
            "weight_pct": round(frac * target_pct * 100, 2),
            "price": batch_price,
            "shares": _shares(batch_cash, batch_price),
            "cash": round(batch_cash, 2),
        })

    # Stop ladder — turn each rule into a concrete price
    stop_ladder = []
    initial_stop = round(price - stop_distance, 2)
    for s in config.get("stop_ladder", DEFAULT_CONFIG["stop_ladder"]):
        gain = s["after_gain_pct"] / 100
        if s["stop_logic"] == "initial_atr":
            stop_price = initial_stop
        elif s["stop_logic"] == "breakeven":
            stop_price = round(price, 2)  # entry price
        elif s["stop_logic"] == "entry_plus_5":
            stop_price = round(price * 1.05, 2)
        elif s["stop_logic"] == "trail_sma20":
            stop_price = row.get("sma_20")  # snapshot — will move with SMA20
        else:
            stop_price = None
        stop_ladder.append({
            "after_gain_pct": s["after_gain_pct"],
            "trigger_price": round(price * (1 + gain), 2),
            "stop_price": stop_price,
            "desc": s["desc"],
        })

    # Profit taking — 1R, 2R, 3R targets
    R = stop_distance  # 1R = stop distance
    profit_taking = []
    for pt in config.get("profit_taking", DEFAULT_CONFIG["profit_taking"]):
        r = pt["r_multiple"]
        tgt_price = round(price + r * R, 2)
        profit_taking.append({
            "r_multiple": r,
            "target_price": tgt_price,
            "gain_pct": round(r * R / price * 100, 1),
            "reduce_fraction": pt["reduce_fraction"],
            "shares_to_sell": int(total_shares * pt["reduce_fraction"]),
            "desc": pt["desc"],
        })

    exit_triggers = config.get("exit_triggers", DEFAULT_CONFIG["exit_triggers"])

    # Risk summary
    risk_cash = total_shares * stop_distance
    risk_pct_of_portfolio = round(risk_cash / cash * 100, 2) if cash else 0

    return {
        "summary": f"目标仓位 {round(target_pct*100,1)}% (${round(total_cash,0):.0f}/{total_shares} 股), 分 {len(batches)} 批",
        "config_snapshot": {
            "portfolio_cash_usd": cash,
            "risk_per_trade_pct": config.get("risk_per_trade_pct", 1.5),
        },
        "target_position_pct": round(target_pct * 100, 2),
        "target_cash_usd": round(total_cash, 2),
        "target_shares": total_shares,
        "initial_stop_price": initial_stop,
        "stop_distance_atr": 1.5,
        "single_trade_risk_usd": round(risk_cash, 2),
        "single_trade_risk_pct": risk_pct_of_portfolio,
        "entry_plan": entry_plan,
        "stop_ladder": stop_ladder,
        "profit_taking": profit_taking,
        "exit_triggers": exit_triggers,
    }


def annotate_rows(rows, config=None):
    """Attach playbook to each row in-place."""
    if config is None:
        config = load_config()
    for r in rows:
        try:
            r["playbook"] = build_playbook(r, config)
        except Exception as e:
            r["playbook"] = None
            r["playbook_error"] = str(e)
    return rows


# Smoke test if run directly
if __name__ == "__main__":
    sample = {
        "ticker": "VRT", "latest_price": 168.50, "atr_20": 4.20,
        "action": "STRONG_BUY", "conviction_score": 96.7,
        "high_4w": 175.30, "sma_20": 162.10, "sma_50": 155.40,
    }
    cfg = load_config()
    pb = build_playbook(sample, cfg)
    print(json.dumps(pb, ensure_ascii=False, indent=2))
