"""StockChaser v3.1 — Conviction Engine (recalibrated).

CHANGES from v3:
  - Regime modifier capped at 1.10 (was 1.20) — prevent score inflation in bulls
  - STRONG_BUY threshold raised to 85 (was 80) and adds sub-score floors
      (must have TS >= 80 AND EQ >= 65 — no weak link allowed)
  - BUY raised to 72 (was 65), TRY_BUY to 58 (was 50)
  - Per-layer cap: max 2 STRONG_BUY signals per layer
      (avoid "all 10 STRONG_BUY are in optical")
  - priority_rank: only top N (default 8) STRONG_BUY get "PRIORITY" flag

THE FRAMEWORK (unchanged):
  Trend Strength (TS)    0-100  : how clean is the uptrend
  Entry Quality  (EQ)    0-100  : how good is the *current* setup
  Fundamental    (FC)    0-100  : potential + role + composite
  Regime Modifier(RM)   0.3-1.1 : graded market environment

  CONVICTION = (TS*0.4 + EQ*0.3 + FC*0.3) * RM   in [0, 100]
"""
import json
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "data" / "universe.json"
PRICES = ROOT / "data" / "prices.json"
OUT = ROOT / "data" / "signals.json"

MAX_STRONG_BUY_PER_LAYER = 2
PRIORITY_TOP_N = 8


SIGNAL_META = {
    "BREAKOUT":       {"emoji": "\U0001f680", "label": "突破4周高",  "weight": 10, "buy_class": "buy"},
    "STRONG_DAY":     {"emoji": "\U0001f4c8", "label": "今日+3%",   "weight": 4,  "buy_class": "buy"},
    "STRONG_WEEK":    {"emoji": "\U0001f4ca", "label": "本周+3%",   "weight": 6,  "buy_class": "buy"},
    "VOLUME_SURGE":   {"emoji": "\U0001f50a", "label": "成交量1.2x", "weight": 5,  "buy_class": "buy"},
    "TREND_PULLBACK": {"emoji": "\U0001f3af", "label": "趋势回调",  "weight": 7,  "buy_class": "buy"},
    "ACCEL":          {"emoji": "\U0001f682", "label": "多周期同向", "weight": 5,  "buy_class": "buy"},
    "RS_LEADER":      {"emoji": "\U0001f4aa", "label": "RS 领涨",   "weight": 5,  "buy_class": "buy"},
    "EXTENDED":       {"emoji": "\u26a0\ufe0f", "label": "短期超买",   "weight": -3, "buy_class": "warn"},
    "BELOW_SMA20":    {"emoji": "\U0001f53b", "label": "跌破SMA20", "weight": -5, "buy_class": "warn"},
    "RS_LAG":         {"emoji": "\U0001f422", "label": "RS 落后",   "weight": -3, "buy_class": "warn"},
}

ACTION_META = {
    "STRONG_BUY":  {"emoji": "\U0001f525", "label": "强力买入", "color": "#d63031", "size_class": "重仓", "size_pct": 10.0},
    "BUY":         {"emoji": "\U0001f7e2", "label": "买入",     "color": "#3fb950", "size_class": "中仓", "size_pct": 7.0},
    "TRY_BUY":     {"emoji": "\U0001f7e1", "label": "试仓",     "color": "#f1c40f", "size_class": "小仓", "size_pct": 4.0},
    "WATCH":       {"emoji": "\U0001f440", "label": "观察",     "color": "#8b949e", "size_class": "观察", "size_pct": 0.0},
    "AVOID":       {"emoji": "\u26aa", "label": "回避",     "color": "#8b949e", "size_class": "回避", "size_pct": 0.0},
}

HORIZON_META = {
    "短线":   {"emoji": "\u26a1", "days": "1-3 周",   "desc": "事件驱动型动量"},
    "中线":   {"emoji": "\U0001f4c8", "days": "2-6 周",   "desc": "趋势中段"},
    "中长线": {"emoji": "\U0001f3d4", "days": "1-3 月",   "desc": "base building 完成"},
}


