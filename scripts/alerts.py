"""Persistent alert engine — detects trigger events and persists them.

v3.7 alerts (out-going principle: "if I wouldn't buy this as a new buyer, sell"):
  Hard triggers (always active, anti-martingale holdings):
    flash_8        single-day return  -8%  -> reduce to 30%
    stop_8         cumulative  -8%         -> close all
    conv_break     Conv  <  45              -> reduce 50%
    would_not_buy  action in (WATCH, AVOID) and held >=2w -> close all
                   (Conv  <  55 = would not initiate new position today)

  Decay triggers (regime-gated, anti-martingale only):
    growth_decay   6M-Mom  <  1%/mo or dropped 50% from entry (>=4w held) -> close all

Regime gating:
  rm >= 1.0  -> all triggers active
  rm 0.6-1.0 -> growth_decay still active (it is the "you would not buy" detector for momentum)
  rm  <  0.6  -> all decay triggers off (HWM stops + base regime sizing handle it)
  Hard triggers and would_not_buy are NEVER gated (absolute rules).

Active-list behavior:
  - Filter dismissed + snoozed
  - Auto-revalidate trigger conditions on read (stale alerts disappear)
  - Dedupe by (ticker, type) - only most recent shows
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIGNALS = ROOT / "data" / "signals.json"
HOLDINGS = ROOT / "data" / "my_holdings.json"
HISTORY_CSV = ROOT / "data" / "history.csv"
PENDING = ROOT / "data" / "pending_alerts.json"


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_json(p, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _hwm_per_ticker(holdings):
    """Read history.csv and find max close since buy_date for each held ticker."""
    out = {}
    if not HISTORY_CSV.exists():
        return out
    try:
        import csv
        rows_by_t = {}
        with HISTORY_CSV.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                t = row.get("ticker")
                if t not in holdings:
                    continue
                d = row.get("date")
                try:
                    c = float(row.get("close") or 0)
                except ValueError:
                    continue
                rows_by_t.setdefault(t, []).append((d, c))
        for t, h in holdings.items():
            buy_date = h.get("buy_date", "")
            arr = rows_by_t.get(t, [])
            since = [c for d, c in arr if d >= buy_date]
            if since:
                out[t] = max(since)
    except Exception as e:
        print(f"  hwm calc failed: {e}")
    return out


def _hold_weeks(h):
    """Floor weeks held since buy_date."""
    bd = h.get("buy_date") or ""
    if not bd:
        return 0
    try:
        bd_dt = datetime.strptime(bd[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - bd_dt).days
        return max(0, days // 7)
    except (ValueError, TypeError):
        return 0


def detect_and_persist():
    """Main entry — scan signals + holdings, append new alerts to PENDING."""
    sig = _load_json(SIGNALS, None)
    if not sig:
        print("alerts: signals.json missing/empty")
        return 0
    holdings = _load_json(HOLDINGS, {})
    if not holdings:
        print("alerts: no holdings to scan")
        return 0
    rows_by_t = {r["ticker"]: r for r in sig.get("rows", [])}
    extras = sig.get("extra_prices", {})
    today = _today_utc()

    # v3.6: regime + VIX 决定衰减触发是否启用 / 是否激进
    regime_mod = sig.get("regime_modifier") or 1.0
    vix_close = sig.get("vix_close") or 0
    is_black_swan = vix_close >= 30

    pending = _load_json(PENDING, {})
    existing_ids = set()
    for ticker_list in pending.values():
        for a in ticker_list:
            existing_ids.add(a.get("id"))

    new_alerts = []

    for t, h in holdings.items():
        r = rows_by_t.get(t)
        extra = extras.get(t)
        if r:
            px = r.get("latest_price")
            daily = r.get("daily_return_pct") or 0
            conv = r.get("conviction_score") or 100
            cur_mom = r.get("avg_monthly_6m_pct")
            pri_rank = r.get("priority_rank")
        elif extra:
            px = extra.get("latest_price")
            daily = 0
            conv = 100
            cur_mom = None
            pri_rank = None
        else:
            continue
        if not px:
            continue
        buy = h.get("buy_price") or 0
        shares = h.get("shares") or 0
        if buy <= 0 or shares <= 0:
            continue
        category = h.get("category")
        strategy = h.get("strategy", "anti_martingale")
        ret_pct = (px / buy - 1) * 100
        weeks_held = _hold_weeks(h)
        peak_conv = h.get("peak_conv_while_held") or conv
        entry_mom = h.get("entry_6m_mom")

        def add_alert(typ, metric, action_label, sell_n, snooze_hr=None):
            aid = f"{t}-{today}-{typ}"
            if aid in existing_ids:
                return
            alert = {
                "id": aid,
                "ticker": t,
                "type": typ,
                "triggered_at": _now_iso(),
                "trigger_price": round(px, 2),
                "trigger_metric": metric,
                "action_label": action_label,
                "shares_to_sell": int(sell_n),
                "shares_remaining": int(max(0, shares - sell_n)),
                "buy_price": buy,
                "dismissed": False,
                "dismissed_at": None,
                "snooze_until": (datetime.now(timezone.utc) + timedelta(hours=snooze_hr)).isoformat()
                                if snooze_hr else None,
            }
            new_alerts.append(alert)
            pending.setdefault(t, []).append(alert)
            existing_ids.add(aid)

        if strategy == "anti_martingale" and category not in ("etf", "external"):
            # ===== Hard triggers — always active =====
            if daily <= -8:
                add_alert("flash_8", f"单日 {daily:.2f}%",
                          "减至 30%（卖 70%）", int(shares * 0.7))
            if ret_pct <= -8:
                add_alert("stop_8", f"累计 {ret_pct:.2f}% (买入 ${buy:.2f})",
                          "全清", shares)
            if conv < 45:
                add_alert("conv_break", f"Conv {conv} < 45",
                          "卖一半（基本面恶化）", int(shares / 2))
            # ===== v3.7: would_not_buy (absolute rule, ungated) =====
            # 卖出原则: "如果我是新买家不会买这只 → 卖"
            # action ∈ {WATCH, AVOID} 等价于 Conv < TRY_BUY 阈值 (55)
            if weeks_held >= 2 and r and r.get("action") in ("WATCH", "AVOID"):
                add_alert("would_not_buy",
                          f"Conv {conv:.0f}, action={r.get('action')} (新买家不会买)",
                          "全清（不再值得持有, 该资金转 PRIORITY 头部）", shares)

            # ===== v3.6 Decay trigger: growth_decay (regime-gated) =====
            decay_active = regime_mod >= 0.6   # 浅熊以上都开
            if decay_active:
                if cur_mom is not None and weeks_held >= 4:
                    mom_dead = cur_mom < 1.0
                    mom_halved = (entry_mom is not None and entry_mom >= 3
                                  and cur_mom < entry_mom * 0.5)
                    if mom_dead or mom_halved:
                        reason = f"6M-Mom {cur_mom:.1f}%/月"
                        if mom_halved:
                            reason += f" (入场 {entry_mom:.1f}%, 跌超 50%)"
                        else:
                            reason += " < 1%/月"
                        add_alert("growth_decay", reason,
                                  "全清（动量衰竭, 资金转 PRIORITY 头部）", shares)

        elif strategy == "martingale":
            initial_shares = h.get("peak_shares", shares) or shares
            for dip in (h.get("dip_ladder") or []):
                trigger = dip.get("trigger_pct", 0)
                if ret_pct <= trigger * 100:
                    name = dip.get("name", "dip")
                    buy_n = max(1, int(initial_shares * dip.get("buy_pct_of_initial", 0.3)))
                    add_alert(name, f"累计 {ret_pct:.2f}% (跌至 {trigger*100:.0f}% 加仓档)",
                              f"💧 加仓 {buy_n} 股 (马丁档 {name})", -buy_n)

    PENDING.write_text(json.dumps(pending, ensure_ascii=False, indent=2),
                       encoding="utf-8")

    if new_alerts:
        print(f"alerts: {len(new_alerts)} NEW: " +
              ", ".join(f"{a['ticker']}({a['type']})" for a in new_alerts))
    else:
        print("alerts: no new triggers this run")
    return len(new_alerts)


def dismiss(ticker, alert_id, snooze_hours=None):
    pending = _load_json(PENDING, {})
    arr = pending.get(ticker, [])
    for a in arr:
        if a.get("id") == alert_id:
            if snooze_hours:
                a["snooze_until"] = (datetime.now(timezone.utc)
                                     + timedelta(hours=snooze_hours)).isoformat()
            else:
                a["dismissed"] = True
                a["dismissed_at"] = _now_iso()
            PENDING.write_text(json.dumps(pending, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            return True
    return False


def _current_state():
    """Snapshot of current price + Conv + daily return per ticker, for revalidation."""
    sig = _load_json(SIGNALS, None) or {}
    holdings = _load_json(HOLDINGS, {})
    rows_by_t = {r["ticker"]: r for r in sig.get("rows", [])}
    extras = sig.get("extra_prices", {})
    out = {}
    for t, h in holdings.items():
        r = rows_by_t.get(t)
        extra = extras.get(t)
        # v3.7: 追踪 strategy/category 用于 strategy-aware stale check
        strategy = h.get("strategy", "anti_martingale")
        category = h.get("category")
        if r:
            out[t] = {
                "price": r.get("latest_price"),
                "daily": r.get("daily_return_pct") or 0,
                "conv": r.get("conviction_score") or 100,
                "mom": r.get("avg_monthly_6m_pct"),
                "rank": r.get("priority_rank"),
                "buy_price": h.get("buy_price") or 0,
                "strategy": strategy,
                "category": category,
            }
        elif extra:
            out[t] = {
                "price": extra.get("latest_price"),
                "daily": 0,
                "conv": 100,
                "mom": None,
                "rank": None,
                "buy_price": h.get("buy_price") or 0,
                "strategy": strategy,
                "category": category,
            }
    return out


# v3.7: 这些类型已经退役, active_alerts 时直接撤掉 (不再显示)
RETIRED_TYPES = {"conv_decay", "dead_money", "hwm_trail", "time_stop", "flash_5"}

# v3.7: 这些 alert 类型只对 anti_martingale 持仓有效
# 如果 ticker 当前 strategy != anti_martingale 或 category=etf/external,
# 这些 alert 应当自动撤掉 (避免旧 alert 在 strategy 切换后残留)
ANTI_MART_ONLY_TYPES = {
    "stop_8", "flash_8", "conv_break", "would_not_buy", "growth_decay",
}


def _alert_still_valid(alert, state):
    """Re-validate trigger condition using latest data. Stale alerts auto-hide."""
    typ = alert.get("type")
    # 退役类型直接判失效, 避免老 alert 占着 banner
    if typ in RETIRED_TYPES:
        return False
    t = alert.get("ticker")
    st = state.get(t)
    if not st or not st.get("price") or not st.get("buy_price"):
        return True
    # Strategy-aware: anti-mart-only 类型在非 anti-mart 持仓上失效
    # (例: MSFT 之前是 anti-mart 触发了 growth_decay, 后来改成 martingale, 旧 alert 应自动撤)
    if typ in ANTI_MART_ONLY_TYPES:
        if st.get("strategy") != "anti_martingale":
            return False
        if st.get("category") in ("etf", "external"):
            return False
    px = st["price"]
    buy = st["buy_price"]
    daily = st["daily"]
    conv = st["conv"]
    mom = st.get("mom")
    ret_pct = (px / buy - 1) * 100 if buy else 0

    if typ == "stop_8":
        return ret_pct <= -7
    if typ == "flash_8":
        return daily <= -7
    if typ == "conv_break":
        return conv < 50
    if typ.startswith("dip_"):
        return ret_pct <= -3
    # v3.7
    if typ == "would_not_buy":
        return conv < 58  # 3pt buffer above TRY_BUY (55)
    if typ == "growth_decay":
        return mom is None or mom < 1.5
    return True


def active_alerts():
    """Return alerts that are not dismissed, not snoozed, and whose trigger
    condition STILL holds. De-dupe by (ticker,type).
    """
    pending = _load_json(PENDING, {})
    now = datetime.now(timezone.utc)
    state = _current_state()
    out = []
    auto_expired = 0
    for t, arr in pending.items():
        for a in arr:
            if a.get("dismissed"):
                continue
            snooze = a.get("snooze_until")
            if snooze:
                try:
                    su = datetime.fromisoformat(snooze.replace("Z", "+00:00"))
                    if su.tzinfo is None:
                        su = su.replace(tzinfo=timezone.utc)
                    if su > now:
                        continue
                except ValueError:
                    pass
            if not _alert_still_valid(a, state):
                auto_expired += 1
                continue
            out.append(a)

    # 严重度表 (越小越紧急)
    sev = {"flash_8": 0, "stop_8": 1, "conv_break": 3,
           "would_not_buy": 4, "growth_decay": 5,
           "dip_3": 7, "dip_2": 8, "dip_1": 9}

    # Step 1: (ticker, type) 去重 — 保留最新触发
    dedup = {}
    for a in out:
        k = (a.get("ticker"), a.get("type"))
        prev = dedup.get(k)
        if prev is None or a.get("triggered_at", "") > prev.get("triggered_at", ""):
            dedup[k] = a
    out = list(dedup.values())

    # Step 2: per-ticker 严重度去重 — 同一 ticker 只保留最高严重度的一条
    # (例: GOOGL 同时有 conv_break + 旧 dead_money, 只显示 conv_break)
    by_ticker = {}
    for a in out:
        t = a.get("ticker")
        prev = by_ticker.get(t)
        if prev is None or sev.get(a.get("type"), 99) < sev.get(prev.get("type"), 99):
            by_ticker[t] = a
    out = list(by_ticker.values())

    out.sort(key=lambda a: (sev.get(a.get("type"), 99), a.get("triggered_at", "")))
    if auto_expired:
        print(f"alerts: auto-expired {auto_expired} stale alerts (condition no longer holds)")
    return out


if __name__ == "__main__":
    n = detect_and_persist()
    print(f"\n{n} new alerts; {len(active_alerts())} active total")
