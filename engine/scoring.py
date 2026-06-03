"""Three-axis scoring + signal detection.

Conviction = (TS × 0.40 + EQ × 0.30 + FC × 0.30) × Regime
  TS  Trend Strength    (40%)  position vs MA / 4W high / RS / 6M-Mom
  EQ  Entry Quality     (30%)  weekly bracket / volume / ATR / breakout reward
  FC  Fundamental       (30%)  potential × role + layer/new-node bonuses
"""
from __future__ import annotations

# ── Signal flags + UI meta ───────────────────────────────────────────────
SIGNAL_META = {
    "BREAKOUT":       {"emoji": "\U0001f680", "label": "突破4周高",  "weight": 10, "buy_class": "buy"},
    "STRONG_DAY":     {"emoji": "\U0001f4c8", "label": "今日+3%",    "weight": 4,  "buy_class": "buy"},
    "STRONG_WEEK":    {"emoji": "\U0001f4ca", "label": "本周+3%",    "weight": 6,  "buy_class": "buy"},
    "VOLUME_SURGE":   {"emoji": "\U0001f50a", "label": "成交量1.2x", "weight": 5,  "buy_class": "buy"},
    "TREND_PULLBACK": {"emoji": "\U0001f3af", "label": "趋势回调",   "weight": 7,  "buy_class": "buy"},
    "ACCEL":          {"emoji": "\U0001f682", "label": "多周期同向", "weight": 5,  "buy_class": "buy"},
    "RS_LEADER":      {"emoji": "\U0001f4aa", "label": "RS 领涨",    "weight": 5,  "buy_class": "buy"},
    "MOM_6M":         {"emoji": "\U0001f4c5", "label": "6月月均>3%", "weight": 6,  "buy_class": "buy"},
    "EXTENDED":       {"emoji": "⚠️", "label": "短期超买", "weight": -3, "buy_class": "warn"},
    "BELOW_SMA20":    {"emoji": "\U0001f53b", "label": "跌破SMA20",  "weight": -5, "buy_class": "warn"},
    "RS_LAG":         {"emoji": "\U0001f422", "label": "RS 落后",    "weight": -3, "buy_class": "warn"},
}


def detect_signals(p):
    """Return list of signal flag codes for one price row."""
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
    if ((p.get("week_return_pct") or 0) > 0
            and (p.get("month_return_pct") or 0) > 0
            and (p.get("m3_return_pct") or 0) > 0):
        flags.append("ACCEL")
    if (p.get("rs_vs_spy_4w_pp") or 0) >= 5:
        flags.append("RS_LEADER")
    if (p.get("avg_monthly_6m_pct") or 0) >= 3:
        flags.append("MOM_6M")
    sma20 = p.get("sma_20") or 0
    if sma20 and last >= sma20 * 1.10:
        flags.append("EXTENDED")
    if sma20 and last < sma20:
        flags.append("BELOW_SMA20")
    if (p.get("rs_vs_spy_4w_pp") or 0) <= -5:
        flags.append("RS_LAG")
    return flags


# ── TS / EQ / FC / Conviction ────────────────────────────────────────────

def trend_strength(p):
    """0–100. Position vs MA + 4W high distance + RS + multi-tf alignment + 6M-Mom."""
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
    # v3.2 — 6-month average monthly momentum
    avg6m = p.get("avg_monthly_6m_pct")
    if avg6m is not None:
        if avg6m >= 3: s += 10
        elif avg6m >= 1.5: s += 6
        elif avg6m >= 0: s += 2
        elif avg6m <= -2: s -= 10
        elif avg6m < 0: s -= 4
    return max(0, min(100, s))


def entry_quality(p):
    """0–100. v3.6 anti-martingale friendly: rewards breakouts, no over-extension penalty."""
    s = 50
    week = p.get("week_return_pct") or 0
    if 3 <= week <= 12: s += 18
    elif week > 12:     s += 8
    elif 0 <= week < 3: s += 4
    elif week < -3:     s -= 12
    vr = p.get("vol_ratio_5_20") or 1
    if vr >= 1.4: s += 12
    elif vr >= 1.2: s += 8
    elif vr < 0.7: s -= 6
    atr = p.get("atr_pct")
    if atr is not None:
        if atr > 10:    s -= 8
        elif atr > 7:   s -= 3
        elif atr < 1.5: s -= 4
    # v3.6: reward breakouts (was penalized in v3.5)
    d4 = p.get("dist_4w_high_pct") or 0
    if d4 >= 0:    s += 10
    elif d4 >= -3: s += 5
    return max(0, min(100, s))


def fundamental_conviction(u, comp_score, downstream_bonus, new_node_bonus):
    """0–100. Potential × role + layer (F1) + new-node (F3) bonuses + composite spillover."""
    pot_pts = (u.get("potential", 0) or 0) * 12
    role_bonus = {"龙头": 12, "二线": 6, "概念": 0}.get(u.get("role"), 0)
    s = pot_pts + role_bonus + downstream_bonus + new_node_bonus + max(0, comp_score - 70) * 0.3
    return max(0, min(100, s))


def conviction_score(ts, eq, fc, rm):
    """Composite conviction (0–100) scaled by regime modifier."""
    base = ts * 0.4 + eq * 0.3 + fc * 0.3
    return round(min(100, max(0, base * rm)), 1)


def momentum_composite(p, signal_bonus, u):
    """Legacy composite score (used as input to FC bonus)."""
    m = 50 + (p.get("week_return_pct") or 0) * 1.5 \
          + (p.get("month_return_pct") or 0) * 0.8 \
          + (p.get("m3_return_pct") or 0) * 0.4 \
          + (p.get("rs_vs_spy_4w_pp") or 0) * 0.6
    if p.get("above_sma_20"): m += 5
    if p.get("above_sma_50"): m += 5
    m = max(0, min(100, m))
    f = (u.get("potential", 0) or 0) * 18 + {"龙头": 8, "二线": 4, "概念": 0}.get(u.get("role"), 0)
    f = min(100, f)
    return round(m * 0.6 + f * 0.4 + signal_bonus, 1), m, f
