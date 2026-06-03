"""Action mapping, PRIORITY ranking, position sizing.

v3.6:
  STRONG_BUY  Conv >= 82 AND TS >= 78   (EQ floor removed — was negative alpha)
  BUY         Conv >= 70 AND TS >= 65
  TRY_BUY     Conv >= 55
  WATCH       Conv >= 40
  AVOID       otherwise

PRIORITY:
  Pool = STRONG_BUY ∪ BUY (was: STRONG_BUY only)
  Sort = 6M-Mom DESC + sticky 2pp/month bonus for prev priority
  Top N = 8
"""
from __future__ import annotations

MAX_STRONG_BUY_PER_LAYER = 2
PRIORITY_TOP_N = 8
PRIORITY_SOFT_TOP_N = 12        # 软退出窗口: 老 priority 在 top 12 就保留
PRIORITY_EXIT_CONV = 60          # 老 priority 跌破此 conv 即放出去
PRIORITY_MIN_TENURE_DAYS = 7     # 入 priority 后最短任期 (除非 conv 暴跌)
PRIORITY_HARD_EXIT_CONV = 45     # 跌破此值无视最短任期立即退出
STICKY_MOM_BONUS = 5.0           # priority hysteresis: +5pp/月 (v3.7 加大稳定性)

ACTION_META = {
    "STRONG_BUY":  {"emoji": "\U0001f525", "label": "强力买入", "color": "#d63031", "size_class": "重仓", "size_pct": 10.0},
    "BUY":         {"emoji": "\U0001f7e2", "label": "买入",     "color": "#3fb950", "size_class": "中仓", "size_pct": 7.0},
    "TRY_BUY":     {"emoji": "\U0001f7e1", "label": "试仓",     "color": "#f1c40f", "size_class": "小仓", "size_pct": 4.0},
    "WATCH":       {"emoji": "\U0001f440", "label": "观察",     "color": "#8b949e", "size_class": "观察", "size_pct": 0.0},
    "AVOID":       {"emoji": "⚪", "label": "回避",     "color": "#8b949e", "size_class": "回避", "size_pct": 0.0},
}

HORIZON_META = {
    "短线":   {"emoji": "⚡", "days": "1-3 周",   "desc": "事件驱动型动量"},
    "中线":   {"emoji": "\U0001f4c8", "days": "2-6 周",   "desc": "趋势中段"},
    "中长线": {"emoji": "\U0001f3d4", "days": "1-3 月",   "desc": "base building 完成"},
}


def map_action(conviction, ts, eq, fc):
    """Translate Conv score to action tag. EQ floor removed in v3.6."""
    if conviction >= 82 and ts >= 78:
        return "STRONG_BUY"
    if conviction >= 70 and ts >= 65:
        return "BUY"
    if conviction >= 55:
        return "TRY_BUY"
    if conviction >= 40:
        return "WATCH"
    return "AVOID"


def map_horizon(p):
    """Classify holding horizon based on momentum + base structure."""
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
    """ATR-aware position sizing recommendation."""
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
    """Limit STRONG_BUY count per industry layer to MAX_STRONG_BUY_PER_LAYER."""
    layer_counts = {}
    for r in rows:
        if r["action"] != "STRONG_BUY":
            continue
        lk = (r.get("layer") or "").split("·")[0]
        if layer_counts.get(lk, 0) >= MAX_STRONG_BUY_PER_LAYER:
            r["action"] = "BUY"
            r["size_class"] = ACTION_META["BUY"]["size_class"]
            r["downgrade_reason"] = f"layer {lk} cap reached"
        else:
            layer_counts[lk] = layer_counts.get(lk, 0) + 1
    return rows


