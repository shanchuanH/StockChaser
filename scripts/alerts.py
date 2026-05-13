"""Persistent alert engine — detects trigger events and persists them.

Why this exists:
  `daily_return_pct` is a rolling 1-day delta. If QCOM drops -13% on Monday
  and recovers to flat on Tuesday, the original signal is gone by Tuesday
  morning. This module captures every trigger as an immutable event so the
  user sees "🔔 待操作: QCOM 周一暴跌 -13%" until they explicitly dismiss it.

Triggers detected (per holding, per run):
  flash_5         single-day ≤ -5%   → reduce 50%
  flash_8         single-day ≤ -8%   → reduce to 30%
  stop_8          cumulative ≤ -8%  → close all
  conv_break      Conv < 45         → reduce 50%
  hwm_trail       price ≤ HWM*0.85 (high-water-mark trail) → reduce 50%
  time_stop       held >= 28d AND ret < 12% (no +1R) → reduce 50%

Output: data/pending_alerts.json
  { ticker: [ {id, type, triggered_at, trigger_price, trigger_metric,
              action, shares_to_sell, shares_remaining, dismissed,
              dismissed_at, snooze_until}, ... ] }

De-dup by id = "{ticker}-{YYYY-MM-DD}-{type}". Re-running on the same day
will not duplicate alerts.
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
        # Build {ticker: [(date, close)]}
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
            # Filter to since buy_date
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
    hwm_map = _hwm_per_ticker(holdings)

    for t, h in holdings.items():
        r = rows_by_t.get(t)
        extra = extras.get(t)
        # Resolve current price
        if r:
            px = r.get("latest_price")
            daily = r.get("daily_return_pct") or 0
            conv = r.get("conviction_score") or 100
        elif extra:
            px = extra.get("latest_price")
            daily = 0  # ETF extras don't carry daily
            conv = 100  # not strategy-managed
        else:
            continue
        if not px:
            continue
        buy = h.get("buy_price") or 0
        shares = h.get("shares") or 0
        if buy <= 0 or shares <= 0:
            continue
        category = h.get("category")  # ETF/external — only trail-stop applies
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

        # ----- Triggers (priority order: most severe first) -----
        if category not in ("etf", "external"):
            # 1. Single-day flash crash ≤ -8% → reduce to 30%
            if daily <= -8:
                add_alert("flash_8", f"单日 {daily:.2f}%",
                          f"减至 30%（卖 70%）", int(shares * 0.7))
            # 2. Single-day -5 to -8% → reduce 50%
            elif daily <= -5:
                add_alert("flash_5", f"单日 {daily:.2f}%",
                          f"减半（卖 50%）", int(shares * 0.5))

            # 3. Cumulative -8% hard stop → close all
            if ret_pct <= -8:
                add_alert("stop_8", f"累计 {ret_pct:.2f}% (买入 ${buy:.2f})",
                          "全清", shares)

            # 4. Conviction breakdown
            if conv < 45:
                add_alert("conv_break", f"Conv {conv} < 45",
                          "卖一半（基本面恶化）", int(shares / 2))

            # 5. Time stop — held >= 28 days AND ret < 12% AND Conv < 70.
            # We skip the alert if Conv is still strong: the engine still
            # likes the stock; "timing wrong" ≠ "thesis wrong". Skip ALSO
            # when ret already ≥ 8% — close enough to +1R.
            buy_d = h.get("buy_date")
            if buy_d and conv < 70:
                try:
                    held_days = (datetime.now(timezone.utc).date()
                                 - datetime.strptime(buy_d, "%Y-%m-%d").date()).days
                    if held_days >= 28 and ret_pct < 8:
                        add_alert("time_stop", f"持有 {held_days} 天仅 {ret_pct:+.1f}%, Conv {conv}",
                                  "卖一半（4 周未到 +1R 且引擎不再强烈推荐）",
                                  int(shares / 2))
                except (TypeError, ValueError):
                    pass

        # 6. High-water-mark trailing stop (-15% from peak) — applies to ALL incl ETF
        hwm = hwm_map.get(t)
        if hwm and hwm > buy * 1.10:  # only meaningful if it ever ran > +10%
            trail_line = hwm * 0.85
            if px <= trail_line:
                drop_from_peak = (px / hwm - 1) * 100
                add_alert("hwm_trail", f"距 4w 峰 ${hwm:.2f} {drop_from_peak:.1f}%",
                          "卖一半（回撤 15% 自峰值）", int(shares / 2))

    # Write back
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


def active_alerts():
    """Return list of currently visible alerts (not dismissed, not snoozed)."""
    pending = _load_json(PENDING, {})
    now = datetime.now(timezone.utc)
    out = []
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
            out.append(a)
    # Sort by severity then recency
    sev = {"flash_8": 0, "stop_8": 1, "flash_5": 2, "conv_break": 3,
           "hwm_trail": 4, "time_stop": 5}
    out.sort(key=lambda a: (sev.get(a.get("type"), 99), a.get("triggered_at", "")))
    return out


if __name__ == "__main__":
    n = detect_and_persist()
    print(f"\n{n} new alerts; {len(active_alerts())} active total")
    out.sort(key=lambda a: (sev.get(a.get("type"), 99), a.get("triggered_at", "")))
    return out


if __name__ == "__main__":
    n = detect_and_persist()
    print(f"\n{n} new alerts; {len(active_alerts())} active total")
