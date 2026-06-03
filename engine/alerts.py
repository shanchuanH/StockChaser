"""Persistent alert engine — anti-martingale exit triggers.

Trigger types (v3.7):
  Hard (always active, anti-mart only):
    flash_8        single-day ≤ -8% (PRIORITY top 8 豁免)→ reduce to 30%
    stop_8         cumulative  ≤ -8%                     → close all
    conv_break     Conv  <  45                            → reduce 50%
    would_not_buy  action ∈ {WATCH, AVOID} AND ≥2w held → close all
                   (Conv < 55 = wouldn't buy as new entrant)

  Decay (regime-gated, anti-mart only):
    growth_decay   6M-Mom < 1%/月 or 跌超 50% from 入场 (≥4w)  → close all
                   rm >= 0.6 active; rm < 0.6 disabled

Active-list behavior:
  - Filter dismissed + snoozed
  - Auto-revalidate condition (stale alerts auto-hide)
  - 7-day sticky suppress after dismissal (don't recreate same (ticker,type))
  - Strategy-aware: anti-mart-only types on non-anti-mart holdings → expire
  - Holdings-aware: ticker not in holdings → expire (already sold)
  - PRIORITY-aware: top 8 ticker's flash_8 → expire
  - Per-ticker severity dedup: only show most urgent alert per ticker

Retired types (auto-expired on read, kept for backward compat with old data):
  conv_decay, dead_money, hwm_trail, time_stop, flash_5
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

SUPPRESS_DAYS_AFTER_DISMISS = 7

RETIRED_TYPES = {"conv_decay", "dead_money", "hwm_trail", "time_stop", "flash_5"}
ANTI_MART_ONLY_TYPES = {"stop_8", "flash_8", "conv_break", "would_not_buy", "growth_decay"}

SEVERITY = {
    "flash_8": 0, "stop_8": 1, "conv_break": 3,
    "would_not_buy": 4, "growth_decay": 5,
    "dip_3": 7, "dip_2": 8, "dip_1": 9,
}


# ── IO helpers ───────────────────────────────────────────────────────────

def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_json(p, default):
    p = Path(p)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _hold_weeks(h):
    bd = h.get("buy_date") or ""
    if not bd:
        return 0
    try:
        bd_dt = datetime.strptime(bd[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - bd_dt).days // 7)
    except (ValueError, TypeError):
        return 0


# ── State + suppression maps ────────────────────────────────────────────

def _current_state(signals, holdings):
    """Snapshot of price/Conv/mom + strategy for revalidation."""
    rows_by_t = {r["ticker"]: r for r in signals.get("rows", [])}
    extras = signals.get("extra_prices", {})
    out = {}
    for t, h in holdings.items():
        r = rows_by_t.get(t)
        extra = extras.get(t)
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
                "strategy": strategy, "category": category,
            }
        elif extra:
            out[t] = {
                "price": extra.get("latest_price"),
                "daily": 0, "conv": 100, "mom": None, "rank": None,
                "buy_price": h.get("buy_price") or 0,
                "strategy": strategy, "category": category,
            }
    return out


def _build_suppress_map(pending):
    """(ticker, type) → latest dismissed_at — for 7-day sticky suppress."""
    out = {}
    for ticker, arr in pending.items():
        for a in arr:
            if not a.get("dismissed"):
                continue
            da = a.get("dismissed_at")
            if not da:
                continue
            try:
                dt = datetime.fromisoformat(da.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            key = (ticker, a.get("type"))
            prev = out.get(key)
            if prev is None or dt > prev:
                out[key] = dt
    return out


def _alert_still_valid(alert, state):
    """Stale-check used in active_alerts()."""
    typ = alert.get("type")
    if typ in RETIRED_TYPES:
        return False
    t = alert.get("ticker")
    st = state.get(t)
    if not st:
        return False  # ticker no longer in holdings (already sold)
    if not st.get("price") or not st.get("buy_price"):
        return True
    # Strategy-aware: anti-mart-only on non-anti-mart → expire
    if typ in ANTI_MART_ONLY_TYPES:
        if st.get("strategy") != "anti_martingale":
            return False
        if st.get("category") in ("etf", "external"):
            return False

    px = st["price"]; buy = st["buy_price"]; daily = st["daily"]
    conv = st["conv"]; mom = st.get("mom"); rank = st.get("rank")
    ret_pct = (px / buy - 1) * 100 if buy else 0

    if typ == "stop_8":
        return ret_pct <= -7
    if typ == "flash_8":
        # PRIORITY 头部豁免: top 8 → 历史 flash_8 自动撤
        if rank is not None and rank <= 8:
            return False
        return daily <= -7
    if typ == "conv_break":
        return conv < 50
    if typ.startswith("dip_"):
        return ret_pct <= -3
    if typ == "would_not_buy":
        return conv < 58
    if typ == "growth_decay":
        return mom is None or mom < 1.5
    return True


# ── Detection + persistence ─────────────────────────────────────────────

def detect_and_persist(signals_path, holdings_path, pending_path):
    """Scan holdings vs signals, append new alerts to pending_path.

    Returns count of new alerts created.
    """
    sig = _load_json(signals_path, None)
    if not sig:
        print("alerts: signals.json missing/empty")
        return 0
    holdings = _load_json(holdings_path, {})
    if not holdings:
        print("alerts: no holdings to scan")
        return 0

    rows_by_t = {r["ticker"]: r for r in sig.get("rows", [])}
    extras = sig.get("extra_prices", {})
    today = _today_utc()
    regime_mod = sig.get("regime_modifier") or 1.0
    vix_close = sig.get("vix_close") or 0
    is_black_swan = vix_close >= 30

    pending = _load_json(pending_path, {})
    existing_ids = {a.get("id") for arr in pending.values() for a in arr}
    suppress_map = _build_suppress_map(pending)
    now_utc = datetime.now(timezone.utc)
    suppress_cutoff = now_utc - timedelta(days=SUPPRESS_DAYS_AFTER_DISMISS)

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
            daily = 0; conv = 100; cur_mom = None; pri_rank = None
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
            sup_at = suppress_map.get((t, typ))
            if sup_at and sup_at > suppress_cutoff:
                return  # user dismissed recently
            alert = {
                "id": aid, "ticker": t, "type": typ,
                "triggered_at": _now_iso(),
                "trigger_price": round(px, 2),
                "trigger_metric": metric,
                "action_label": action_label,
                "shares_to_sell": int(sell_n),
                "shares_remaining": int(max(0, shares - sell_n)),
                "buy_price": buy, "dismissed": False, "dismissed_at": None,
                "snooze_until": (now_utc + timedelta(hours=snooze_hr)).isoformat()
                                if snooze_hr else None,
            }
            new_alerts.append(alert)
            pending.setdefault(t, []).append(alert)
            existing_ids.add(aid)

        if strategy == "anti_martingale" and category not in ("etf", "external"):
            # PRIORITY 头部 flash_8 豁免
            in_priority_top = (pri_rank is not None and pri_rank <= 8)
            if daily <= -8 and not in_priority_top:
                add_alert("flash_8", f"单日 {daily:.2f}%",
                          "减至 30%（卖 70%）", int(shares * 0.7))
            if ret_pct <= -8:
                add_alert("stop_8", f"累计 {ret_pct:.2f}% (买入 ${buy:.2f})",
                          "全清", shares)
            if conv < 45:
                add_alert("conv_break", f"Conv {conv} < 45",
                          "卖一半（基本面恶化）", int(shares / 2))
            # would_not_buy
            if weeks_held >= 2 and r and r.get("action") in ("WATCH", "AVOID"):
                add_alert("would_not_buy",
                          f"Conv {conv:.0f}, action={r.get('action')} (新买家不会买)",
                          "全清（不再值得持有, 该资金转 PRIORITY 头部）", shares)
            # growth_decay (regime-gated)
            if regime_mod >= 0.6:
                if cur_mom is not None and weeks_held >= 4:
                    mom_dead = cur_mom < 1.0
                    mom_halved = (entry_mom is not None and entry_mom >= 3
                                  and cur_mom < entry_mom * 0.5)
                    if mom_dead or mom_halved:
                        reason = f"6M-Mom {cur_mom:.1f}%/月"
                        reason += f" (入场 {entry_mom:.1f}%, 跌超 50%)" if mom_halved else " < 1%/月"
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

    Path(pending_path).write_text(
        json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")

    if new_alerts:
        print("alerts: " + str(len(new_alerts)) + " NEW: " +
              ", ".join(a['ticker'] + "(" + a['type'] + ")" for a in new_alerts))
    else:
        print("alerts: no new triggers this run")
    return len(new_alerts)


def active_alerts(signals_path, holdings_path, pending_path):
    """Return un-dismissed, un-snoozed, condition-valid, deduped alerts."""
    pending = _load_json(pending_path, {})
    signals = _load_json(signals_path, {}) or {}
    holdings = _load_json(holdings_path, {})
    state = _current_state(signals, holdings)

    now = datetime.now(timezone.utc)
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

    # (ticker, type) dedup — keep latest
    dedup = {}
    for a in out:
        k = (a.get("ticker"), a.get("type"))
        prev = dedup.get(k)
        if prev is None or a.get("triggered_at", "") > prev.get("triggered_at", ""):
            dedup[k] = a
    out = list(dedup.values())

    # per-ticker severity dedup — only most urgent per ticker
    by_ticker = {}
    for a in out:
        t = a.get("ticker")
        prev = by_ticker.get(t)
        if prev is None or SEVERITY.get(a.get("type"), 99) < SEVERITY.get(prev.get("type"), 99):
            by_ticker[t] = a
    out = list(by_ticker.values())

    out.sort(key=lambda a: (SEVERITY.get(a.get("type"), 99), a.get("triggered_at", "")))
    if auto_expired:
        print("alerts: auto-expired " + str(auto_expired) + " stale alerts")
    return out


def dismiss(ticker, alert_id, snooze_hours, pending_path):
    """Mark alert dismissed (snooze_hours=None) or snooze for N hours."""
    pending = _load_json(pending_path, {})
    arr = pending.get(ticker, [])
    for a in arr:
        if a.get("id") == alert_id:
            if snooze_hours:
                a["snooze_until"] = (datetime.now(timezone.utc)
                                     + timedelta(hours=snooze_hours)).isoformat()
            else:
                a["dismissed"] = True
                a["dismissed_at"] = _now_iso()
            Path(pending_path).write_text(
                json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
    return False
