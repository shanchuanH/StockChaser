"""Compare 4 exit-rule variants on top of identical S6 entry picks.

Why this exists:
  bt_v36.py only tests entry+stop, not the new alerts (flash_5/hwm_trail/time_stop).
  User wants to know if the new aggressive alerts help or hurt vs the
  original conservative rules — without speculation, just measured P&L.

Variants (all use same S6 weekly entry selection):
  V0  No exits at all          baseline showing "what AI chain just did"
  V1  Original conservative    8% stop + Conv<45 exit (what's in portfolio.json)
  V2  Aggressive (current code) V1 + flash5/flash8 + hwm_trail -15% + time_stop @28d
  V3  Middle ground            V1 + hwm_trail only (drop flash5, keep min hold)

Output: scripts run, prints comparison table; writes JSON.
"""
from __future__ import annotations
import json, math
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "data" / "universe.json"
HIST_PARQ = ROOT / "data" / "history.parquet"
HIST_CSV = ROOT / "data" / "history.csv"
OUT = ROOT / "data" / "bt_exit_variants.json"

TXN_COST = 0.0005
TOP_N = 8
MIN_BARS = 252
MIN_HOLD_WEEKS = 4
STOP_PCT = 0.08         # cumulative -8% stop (V1+)
DAILY_FLASH_5 = -5.0    # V2 only — single-day ≤ -5%
DAILY_FLASH_8 = -8.0    # V2 only — single-day ≤ -8%
HWM_TRAIL_PCT = 0.15    # V2 + V3 — drawdown from peak
TIME_STOP_WEEKS = 4     # V2 only
TIME_STOP_RET = 0.08    # V2 only — ret < +8% considered "no progress"


def load_history():
    import pandas as pd
    if HIST_PARQ.exists():
        try: return pd.read_parquet(HIST_PARQ)
        except Exception: pass
    return pd.read_csv(HIST_CSV)


def equity_metrics(equity):
    if len(equity) < 2:
        return {"total_return": 0, "cagr": 0, "sharpe": 0, "max_dd": 0, "win_rate": 0}
    total = equity[-1] / equity[0] - 1
    n_years = (len(equity) - 1) / 52
    cagr = (equity[-1] / equity[0]) ** (1 / max(n_years, 0.01)) - 1
    rets = [equity[i] / equity[i-1] - 1 for i in range(1, len(equity))]
    avg = sum(rets) / len(rets)
    var = sum((r - avg) ** 2 for r in rets) / max(len(rets) - 1, 1)
    std = math.sqrt(var)
    sharpe = (avg / std) * math.sqrt(52) if std > 0 else 0
    win = sum(1 for r in rets if r > 0) / len(rets) if rets else 0
    peak = equity[0]; max_dd = 0
    for e in equity:
        if e > peak: peak = e
        dd = e / peak - 1
        if dd < max_dd: max_dd = dd
    return {"total_return": total, "cagr": cagr, "sharpe": sharpe,
            "max_dd": max_dd, "win_rate": win}


def avg_monthly_6m(close, di, step=21, n=6):
    pts = []
    for k in range(n + 1):
        idx = di - k * step
        if idx < 0: return None
        pts.append(close[idx])
    pts = list(reversed(pts))
    rets = [(pts[i]/pts[i-1] - 1) for i in range(1, len(pts))]
    return sum(rets) / len(rets)


def regime_scale(spy_close, di):
    """v3.5 SMA200 kill switch (matches engine_v35)."""
    if di < 200: return 1.0
    sma200 = sum(spy_close[di-199:di+1]) / 200
    sma50 = sum(spy_close[di-49:di+1]) / 50 if di >= 50 else sma200
    spy_4w = (spy_close[di] / spy_close[di-21] - 1) * 100 if di >= 21 else 0
    if spy_close[di] < sma200:
        dist = (spy_close[di] / sma200 - 1) * 100
        if dist < -10: return 0.3
        if dist < -5: return 0.4
        return 0.5
    if spy_close[di] < sma50 and spy_4w < -3:
        return 0.7
    return 1.0


