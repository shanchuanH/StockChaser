"""StockChaser v3.2 — Conviction Engine with Three Frameworks.

Adds to v3.1:
  - 6-month average monthly momentum integrated into Trend Strength
    (the article's "六月均值动量" — the math problem)
  - Framework tags per stock (why this stock is on the list):
      F1 · 下游传导   downstream lag thesis (12-18mo money flow)
      F2 · 财报真空   earnings vacuum reversal (long base + still healthy)
      F3 · 新节点     new architecture position (凭空多出的位置)
      6M-MOM          6-month avg monthly return ≥ 3% (clean LT momentum)
  - Per-stock framework_tags array surfaced to UI
"""
import json
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "data" / "universe.json"
PRICES = ROOT / "data" / "prices.json"
OUT = ROOT / "data" / "signals.json"

# Optional playbook generator (傻瓜式 操作手册)
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from playbook import annotate_rows as _annotate_playbooks, load_config as _load_pb_config
except ImportError:
    _annotate_playbooks = None
    _load_pb_config = lambda: {}

MAX_STRONG_BUY_PER_LAYER = 2
PRIORITY_TOP_N = 8

# Framework 3 — explicit "new node" whitelist
NEW_NODE_TICKERS = {
    "SNDK": "NAND 独立后凭空多出的纯 NAND 龙头",
    "ALAB": "GPU 机柜密度催生的 PCIe Retimer 新位置",
    "AAOI": "800G 光模块爬坡过程的新供应商",
    "CRDO": "AEC 有源铜缆 — 替代部分光模块的新位置",
    "CRWV": "Neocloud — hyperscaler 之外的 GPU 出租新位置",
    "IREN": "矿改算力 — 利用闲置电力的新位置",
    "MTSI": "TIA/Driver — 800G 光模块带动的新增长点",
    "GLW":  "CPO 玻璃光纤的新需求位置",
    "CAMT": "HBM 检测设备 — 凭空多出的设备位置",
    "TTMI": "AI 高端 PCB — 复杂度跳级的新位置",
    "AXTI": "InP 衬底卡脖子 — 光模块上游的关键位置",
    "POET": "硅光集成 — CPO 时代的新平台",
    "BE":   "燃料电池 — IDC 现场发电的新位置",
    "OKLO": "微堆 — SMR 之外的新核电形态",
    "NBIS": "欧洲 Neocloud — 区域 GPU 出租新位置",
    "APLD": "HPC 托管 IDC — 矿改资源的新位置",
}

# Framework 1 — downstream layers (12-18 mo lag, money flows here LATE)
DOWNSTREAM_LAYER_BONUS = {
    "L10": 5,  # 电力配套
    "L11": 5,  # 核电/SMR
    "L12": 4,  # 水电/可再生
    "L9":  3,  # 半导体底座 (设备+材料)
    "L6":  3,  # 光通信 (持续传导期)
    "L13": 3,  # 燃料电池/储能
}