def apply_priority_rank(rows, prev_priority_set=None, priority_history=None, today_str=None):
    """v3.7: Priority 稳定性增强.

    规则 (优先级从高到低):
      1. **Hard exit**  conv < 45 → 立即出 priority (无视任期)
      2. **Min tenure** 入 priority 不到 7 天 + conv >= 45 → 强制保留
      3. **Soft exit**  老 priority 在 top 12 内 → 保留 (sticky 5pp mom 加成)
      4. **Fresh entry** Top 8 (按 mom DESC 排序) → 新进 priority

    Args:
        rows: 已 score 的 ticker 行
        prev_priority_set: 上次 is_priority=True 的 ticker 集合
        priority_history: {ticker: {first_entry_date: 'YYYY-MM-DD'}} 用于算任期
                          (run() 负责 load/save 这个字典)
        today_str: 今天日期字符串, 用于计算 days_in_priority
    """
    from datetime import datetime, timezone

    prev_priority_set = prev_priority_set or set()
    priority_history = priority_history if priority_history is not None else {}
    today_str = today_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _mom_key(r, is_sticky):
        mom = r.get("avg_monthly_6m_pct") or 0
        if is_sticky:
            mom += STICKY_MOM_BONUS
        return (-mom, -r.get("conviction_score", 0))

    def _days_in_priority(ticker):
        rec = priority_history.get(ticker)
        if not rec or not rec.get("first_entry_date"):
            return 0
        try:
            first = datetime.strptime(rec["first_entry_date"], "%Y-%m-%d")
            today = datetime.strptime(today_str, "%Y-%m-%d")
            return max(0, (today - first).days)
        except ValueError:
            return 0

    # Step 1: 池子 = 所有 BUY+ + 老 priority 仍能保留的
    eligible = []
    seen = set()
    for r in rows:
        is_old = r["ticker"] in prev_priority_set
        conv = r["conviction_score"]
        if is_old and r["action"] in ("STRONG_BUY", "BUY"):
            if conv >= PRIORITY_HARD_EXIT_CONV:
                eligible.append((r, True))
                seen.add(r["ticker"])
                continue
        if is_old and conv < PRIORITY_HARD_EXIT_CONV:
            continue
    for r in rows:
        if r["action"] in ("STRONG_BUY", "BUY") and r["ticker"] not in seen:
            eligible.append((r, False))
            seen.add(r["ticker"])
    eligible.sort(key=lambda x: _mom_key(x[0], x[1]))

    # Step 2: 决定 is_priority
    locked_in_priority = set()
    for r in rows:
        if r["ticker"] in prev_priority_set:
            days = _days_in_priority(r["ticker"])
            if days < PRIORITY_MIN_TENURE_DAYS and r["conviction_score"] >= PRIORITY_HARD_EXIT_CONV:
                locked_in_priority.add(r["ticker"])

    is_priority_set = set(locked_in_priority)

    for i, (r, was_priority) in enumerate(eligible, start=1):
        r["priority_rank"] = i
        if was_priority and i <= PRIORITY_SOFT_TOP_N:
            is_priority_set.add(r["ticker"])
        elif i <= PRIORITY_TOP_N:
            is_priority_set.add(r["ticker"])

    for r in rows:
        if r.get("priority_rank") is None:
            continue
        is_pri = r["ticker"] in is_priority_set
        r["is_priority"] = is_pri
        r["priority_sticky"] = r["ticker"] in prev_priority_set
        days = _days_in_priority(r["ticker"])
        r["days_in_priority"] = days
        r["priority_min_tenure_lock"] = (r["ticker"] in locked_in_priority)

    for r in rows:
        if r.get("is_priority"):
            if r["ticker"] not in priority_history:
                priority_history[r["ticker"]] = {"first_entry_date": today_str}
            elif not priority_history[r["ticker"]].get("first_entry_date"):
                priority_history[r["ticker"]]["first_entry_date"] = today_str
        else:
            priority_history.pop(r["ticker"], None)

    rank = len(eligible) + 1
    for r in rows:
        if r.get("priority_rank") is None and r["action"] == "STRONG_BUY":
            r["priority_rank"] = rank
            r["is_priority"] = False
            r["priority_sticky"] = False
            r["days_in_priority"] = 0
            r["priority_min_tenure_lock"] = False
            rank += 1
    return rows