def detect_signals(p):
    flags = []
    last = p.get("latest_price") or 0
    high4 = p.get("high_4w") or 0
    if high4 and last >= high4 * 0.99:
        flags.append("BREAKOUT")
    if (p.get("daily_return_pct") or 0) >= 3:
        flags.append("STRONG_DAY")
    if (p.get("week_return_pct") or 0) >= 3:
        flags.append("STRONG_WEEK")
    vr = p.get("vol_ratio_5_20")
    if vr and vr >= 1.2:
        flags.append("VOLUME_SURGE")
    dist = p.get("dist_4w_high_pct") or 0
    if p.get("above_sma_20") and -7 <= dist <= -3:
        flags.append("TREND_PULLBACK")
    if (p.get("week_return_pct") or 0) > 0 and (p.get("month_return_pct") or 0) > 0 and (p.get("m3_return_pct") or 0) > 0:
        flags.append("ACCEL")
    if (p.get("rs_vs_spy_4w_pp") or 0) >= 5:
        flags.append("RS_LEADER")
    sma20 = p.get("sma_20") or 0
    if sma20 and last >= sma20 * 1.10:
        flags.append("EXTENDED")
    if sma20 and last < sma20:
        flags.append("BELOW_SMA20")
    if (p.get("rs_vs_spy_4w_pp") or 0) <= -5:
        flags.append("RS_LAG")
    return flags


def trend_strength(p):
    s = 50
    if p.get("above_sma_20"): s += 8
    if p.get("above_sma_50"): s += 8
    d4 = p.get("dist_4w_high_pct") or 0
    if d4 >= -1: s += 15
    elif d4 >= -5: s += 8
    elif d4 < -15: s -= 12
    rs = p.get("rs_vs_spy_4w_pp") or 0
    if rs >= 8: s += 12
    elif rs >= 3: s += 6
    elif rs <= -5: s -= 10
    w, m, m3 = (p.get(k) or 0 for k in ("week_return_pct", "month_return_pct", "m3_return_pct"))
    aligned = sum(1 for x in (w, m, m3) if x > 0)
    s += [0, 2, 5, 10][aligned] if aligned <= 3 else 10
    return max(0, min(100, s))


def entry_quality(p):
    s = 50
    week = p.get("week_return_pct") or 0
    if 3 <= week <= 8: s += 18
    elif 8 < week <= 12: s += 6
    elif week > 12: s -= 8
    elif 0 <= week < 3: s += 4
    elif week < -3: s -= 12
    vr = p.get("vol_ratio_5_20") or 1
    if vr >= 1.4: s += 12
    elif vr >= 1.2: s += 8
    elif vr < 0.7: s -= 6
    atr = p.get("atr_pct")
    if atr is not None:
        if atr > 7: s -= 12
        elif atr > 5: s -= 5
        elif atr < 1.5: s -= 4
    last = p.get("latest_price") or 0
    sma20 = p.get("sma_20") or 0
    if sma20 and last / sma20 > 1.12: s -= 15
    elif sma20 and last / sma20 > 1.08: s -= 7
    d4 = p.get("dist_4w_high_pct") or 0
    if d4 > 3: s -= 8
    return max(0, min(100, s))


def fundamental_conviction(u, comp_score):
    pot_pts = (u.get("potential", 0) or 0) * 12
    role_bonus = {"龙头": 12, "二线": 6, "概念": 0}.get(u.get("role"), 0)
    layer_key = u.get("layer", "").split("·")[0]
    leader_bonus = 4 if layer_key in ("L2", "L3", "L4", "L6", "L10", "L11") else 0
    s = pot_pts + role_bonus + leader_bonus + max(0, comp_score - 70) * 0.3
    return max(0, min(100, s))


def regime_modifier(prices_root):
    """RM capped at 1.10 (was 1.20) — prevents bull-market inflation."""
    spy_above_50 = bool(prices_root.get("spy_above_sma50"))
    spy_4w = prices_root.get("spy_4w_return_pct") or 0
    if spy_above_50 and spy_4w >= 4:    return 1.10
    if spy_above_50 and spy_4w >= 1:    return 1.00
    if spy_above_50:                     return 0.90
    if -3 <= spy_4w < 0:                 return 0.75
    if spy_4w < -5:                      return 0.35
    return 0.55


