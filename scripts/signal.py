"""Combine fundamentals + price data → signals.json with strategy gates.

THE STRATEGY (StockChaser v2):
==============================
Three entry gates must ALL pass for a "READY" status:

  Gate 1 - Regime  : SPY above SMA50 (risk-on environment)
  Gate 2 - Startup : at least 2 of 3 must trigger
                     (a) breakout: latest >= 4-week high * 0.99
                     (b) week return >= +3%
                     (c) volume confirmation: vol_5/vol_20 >= 1.2
  Gate 3 - Quality : composite_score >= 80
                     AND rs_vs_spy_4w_pp >= 0
                     AND dist_52w_high_pct >= -25%

Output status per ticker (overall_status):
  READY    - all 3 gates pass → eligible to enter
  HOLD     - gates 2-3 pass, gate 1 (regime) fails → don't add but ok to hold
  WARN     - showing weakness (price < SMA20, or RS < -5pp)
  EXTENDED - extended ≥ +10% above SMA20 (avoid chasing)
  --       - none of the above (neutral)

Position sizing & stops (Minervini/Weinstein style):
  suggested_stop_price = latest - 1.5 * ATR_20    (initial hard stop)
  suggested_target_price = latest + 3.0 * ATR_20  (1:2 R:R take-profit zone)
  suggested_size_pct     = 1% / (1.5 * ATR / latest)  capped at 25% of portfolio
"""
import json
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "data" / "universe.json"
PRICES = ROOT / "data" / "prices.json"
OUT = ROOT / "data" / "signals.json"


# ----- Scoring -----
def momentum_score(p):
    s = 50.0
    s += (p.get("week_return_pct") or 0) * 1.5
    s += (p.get("month_return_pct") or 0) * 0.8
    s += (p.get("m3_return_pct") or 0) * 0.4
    s += (p.get("rs_vs_spy_4w_pp") or 0) * 0.6
    if p.get("above_sma_20"):
        s += 5
    if p.get("above_sma_50"):
        s += 5
    dist = p.get("dist_4w_high_pct") or 0
    if dist < -10:
        s += dist * 0.5
    return max(0, min(100, round(s, 1)))


def fundamental_score(u):
    s = (u.get("potential", 0) or 0) * 18
    role_bonus = {"龙头": 8, "二线": 4, "概念": 0}.get(u.get("role"), 0)
    return min(100, s + role_bonus)