def conviction(f, u, rm):
    ts = 50
    if f["close"] > f["sma20"]: ts += 8
    if f["close"] > f["sma50"]: ts += 8
    d4 = f["dist_4w"]
    if d4 >= -1: ts += 15
    elif d4 >= -5: ts += 8
    elif d4 < -15: ts -= 12
    rs = f["rs"]
    if rs >= 8: ts += 12
    elif rs >= 3: ts += 6
    elif rs <= -5: ts -= 10
    aligned = sum(1 for x in (f["wk"], f["mo"], f["m3"]) if (x or 0) > 0)
    ts += [0,2,5,10][min(aligned, 3)]
    avg6m = f.get("avg6m")
    if avg6m is not None:
        if avg6m >= 0.03: ts += 10
        elif avg6m >= 0.015: ts += 6
        elif avg6m >= 0: ts += 2
        elif avg6m <= -0.02: ts -= 10
        elif avg6m < 0: ts -= 4
    ts = max(0, min(100, ts))
    eq = 50
    wk = f["wk"] or 0
    if 3 <= wk <= 8: eq += 18
    elif 8 < wk <= 12: eq += 6
    elif wk > 12: eq -= 8
    elif 0 <= wk < 3: eq += 4
    elif wk < -3: eq -= 12
    if d4 > 3: eq -= 8
    if f["sma20"] and f["close"]/f["sma20"] > 1.12: eq -= 15
    elif f["sma20"] and f["close"]/f["sma20"] > 1.08: eq -= 7
    eq = max(0, min(100, eq))
    fc = (u.get("potential", 0) or 0) * 12 + {"龙头":12,"二线":6}.get(u.get("role"), 0)
    layer_key = u.get("layer","").split("·")[0]
    fc += {"L10":5,"L11":5,"L12":4,"L9":3,"L6":3,"L13":3}.get(layer_key, 0)
    if u["ticker"] in {"SNDK","ALAB","AAOI","CRDO","CRWV","IREN","MTSI","GLW","CAMT","TTMI","AXTI","POET","BE","OKLO","NBIS","APLD"}:
        fc += 5
    fc = max(0, min(100, fc))
    return (ts*0.4 + eq*0.3 + fc*0.3) * rm, layer_key