def conviction_score(ts, eq, fc, rm):
    base = ts * 0.4 + eq * 0.3 + fc * 0.3
    return round(min(100, max(0, base * rm)), 1)


def map_action(conviction, ts, eq, fc):
    """Tightened thresholds + sub-score floors.

    STRONG_BUY now requires no weak link: TS>=80 AND EQ>=65.
    This catches the case where a stock is conviction-high but has hidden weakness.
    """
    if conviction >= 85 and ts >= 80 and eq >= 65:
        return "STRONG_BUY"
    if conviction >= 72 and ts >= 70:
        return "BUY"
    if conviction >= 58:
        return "TRY_BUY"
    if conviction >= 42:
        return "WATCH"
    return "AVOID"


def map_horizon(p):
    week = p.get("week_return_pct") or 0
    month = p.get("month_return_pct") or 0
    d4 = p.get("dist_4w_high_pct") or 0
    d52 = p.get("dist_52w_high_pct") or 0
    above50 = p.get("above_sma_50")
    if -3 <= d4 <= 1 and week >= 3:
        return "短线"
    if above50 and month > 5 and d52 >= -15:
        return "中线"
    if above50 and -10 <= d52:
        return "中长线"
    return "短线"


def position_advice(p, action, port_value=10000.0, risk_pct=0.01):
    base_pct = ACTION_META[action]["size_pct"]
    if base_pct == 0:
        return {"size_pct_recommended": 0, "stop_price": None, "target_price": None, "n_shares_at_10k": 0}
    last = p.get("latest_price") or 0
    atr = p.get("atr_20") or 0
    if not last or not atr:
        return {"size_pct_recommended": base_pct, "stop_price": None, "target_price": None, "n_shares_at_10k": 0}
    stop_distance = 1.5 * atr
    target_distance = 3.0 * atr
    risk_budget = port_value * risk_pct
    risk_capped_pct = min(base_pct, (risk_budget / stop_distance * last) / port_value * 100)
    n_shares = int((port_value * risk_capped_pct / 100) / last)
    return {
        "size_pct_recommended": round(risk_capped_pct, 1),
        "stop_price": round(last - stop_distance, 2),
        "target_price": round(last + target_distance, 2),
        "n_shares_at_10k": n_shares,
    }


def apply_per_layer_cap(rows):
    """Downgrade STRONG_BUY to BUY if layer already has MAX_STRONG_BUY_PER_LAYER STRONG_BUYs."""
    layer_counts = {}
    for r in rows:  # rows already sorted desc by conviction
        if r["action"] != "STRONG_BUY":
            continue
        lk = r["layer"].split("·")[0]
        if layer_counts.get(lk, 0) >= MAX_STRONG_BUY_PER_LAYER:
            r["action"] = "BUY"
            r["size_class"] = ACTION_META["BUY"]["size_class"]
            r["downgrade_reason"] = f"layer {lk} cap reached"
        else:
            layer_counts[lk] = layer_counts.get(lk, 0) + 1
    return rows


def apply_priority_rank(rows):
    """Mark top N STRONG_BUY as PRIORITY (the actual picks to consider this week)."""
    strong = [r for r in rows if r["action"] == "STRONG_BUY"]
    for i, r in enumerate(strong, start=1):
        r["priority_rank"] = i
        r["is_priority"] = i <= PRIORITY_TOP_N
    return rows


