"""Persistent alert engine — detects trigger events and persists them.

Why this exists:
  `daily_return_pct` is a rolling 1-day delta. If QCOM drops -13% on Monday
  and recovers to flat on Tuesday, the original signal is gone by Tuesday
  morning. This module captures every trigger as an immutable event so the
  user sees "🔔 待操作: QCOM 周一暴跌 -13%" until they explicitly dismiss it.

Triggers detected (per holding, per run):
  flash_8         single-day ≤ -8%   → reduce to 30%
  stop_8          cumulative ≤ -8%   → close all
  conv_break      Conv < 45          → reduce 50%

Active-list behavior (active_alerts):
  - Filters dismissed + snoozed (original)
  - NEW: re-validates trigger condition against latest price/Conv. If the
    condition no longer holds (e.g. price recovered above stop_8 threshold),
    the alert is auto-hidden — history is preserved in pending_alerts.json.
  - NEW: de-dupes by (ticker, type) — only the most recent trigger shows.

Output: data/pending_alerts.json
  { ticker: [ {id, type, triggered_at, trigger_price, trigger_metric,
              action, shares_to_sell, shares_remaining, dismissed,
              dismissed_at, snooze_until}, ... ] }
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
        elif extra:
            px = extra.get("latest_price")
            daily = 0
            conv = 100
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
            # 反马丁纪律: 硬止损 + 单日暴跌 + Conviction 跌破。
            # 不设固定止盈 — 让赢家奔跑。
            if daily <= -8:
                add_alert("flash_8", f"单日 {daily:.2f}%",
                          "减至 30%（卖 70%）", int(shares * 0.7))
            if ret_pct <= -8:
                add_alert("stop_8", f"累计 {ret_pct:.2f}% (买入 ${buy:.2f})",
                          "全清", shares)
            if conv < 45:
                add_alert("conv_break", f"Conv {conv} < 45",
                          "卖一半（基本面恶化）", int(shares / 2))
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
        if r:
            out[t] = {
                "price": r.get("latest_price"),
                "daily": r.get("daily_return_pct") or 0,
                "conv": r.get("conviction_score") or 100,
                "buy_price": h.get("buy_price") or 0,
            }
        elif extra:
            out[t] = {
                "price": extra.get("latest_price"),
                "daily": 0,
                "conv": 100,
                "buy_price": h.get("buy_price") or 0,
            }
    return out


def _alert_still_valid(alert, state):
    """触发条件是否仍成立 — 不成立就视为该操作已不需要, 自动撤掉。

    例:
      stop_8 触发时累计 -10%, 现在反弹到 -3% → invalid (不需要再全清)
      flash_8 周一暴跌 -9%, 周二 daily +2% → invalid (今日不是黑天鹅)
      conv_break Conv=42 触发, 现在 Conv=55 → invalid (基本面回升)
    """
    t = alert.get("ticker")
    typ = alert.get("type")
    st = state.get(t)
    if not st or not st.get("price") or not st.get("buy_price"):
        return True
    px = st["price"]
    buy = st["buy_price"]
    daily = st["daily"]
    conv = st["conv"]
    ret_pct = (px / buy - 1) * 100 if buy else 0

    if typ == "stop_8":
        return ret_pct <= -7  # 留 1% 缓冲避免抖动
    if typ == "flash_8":
        return daily <= -7
    if typ == "flash_5":
        return daily <= -4
    if typ == "conv_break":
        return conv < 50  # 留 5 点缓冲
    if typ == "hwm_trail":
        return ret_pct <= -10
    if typ.startswith("dip_"):
        return ret_pct <= -3
    return True


def active_alerts():
    """Return alerts that are not dismissed, not snoozed, and whose
    trigger condition STILL holds (auto-expire stale ones)."""
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

    # 同一 ticker+type 只保留最新触发的一条
    dedup = {}
    for a in out:
        k = (a.get("ticker"), a.get("type"))
        prev = dedup.get(k)
        if prev is None or a.get("triggered_at", "") > prev.get("triggered_at", ""):
            dedup[k] = a
    out = list(dedup.values())

    sev = {"flash_8": 0, "stop_8": 1, "flash_5": 2, "conv_break": 3,
           "dip_3": 4, "dip_2": 5, "dip_1": 6,
           "hwm_trail": 7, "time_stop": 8}
    out.sort(key=lambda a: (sev.get(a.get("type"), 99), a.get("triggered_at", "")))
    if auto_expired:
        print(f"alerts: auto-expired {auto_expired} stale alerts (condition no longer holds)")
    return out


if __name__ == "__main__":
    n = detect_and_persist()
    print(f"\n{n} new alerts; {len(active_alerts())} active total")