def run():
    import pandas as pd
    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    by_t = {u["ticker"]: u for u in universe}
    df = load_history()
    df["date"] = pd.to_datetime(df["date"])
    pv = df.pivot(index="date", columns="ticker", values="close").sort_index().ffill()
    dates = list(pv.index)
    didx = {d: i for i, d in enumerate(dates)}

    # Weekly review dates (Mondays-ish)
    weeks = {}
    for d in dates:
        k = (d.isocalendar().year, d.isocalendar().week)
        if k not in weeks: weeks[k] = d
    weeks = [weeks[k] for k in sorted(weeks)]
    start = next(i for i, d in enumerate(weeks) if didx.get(d, -1) >= MIN_BARS)
    bt_weeks = weeks[start:]
    print(f"Backtest: {bt_weeks[0].date()} → {bt_weeks[-1].date()}  ({len(bt_weeks)} weeks ≈ {len(bt_weeks)/52:.1f} yr)")

    tickers = [t for t in pv.columns if t != "QQQ"]
    closes = {t: pv[t].ffill().tolist() for t in tickers}
    spy = closes["SPY"]
    universe_t = [t for t in tickers if t in by_t]

    # Four parallel portfolios — same picks, different exit rules
    variants = ["V0", "V1", "V2", "V3"]
    eq = {v: [1.0] for v in variants}
    holdings = {v: [] for v in variants}
    exit_log = {v: Counter() for v in variants}
    txn_log = {v: 0 for v in variants}  # total turnover count

    for w_idx, wd in enumerate(bt_weeks):
        di = didx[wd]
        if di < MIN_BARS: continue

        # Compute features
        feats = {}
        for t in universe_t:
            cl = closes[t]
            if di >= len(cl) or cl[di] is None or math.isnan(cl[di]): continue
            last = cl[di]
            if last <= 0: continue
            def ret(n):
                if di-n < 0: return None
                base = cl[di-n]
                return (last/base - 1)*100 if base and not math.isnan(base) else None
            wk, mo, m3 = ret(5), ret(21), ret(63)
            spy_4w = (spy[di]/spy[di-21] - 1)*100 if di >= 21 else 0
            rs = (mo - spy_4w) if (mo is not None) else 0
            avg6m = avg_monthly_6m(cl, di)
            high4 = max(cl[max(0,di-19):di+1])
            sma20 = sum(cl[max(0,di-19):di+1]) / min(20, di+1)
            sma50 = sum(cl[max(0,di-49):di+1]) / min(50, di+1)
            mom_s = 50 + (wk or 0)*1.5 + (mo or 0)*0.8 + (m3 or 0)*0.4 + rs*0.6
            if last > sma20: mom_s += 5
            if last > sma50: mom_s += 5
            d4 = (last/high4 - 1)*100
            mom_s = max(0, min(100, mom_s))
            comp = mom_s*0.6 + min(100, (by_t[t].get("potential",0) or 0)*18 + {"龙头":8,"二线":4}.get(by_t[t].get("role"),0))*0.4
            feats[t] = {"close": last, "wk": wk, "mo": mo, "m3": m3, "rs": rs,
                       "avg6m": avg6m, "high_4w": high4, "sma20": sma20, "sma50": sma50,
                       "comp": comp, "dist_4w": d4}

        # ---- Realize prior week PnL with variant-specific exits ----
        if w_idx > 0:
            prev_di = didx[bt_weeks[w_idx-1]]
            for v in variants:
                basket = holdings[v]
                if not basket:
                    eq[v].append(eq[v][-1]); continue
                weighted = 0
                txn_w = 0
                new_basket = []
                for h in basket:
                    cl = closes[h["ticker"]]
                    if di >= len(cl) or math.isnan(cl[di]):
                        new_basket.append(h); continue
                    last_px = cl[di]
                    entry = h["entry_price"]
                    weight = h["weight"]
                    held_weeks = w_idx - h.get("entry_week_idx", w_idx)
                    cum_ret = last_px / entry - 1

                    # Compute intraweek high for HWM trail
                    week_start_di = max(0, prev_di + 1)
                    week_slice = cl[week_start_di:di+1]
                    week_high = max([x for x in week_slice if x and not math.isnan(x)]
                                    + [h.get("peak_price", entry)])
                    h["peak_price"] = max(h.get("peak_price", entry), week_high)

                    # Compute daily worst (intraweek) from close-to-close
                    worst_day = 0
                    for j in range(max(week_start_di, 1), di+1):
                        if cl[j-1] and not math.isnan(cl[j-1]) and cl[j] and not math.isnan(cl[j]):
                            d_ret = (cl[j]/cl[j-1] - 1) * 100
                            if d_ret < worst_day: worst_day = d_ret

                    conv_now = feats.get(h["ticker"], {}).get("comp", 100)

                    # ---- Variant-specific exit logic ----
                    exit_type = None
                    fraction_sold = 0  # how much of position closed this week

                    if v == "V0":
                        # No exits except Conv < 45 thesis-break
                        if conv_now < 45:
                            exit_type = "conv_break"
                            fraction_sold = 1.0
                    elif v == "V1":
                        # Original conservative: 8% stop + Conv < 45
                        if cum_ret <= -STOP_PCT:
                            exit_type = "stop_8"
                            fraction_sold = 1.0
                        elif conv_now < 45:
                            exit_type = "conv_break"
                            fraction_sold = 1.0
                    elif v == "V2":
                        # Aggressive: all triggers
                        # 1. Single-day flash crash (highest priority)
                        if worst_day <= DAILY_FLASH_8:
                            exit_type = "flash_8"
                            fraction_sold = 0.70
                        elif worst_day <= DAILY_FLASH_5:
                            exit_type = "flash_5"
                            fraction_sold = 0.50
                        # 2. Cumulative stop
                        elif cum_ret <= -STOP_PCT:
                            exit_type = "stop_8"
                            fraction_sold = 1.0
                        # 3. Conv break
                        elif conv_now < 45:
                            exit_type = "conv_break"
                            fraction_sold = 1.0
                        # 4. HWM trail (only if peak > +10%)
                        elif h["peak_price"] > entry * 1.10 and last_px <= h["peak_price"] * (1 - HWM_TRAIL_PCT):
                            exit_type = "hwm_trail"
                            fraction_sold = 0.50
                        # 5. Time stop (only after min_hold satisfied)
                        elif held_weeks >= TIME_STOP_WEEKS and cum_ret < TIME_STOP_RET and conv_now < 70:
                            exit_type = "time_stop"
                            fraction_sold = 0.50
                    elif v == "V3":
                        # Middle ground: V1 + HWM trail only
                        if cum_ret <= -STOP_PCT:
                            exit_type = "stop_8"
                            fraction_sold = 1.0
                        elif conv_now < 45:
                            exit_type = "conv_break"
                            fraction_sold = 1.0
                        elif h["peak_price"] > entry * 1.10 and last_px <= h["peak_price"] * (1 - HWM_TRAIL_PCT):
                            exit_type = "hwm_trail"
                            fraction_sold = 0.50

                    if exit_type:
                        exit_log[v][exit_type] += 1
                        txn_log[v] += 1
                        if fraction_sold >= 1.0:
                            # Sell entire position — realize from last_price to current
                            base_px = h.get("last_price", entry)
                            weighted += (last_px/base_px - 1) * weight
                            txn_w += weight
                            # Don't carry forward
                            continue
                        else:
                            # Partial sale — record gain on sold portion, keep remainder
                            base_px = h.get("last_price", entry)
                            weighted += (last_px/base_px - 1) * weight  # account for the price move on entire position first
                            txn_w += weight * fraction_sold              # txn cost on the sold portion
                            # Reduce weight for next iteration
                            h["weight"] = weight * (1 - fraction_sold)
                            h["last_price"] = last_px
                            new_basket.append(h)
                    else:
                        base_px = h.get("last_price", entry)
                        weighted += (last_px/base_px - 1) * weight
                        h["last_price"] = last_px
                        new_basket.append(h)

                eq[v].append(eq[v][-1] * (1 + weighted - 2 * TXN_COST * txn_w))
                holdings[v] = new_basket

        # ---- Pick new entries (same logic for all variants) ----
        rm = regime_scale(spy, di)
        cands = []
        for t, f in feats.items():
            cv, lk = conviction(f, by_t[t], rm)
            cands.append((t, cv, lk, f.get("avg6m") or 0))
        cands.sort(key=lambda x: x[1], reverse=True)

        for v in variants:
            kept = holdings[v]
            kept_ticks = {h["ticker"] for h in kept}
            layer_count = Counter(h.get("lk") for h in kept)
            # Min hold protects each holding
            for h in kept:
                h["lk"] = by_t[h["ticker"]]["layer"].split("·")[0]

            new_basket = list(kept)
            # Top up to TOP_N slots with new picks (after min-hold filter on existing)
            for t, cv, lk, mom in cands:
                if len(new_basket) >= TOP_N: break
                if t in kept_ticks: continue
                if cv < 50: continue
                if mom < 0.01: continue
                if layer_count.get(lk, 0) >= 2: continue
                # Tier weight
                if cv >= 85: base_w = 0.12
                elif cv >= 72: base_w = 0.08
                else: base_w = 0.04
                w = base_w * rm
                new_basket.append({
                    "ticker": t, "entry_price": feats[t]["close"],
                    "last_price": feats[t]["close"], "peak_price": feats[t]["close"],
                    "weight": w, "entry_week_idx": w_idx, "lk": lk,
                })
                kept_ticks.add(t)
                layer_count[lk] = layer_count.get(lk, 0) + 1
                txn_log[v] += 1  # entry txn
            holdings[v] = new_basket

    # ---- Metrics ----
    metrics = {v: equity_metrics(eq[v]) for v in variants}
    spy_eq = [1.0]
    for i in range(1, len(bt_weeks)):
        prev = didx[bt_weeks[i-1]]; cur = didx[bt_weeks[i]]
        spy_eq.append(spy_eq[-1] * spy[cur] / spy[prev])
    metrics["SPY"] = equity_metrics(spy_eq)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "weeks": len(bt_weeks),
        "years": len(bt_weeks) / 52,
        "start": str(bt_weeks[0].date()),
        "end": str(bt_weeks[-1].date()),
        "variants": {
            "V0_no_exits":       {"desc": "Buy & Hold S6 picks (only Conv<45)",       **metrics["V0"], "exits": dict(exit_log["V0"]), "txns": txn_log["V0"]},
            "V1_conservative":   {"desc": "Original: 8% stop + Conv<45",                **metrics["V1"], "exits": dict(exit_log["V1"]), "txns": txn_log["V1"]},
            "V2_aggressive":     {"desc": "V1 + flash5/8 + HWM trail + time stop",      **metrics["V2"], "exits": dict(exit_log["V2"]), "txns": txn_log["V2"]},
            "V3_middle":         {"desc": "V1 + HWM trail only",                        **metrics["V3"], "exits": dict(exit_log["V3"]), "txns": txn_log["V3"]},
            "SPY_baseline":      {"desc": "Buy & Hold SPY",                              **metrics["SPY"]},
        },
        "equity_curves": {v: eq[v] for v in variants},
        "spy_equity": spy_eq,
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT}\n")

    # Print table
    header = ["Variant", "Total %", "CAGR", "MaxDD", "Sharpe", "Win%", "Txns", "Exits breakdown"]
    print("{:<20} {:>9} {:>8} {:>8} {:>7} {:>6} {:>5} {}".format(*header))
    print("-" * 110)
    for v in ["V0", "V1", "V2", "V3"]:
        m = metrics[v]
        e = exit_log[v]
        ex_str = ", ".join(f"{k}:{c}" for k, c in e.most_common())
        print("{:<20} {:>+8.1%} {:>+7.1%} {:>+7.1%} {:>7.2f} {:>5.0%} {:>5d} {}".format(
            v, m["total_return"], m["cagr"], m["max_dd"], m["sharpe"], m["win_rate"], txn_log[v], ex_str or "—"))
    m = metrics["SPY"]
    print("{:<20} {:>+8.1%} {:>+7.1%} {:>+7.1%} {:>7.2f} {:>5.0%}  —    (baseline)".format(
        "SPY (B&H)", m["total_return"], m["cagr"], m["max_dd"], m["sharpe"], m["win_rate"]))


if __name__ == "__main__":
    run()