def main():
    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    prices = json.loads(PRICES.read_text(encoding="utf-8"))
    pdata = prices.get("tickers", {})
    rm = regime_modifier(prices)

    rows = []
    for u in universe:
        t = u["ticker"]
        p = pdata.get(t)
        if not p:
            continue
        flags = detect_signals(p)
        signal_bonus = sum(SIGNAL_META[f]["weight"] for f in flags)

        m_score = 50 + (p.get("week_return_pct") or 0) * 1.5                   + (p.get("month_return_pct") or 0) * 0.8                   + (p.get("m3_return_pct") or 0) * 0.4                   + (p.get("rs_vs_spy_4w_pp") or 0) * 0.6
        if p.get("above_sma_20"): m_score += 5
        if p.get("above_sma_50"): m_score += 5
        m_score = max(0, min(100, m_score))
        f_score = (u.get("potential", 0) or 0) * 18 + {"龙头": 8, "二线": 4, "概念": 0}.get(u.get("role"), 0)
        f_score = min(100, f_score)
        comp = round(m_score * 0.6 + f_score * 0.4 + signal_bonus, 1)

        ts = round(trend_strength(p), 1)
        eq = round(entry_quality(p), 1)
        fc = round(fundamental_conviction(u, comp), 1)
        conv = conviction_score(ts, eq, fc, rm)

        action = map_action(conv, ts, eq, fc)
        horizon = map_horizon(p)
        pos = position_advice(p, action)

        rows.append({
            **u, **p,
            "signals": flags,
            "momentum_score": m_score,
            "fundamental_score": f_score,
            "composite_score": comp,
            "trend_strength": ts,
            "entry_quality": eq,
            "fund_conviction": fc,
            "regime_modifier": rm,
            "conviction_score": conv,
            "action": action,
            "size_class": ACTION_META[action]["size_class"],
            "size_pct_recommended": pos["size_pct_recommended"],
            "suggested_stop_price": pos["stop_price"],
            "suggested_target_price": pos["target_price"],
            "n_shares_at_10k": pos["n_shares_at_10k"],
            "holding_horizon": horizon,
            "horizon_days": HORIZON_META[horizon]["days"],
            "is_priority": False,
            "priority_rank": None,
        })

    rows.sort(key=lambda x: x["conviction_score"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i

    rows = apply_per_layer_cap(rows)
    rows = apply_priority_rank(rows)

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "spy_4w_return_pct": prices.get("spy_4w_return_pct"),
        "spy_above_sma50": prices.get("spy_above_sma50"),
        "regime_modifier": rm,
        "regime_label": (
            "强 risk-on" if rm >= 1.05 else
            "risk-on" if rm >= 0.95 else
            "弱多头" if rm >= 0.85 else
            "中性偏防御" if rm >= 0.6 else
            "risk-off"
        ),
        "is_mock": prices.get("is_mock", False),
        "signal_meta": SIGNAL_META,
        "action_meta": ACTION_META,
        "horizon_meta": HORIZON_META,
        "thresholds": {
            "STRONG_BUY": 85, "BUY": 72, "TRY_BUY": 58, "WATCH": 42,
            "strong_buy_floors": {"TS": 80, "EQ": 65},
            "max_strong_buy_per_layer": MAX_STRONG_BUY_PER_LAYER,
            "priority_top_n": PRIORITY_TOP_N,
        },
        "rows": rows,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(rows)} rows -> {OUT}")
    print(f"Regime modifier: {rm}  ({out['regime_label']})")

    actions = Counter(r["action"] for r in rows)
    print(f"\nAction distribution: {dict(actions)}")
    horizons = Counter(r["holding_horizon"] for r in rows)
    print(f"Horizon distribution: {dict(horizons)}")

    priority = [r for r in rows if r.get("is_priority")]
    print(f"\nPriority Top-{PRIORITY_TOP_N} (today's actual picks):")
    print(f"{'Pri':>3} {'Ticker':<6} {'Layer':<24} {'Conv':>5} {'TS':>4} {'EQ':>4} {'FC':>4} {'Size':>5}")
    for r in priority:
        print(f"{r['priority_rank']:>3} {r['ticker']:<6} {r['layer']:<24} {r['conviction_score']:>5} "
              f"{r['trend_strength']:>4} {r['entry_quality']:>4} {r['fund_conviction']:>4} "
              f"{r['size_pct_recommended']:>4}%")


if __name__ == "__main__":
    main()