# ----- Signal flags -----
SIGNAL_META = {
    "BREAKOUT":       {"emoji": "🚀", "label": "突破4周高",  "weight": 10, "buy_class": "buy"},
    "STRONG_DAY":     {"emoji": "📈", "label": "今日+3%",   "weight": 4,  "buy_class": "buy"},
    "STRONG_WEEK":    {"emoji": "📊", "label": "本周+3%",   "weight": 6,  "buy_class": "buy"},
    "VOLUME_SURGE":   {"emoji": "🔊", "label": "成交量1.2x", "weight": 5,  "buy_class": "buy"},
    "TREND_PULLBACK": {"emoji": "🎯", "label": "趋势回调",  "weight": 7,  "buy_class": "buy"},
    "ACCEL":          {"emoji": "🚂", "label": "多周期同向", "weight": 5,  "buy_class": "buy"},
    "RS_LEADER":      {"emoji": "💪", "label": "RS 领涨",   "weight": 5,  "buy_class": "buy"},
    "EXTENDED":       {"emoji": "⚠️", "label": "短期超买",   "weight": -3, "buy_class": "warn"},
    "BELOW_SMA20":    {"emoji": "🔻", "label": "跌破SMA20", "weight": -5, "buy_class": "warn"},
    "RS_LAG":         {"emoji": "🐢", "label": "RS 落后",   "weight": -3, "buy_class": "warn"},
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
    vol_ratio = p.get("vol_ratio_5_20")
    if vol_ratio and vol_ratio >= 1.2:
        flags.append("VOLUME_SURGE")
    dist = p.get("dist_4w_high_pct") or 0
    if p.get("above_sma_20") and -7 <= dist <= -3:
        flags.append("TREND_PULLBACK")
    if (p.get("week_return_pct") or 0) > 0 \
            and (p.get("month_return_pct") or 0) > 0 \
            and (p.get("m3_return_pct") or 0) > 0:
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


# ----- Three Gates -----
def evaluate_gates(p, composite, regime_pass):
    """Return (gate1_pass, gate2_pass, gate3_pass, gate_details)."""
    last = p.get("latest_price") or 0
    high_4w = p.get("high_4w") or 0
    week_ret = p.get("week_return_pct") or 0
    vol_ratio = p.get("vol_ratio_5_20") or 0
    rs = p.get("rs_vs_spy_4w_pp") or 0
    dist52 = p.get("dist_52w_high_pct") or 0

    g1 = regime_pass
    # Gate 2: 2 of 3 startup signals
    s2_a = bool(high_4w and last >= high_4w * 0.99)
    s2_b = week_ret >= 3
    s2_c = vol_ratio >= 1.2
    g2 = sum([s2_a, s2_b, s2_c]) >= 2
    # Gate 3: quality
    g3 = composite >= 80 and rs >= 0 and dist52 >= -25
    return {
        "g1_regime": g1,
        "g2_startup": g2,
        "g2_breakout": s2_a,
        "g2_week_3pct": s2_b,
        "g2_vol_surge": s2_c,
        "g3_quality": g3,
    }


def status_label(gates, signals):
    if "BELOW_SMA20" in signals or "RS_LAG" in signals:
        return "WARN"
    if "EXTENDED" in signals:
        return "EXTENDED"
    if gates["g1_regime"] and gates["g2_startup"] and gates["g3_quality"]:
        return "READY"
    if gates["g2_startup"] and gates["g3_quality"]:
        return "HOLD"
    return "—"


STATUS_META = {
    "READY":    {"label": "可入场",   "color": "#3fb950"},
    "HOLD":     {"label": "持有/等",  "color": "#58a6ff"},
    "WARN":     {"label": "警示",     "color": "#f39c12"},
    "EXTENDED": {"label": "超买",     "color": "#f39c12"},
    "—":        {"label": "中性",     "color": "#8b949e"},
}


# ----- Position sizing -----
def position_advice(p, port_value=10000.0, risk_pct=0.01, max_pos_pct=0.25):
    last = p.get("latest_price") or 0
    atr = p.get("atr_20") or 0
    if not (last and atr):
        return {"stop_price": None, "target_price": None, "size_pct": None}
    stop_dist = 1.5 * atr
    target_dist = 3.0 * atr
    risk_per_share = stop_dist
    risk_budget = port_value * risk_pct
    shares = risk_budget / risk_per_share
    notional = shares * last
    size_pct = min(notional / port_value, max_pos_pct)
    return {
        "stop_price": round(last - stop_dist, 2),
        "target_price": round(last + target_dist, 2),
        "size_pct": round(size_pct * 100, 1),
    }


# ----- Main -----
def main():
    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    prices = json.loads(PRICES.read_text(encoding="utf-8"))
    pdata = prices.get("tickers", {})
    regime_pass = bool(prices.get("spy_above_sma50", False))

    rows = []
    for u in universe:
        t = u["ticker"]
        p = pdata.get(t)
        if not p:
            continue
        flags = detect_signals(p)
        signal_bonus = sum(SIGNAL_META[f]["weight"] for f in flags)

        m = momentum_score(p)
        f = fundamental_score(u)
        composite = round(m * 0.6 + f * 0.4 + signal_bonus, 1)

        gates = evaluate_gates(p, composite, regime_pass)
        status = status_label(gates, flags)
        size = position_advice(p)

        rows.append({
            **u, **p,
            "signals": flags,
            "momentum_score": m,
            "fundamental_score": f,
            "composite_score": composite,
            "gates": gates,
            "overall_status": status,
            "suggested_stop_price": size["stop_price"],
            "suggested_target_price": size["target_price"],
            "suggested_size_pct": size["size_pct"],
        })

    rows.sort(key=lambda x: x["composite_score"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i

    per_layer = {}
    for r in rows:
        per_layer.setdefault(r["layer"], []).append(r)
    for ls in per_layer.values():
        ls.sort(key=lambda x: x["composite_score"], reverse=True)
        for i, r in enumerate(ls, start=1):
            r["layer_rank"] = i

    out = {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "spy_4w_return_pct": prices.get("spy_4w_return_pct"),
        "spy_above_sma50": regime_pass,
        "is_mock": prices.get("is_mock", False),
        "signal_meta": SIGNAL_META,
        "status_meta": STATUS_META,
        "rows": rows,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(rows)} rows -> {OUT}")
    print(f"Regime (SPY > SMA50): {regime_pass}")

    # Status breakdown
    from collections import Counter
    statuses = Counter(r["overall_status"] for r in rows)
    print(f"\nStatus breakdown: {dict(statuses)}")

    # Top 10 READY
    ready = [r for r in rows if r["overall_status"] == "READY"]
    print(f"\nTop READY ({len(ready)} 只):")
    print(f"{'Rk':>3} {'Ticker':<6} {'Layer':<28} {'Comp':>6} {'Stop':>7} {'Target':>7} {'Size':>5}")
    for r in ready[:10]:
        print(f"{r['rank']:>3} {r['ticker']:<6} {r['layer']:<28} {r['composite_score']:>6} "
              f"${r['suggested_stop_price']:>6} ${r['suggested_target_price']:>6} {r['suggested_size_pct']:>4}%")


if __name__ == "__main__":
    main()