SIGNAL_META = {
    "BREAKOUT":       {"emoji": "\U0001f680", "label": "突破4周高",  "weight": 10, "buy_class": "buy"},
    "STRONG_DAY":     {"emoji": "\U0001f4c8", "label": "今日+3%",   "weight": 4,  "buy_class": "buy"},
    "STRONG_WEEK":    {"emoji": "\U0001f4ca", "label": "本周+3%",   "weight": 6,  "buy_class": "buy"},
    "VOLUME_SURGE":   {"emoji": "\U0001f50a", "label": "成交量1.2x", "weight": 5,  "buy_class": "buy"},
    "TREND_PULLBACK": {"emoji": "\U0001f3af", "label": "趋势回调",  "weight": 7,  "buy_class": "buy"},
    "ACCEL":          {"emoji": "\U0001f682", "label": "多周期同向", "weight": 5,  "buy_class": "buy"},
    "RS_LEADER":      {"emoji": "\U0001f4aa", "label": "RS 领涨",   "weight": 5,  "buy_class": "buy"},
    "MOM_6M":         {"emoji": "\U0001f4c5", "label": "6月月均>3%", "weight": 6,  "buy_class": "buy"},
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

FRAMEWORK_META = {
    "F1": {"emoji": "\U0001f30a", "label": "下游传导",  "color": "#3498db", "desc": "12-18 月资金传导期"},
    "F2": {"emoji": "\U0001f9ee", "label": "财报真空",  "color": "#9b59b6", "desc": "长底+健康，黎明前"},
    "F3": {"emoji": "\U0001f4a1", "label": "新节点",    "color": "#e67e22", "desc": "凭空多出来的位置"},
    "6M": {"emoji": "\U0001f4c5", "label": "6月均值",   "color": "#16a085", "desc": "6月月均≥3%"},
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
    # NEW v3.2: 6-month average monthly momentum integration
    avg6m = p.get("avg_monthly_6m_pct")
    if avg6m is not None:
        if avg6m >= 3: s += 10
        elif avg6m >= 1.5: s += 6
        elif avg6m >= 0: s += 2
        elif avg6m <= -2: s -= 10
        elif avg6m < 0: s -= 4
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


def fundamental_conviction(u, comp_score, framework_tags):
    pot_pts = (u.get("potential", 0) or 0) * 12
    role_bonus = {"龙头": 12, "二线": 6, "概念": 0}.get(u.get("role"), 0)
    layer_key = u.get("layer", "").split("·")[0]
    # Framework 1 — downstream lag bonus
    leader_bonus = DOWNSTREAM_LAYER_BONUS.get(layer_key, 0)
    # Framework 3 — new node bonus
    new_node_bonus = 5 if u["ticker"] in NEW_NODE_TICKERS else 0
    s = pot_pts + role_bonus + leader_bonus + new_node_bonus + max(0, comp_score - 70) * 0.3
    return max(0, min(100, s))


def detect_framework_tags(u, p, comp_score, conviction):
    """Return list of {"code","reason"} for which frameworks this stock fits."""
    tags = []
    t = u["ticker"]
    layer_key = u.get("layer", "").split("·")[0]

    # F1 · downstream lag
    if layer_key in ("L10", "L11", "L12") and conviction >= 50:
        tags.append({"code": "F1", "reason": f"{u['layer']} 在 12-18 月传导期, 综合分 {comp_score:.0f}"})

    # F3 · new node (whitelist)
    if t in NEW_NODE_TICKERS:
        tags.append({"code": "F3", "reason": NEW_NODE_TICKERS[t]})

    # F2 · earnings vacuum reversal candidates
    # heuristic: distance 52w high in [-30%, -10%] AND above SMA50 AND comp >= 70
    d52 = p.get("dist_52w_high_pct") or 0
    if (-30 <= d52 <= -10
            and p.get("above_sma_50")
            and comp_score >= 70):
        tags.append({"code": "F2", "reason": f"距 52W 高 {d52:.0f}%, 在 SMA50 上, 综合分 {comp_score:.0f}"})

    # 6M-Mom · clean long-term momentum
    avg6m = p.get("avg_monthly_6m_pct")
    if avg6m is not None and avg6m >= 3:
        tags.append({"code": "6M", "reason": f"6 月月均 {avg6m:.1f}%/月"})

    return tags


def regime_modifier(prices_root):
    """v3.5: SMA200 acts as hard bull/bear cap.

    - SPY < SMA200 (bear regime)  → max 0.50 modifier, often 0.20-0.35
    - SPY > SMA200 (bull regime)  → SMA50 nuances normally
    """
    spy_above_50 = bool(prices_root.get("spy_above_sma50"))
    # default True for back-compat when older signals.json lacks the field
    spy_above_200 = bool(prices_root.get("spy_above_sma200", True))
    spy_4w = prices_root.get("spy_4w_return_pct") or 0
    spy_dist_200 = prices_root.get("spy_dist_sma200_pct") or 0

    # BEAR regime — hard cap
    if not spy_above_200:
        if spy_dist_200 < -10 or spy_4w < -8: return 0.20   # accelerating bear
        if spy_4w < -3:                       return 0.30   # bear + falling
        return 0.50                                          # bear but stable

    # BULL regime — graded by SMA50 + momentum
    if spy_above_50 and spy_4w >= 4:    return 1.10
    if spy_above_50 and spy_4w >= 1:    return 1.00
    if spy_above_50:                     return 0.90
    if -3 <= spy_4w < 0:                 return 0.75
    if spy_4w < -5:                      return 0.55
    return 0.65


def conviction_score(ts, eq, fc, rm):
    base = ts * 0.4 + eq * 0.3 + fc * 0.3
    return round(min(100, max(0, base * rm)), 1)


def map_action(conviction, ts, eq, fc):
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
    layer_counts = {}
    for r in rows:
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


def apply_priority_rank(rows, prev_priority_set=None):
    """Assign PRIORITY ranks with optional hysteresis from prior run.

    If a ticker was in last week's PRIORITY and its action is still BUY+ AND
    conviction >= PRIORITY_EXIT_THRESHOLD (60), keep it in PRIORITY ahead of
    fresh STRONG_BUY candidates. This dampens week-to-week churn.
    """
    PRIORITY_EXIT_CONV = 60  # held priority kept while conv >= this
    prev_priority_set = prev_priority_set or set()
    # Eligible pool: STRONG_BUY ∪ (prev_priority still BUY+ with conv >= exit)
    eligible = []
    seen = set()
    # Start with prior priority tickers that still qualify (sticky)
    for r in rows:
        if r["ticker"] in prev_priority_set \
                and r["action"] in ("STRONG_BUY", "BUY") \
                and r["conviction_score"] >= PRIORITY_EXIT_CONV:
            eligible.append((r, True))
            seen.add(r["ticker"])
    # Then fresh STRONG_BUY (not already in sticky)
    for r in rows:
        if r["action"] == "STRONG_BUY" and r["ticker"] not in seen:
            eligible.append((r, False))
            seen.add(r["ticker"])
    # Sort sticky-first, then by conviction desc within group
    eligible.sort(key=lambda x: (not x[1], -x[0]["conviction_score"]))
    for i, (r, was_priority) in enumerate(eligible, start=1):
        r["priority_rank"] = i
        r["is_priority"] = i <= PRIORITY_TOP_N
        r["priority_sticky"] = was_priority
    # Strong_buy not in priority should still have rank
    rank = len(eligible) + 1
    for r in rows:
        if r.get("priority_rank") is None and r["action"] == "STRONG_BUY":
            r["priority_rank"] = rank
            r["is_priority"] = False
            r["priority_sticky"] = False
            rank += 1
    return rows


def load_prior_priority():
    """Read prior signals.json (if exists) and return set of last priority tickers."""
    if not OUT.exists():
        return set()
    try:
        prior = json.loads(OUT.read_text(encoding="utf-8"))
        return {r["ticker"] for r in prior.get("rows", []) if r.get("is_priority")}
    except Exception:
        return set()


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

        m_score = 50 + (p.get("week_return_pct") or 0) * 1.5 \
                  + (p.get("month_return_pct") or 0) * 0.8 \
                  + (p.get("m3_return_pct") or 0) * 0.4 \
                  + (p.get("rs_vs_spy_4w_pp") or 0) * 0.6
        if p.get("above_sma_20"): m_score += 5
        if p.get("above_sma_50"): m_score += 5
        m_score = max(0, min(100, m_score))
        f_score = (u.get("potential", 0) or 0) * 18 + {"龙头": 8, "二线": 4, "概念": 0}.get(u.get("role"), 0)
        f_score = min(100, f_score)
        comp = round(m_score * 0.6 + f_score * 0.4 + signal_bonus, 1)

        ts = round(trend_strength(p), 1)
        eq = round(entry_quality(p), 1)

        # Detect frameworks BEFORE FC (FC uses framework membership for bonus)
        # We compute a preliminary conviction here for tag heuristics
        fc_prelim = (u.get("potential", 0) or 0) * 12 + {"龙头": 12, "二线": 6}.get(u.get("role"), 0)
        prelim_conv = (ts * 0.4 + eq * 0.3 + fc_prelim * 0.3) * rm
        framework_tags = detect_framework_tags(u, p, comp, prelim_conv)

        fc = round(fundamental_conviction(u, comp, framework_tags), 1)
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
            "framework_tags": framework_tags,
            "is_priority": False,
            "priority_rank": None,
        })

    rows.sort(key=lambda x: x["conviction_score"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i

    rows = apply_per_layer_cap(rows)
    prev_priority = load_prior_priority()  # hysteresis carry-over
    rows = apply_priority_rank(rows, prev_priority_set=prev_priority)

    # Attach personalized playbook (傻瓜式 操作手册) for actionable signals
    pb_config = None
    if _annotate_playbooks is not None:
        try:
            pb_config = _load_pb_config()
            _annotate_playbooks(rows, pb_config)
        except Exception as e:
            print(f"  ! playbook generation failed: {e}")

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
        "engine_version": "v3.5",
        "extra_prices": prices.get("extra_prices", {}),
        "signal_meta": SIGNAL_META,
        "action_meta": ACTION_META,
        "horizon_meta": HORIZON_META,
        "framework_meta": FRAMEWORK_META,
        "thresholds": {
            "STRONG_BUY": 85, "BUY": 72, "TRY_BUY": 58, "WATCH": 42,
            "strong_buy_floors": {"TS": 80, "EQ": 65},
            "max_strong_buy_per_layer": MAX_STRONG_BUY_PER_LAYER,
            "priority_top_n": PRIORITY_TOP_N,
        },
        "portfolio_config": pb_config or {},
        "rows": rows,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- Append to conviction_history.json (for portfolio sparklines) ----
    hist_path = ROOT / "data" / "conviction_history.json"
    try:
        hist = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {}
    except Exception:
        hist = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for r in rows:
        t = r["ticker"]
        series = hist.setdefault(t, [])
        # Don't append duplicate entries on the same day
        if series and series[-1].get("date") == today:
            series[-1] = {"date": today, "conv": r["conviction_score"], "action": r["action"]}
        else:
            series.append({"date": today, "conv": r["conviction_score"], "action": r["action"]})
        # Cap each series at 90 entries (~3 months daily, longer if weekly)
        if len(series) > 90:
            hist[t] = series[-90:]
    hist_path.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"v3.5 wrote {len(rows)} rows -> {OUT}")
    print(f"Regime modifier: {rm}  ({out['regime_label']})")

    actions = Counter(r["action"] for r in rows)
    print(f"\nAction distribution: {dict(actions)}")
    framework_counter = Counter()
    for r in rows:
        for tag in r["framework_tags"]:
            framework_counter[tag["code"]] += 1
    print(f"Framework hits: {dict(framework_counter)}")

    priority = [r for r in rows if r.get("is_priority")]
    print(f"\nPriority Top-{PRIORITY_TOP_N}:")
    print(f"{'Pri':>3} {'Ticker':<6} {'Layer':<24} {'Conv':>5} {'TS':>4} {'EQ':>4} {'FC':>4} Tags")
    for r in priority:
        tags_str = " ".join(t["code"] for t in r["framework_tags"])
        print(f"{r['priority_rank']:>3} {r['ticker']:<6} {r['layer']:<24} {r['conviction_score']:>5} "
              f"{r['trend_strength']:>4} {r['entry_quality']:>4} {r['fund_conviction']:>4} {tags_str}")


if __name__ == "__main__":
    main()
