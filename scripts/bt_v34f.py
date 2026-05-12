"""Backtest 5 strategies on the AI-chain universe.

Strategies:
  S1  Buy & Hold SPY                       (baseline)
  S2  6-month avg monthly momentum, Top 3  (the article)
  S3  Composite score Top 10 (no gates)
  S4  Three-Gate + ATR sizing  (v2 — too binary)
  S5  Conviction Engine ★ NEW v3
        - Graded position: STRONG_BUY 12% / BUY 8% / TRY_BUY 4%
        - Regime modifier scales positions instead of binary cutoff
        - Per-layer cap: max 1 strong-buy per layer
        - Up to 8 concurrent positions, 15% cash buffer
"""
from __future__ import annotations
import json
import math
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "data" / "universe.json"
HIST_PARQ = ROOT / "data" / "history.parquet"
HIST_CSV = ROOT / "data" / "history.csv"
OUT = ROOT / "data" / "backtest_results.json"

TXN_COST = 0.0005
TOP_N_S2 = 3
TOP_N_S3 = 10
TOP_N_S4 = 5
TOP_N_S5 = 8
LOOKBACK_DAYS = 126
MIN_BARS_BEFORE_START = 252


def load_history():
    import pandas as pd
    df = None
    if HIST_PARQ.exists():
        try:
            df = pd.read_parquet(HIST_PARQ)
        except Exception:
            df = None
    if df is None:
        if HIST_CSV.exists():
            df = pd.read_csv(HIST_CSV)
        else:
            raise FileNotFoundError(f"No history file found at {HIST_PARQ} or {HIST_CSV}")
    df["date"] = pd.to_datetime(df["date"])
    return df


def weekly_dates(pv):
    dates = list(pv.index)
    weeks = {}
    for d in dates:
        key = (d.isocalendar().year, d.isocalendar().week)
        if key not in weeks:
            weeks[key] = d
    return [weeks[k] for k in sorted(weeks)]


def avg_monthly_6m(close_series, end_idx, monthly_step=21, n_months=6):
    pts = []
    for k in range(n_months + 1):
        idx = end_idx - k * monthly_step
        if idx < 0:
            return None
        pts.append(close_series[idx])
    pts = list(reversed(pts))
    rets = [(pts[i] / pts[i - 1] - 1) for i in range(1, len(pts))]
    return sum(rets) / len(rets)


def equity_curve_metrics(equity, dates):
    if not equity or len(equity) < 2:
        return {"total_return": 0, "cagr": 0, "sharpe": 0, "win_rate": 0, "max_drawdown": 0, "n_weeks": 0}
    total_return = equity[-1] / equity[0] - 1
    n_weeks = len(equity)
    n_years = n_weeks / 52
    cagr = (equity[-1] / equity[0]) ** (1 / max(n_years, 0.01)) - 1
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    if rets:
        avg = sum(rets) / len(rets)
        var = sum((r - avg) ** 2 for r in rets) / max(len(rets) - 1, 1)
        std = math.sqrt(var)
        sharpe = (avg / std) * math.sqrt(52) if std > 0 else 0.0
        win_rate = sum(1 for r in rets if r > 0) / len(rets)
    else:
        sharpe = 0.0
        win_rate = 0.0
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = e / peak - 1
        if dd < max_dd:
            max_dd = dd
    return {"total_return": total_return, "cagr": cagr, "sharpe": sharpe,
            "win_rate": win_rate, "max_drawdown": max_dd, "n_weeks": n_weeks}


def compute_conviction(f, u, rm):
    """Replicate engine.py logic at backtest time."""
    # Trend Strength
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
    ts += [0, 2, 5, 10][min(aligned, 3)]
    # v3.2 — 6-month average monthly momentum integration
    avg6m = f.get("avg6m")
    if avg6m is not None:
        if avg6m >= 0.03: ts += 10        # ≥3% / month
        elif avg6m >= 0.015: ts += 6
        elif avg6m >= 0: ts += 2
        elif avg6m <= -0.02: ts -= 10
        elif avg6m < 0: ts -= 4
    ts = max(0, min(100, ts))

    # Entry Quality
    eq = 50
    wk = f["wk"] or 0
    if 3 <= wk <= 8: eq += 18
    elif 8 < wk <= 12: eq += 6
    elif wk > 12: eq -= 8
    elif 0 <= wk < 3: eq += 4
    elif wk < -3: eq -= 12
    if d4 > 3: eq -= 8
    if f["sma20"] and f["close"] / f["sma20"] > 1.12: eq -= 15
    elif f["sma20"] and f["close"] / f["sma20"] > 1.08: eq -= 7
    eq = max(0, min(100, eq))

    # Fundamental (v3.2 — Framework 1 downstream lag + Framework 3 new node bonus)
    fc = (u.get("potential", 0) or 0) * 12 + {"龙头": 12, "二线": 6, "概念": 0}.get(u.get("role"), 0)
    layer_key = u.get("layer", "").split("·")[0]
    ds_bonus = {"L10": 5, "L11": 5, "L12": 4, "L9": 3, "L6": 3, "L13": 3}.get(layer_key, 0)
    fc += ds_bonus
    new_node = {"SNDK","ALAB","AAOI","CRDO","CRWV","IREN","MTSI","GLW","CAMT","TTMI","AXTI","POET","BE","OKLO","NBIS","APLD"}
    if u.get("ticker") in new_node: fc += 5
    fc += max(0, f["comp"] - 70) * 0.3
    fc = max(0, min(100, fc))

    return (ts * 0.4 + eq * 0.3 + fc * 0.3) * rm, layer_key


def regime_modifier_for(spy_close, di):
    if di < 50:
        return 0.7
    spy_above_50 = spy_close[di] > sum(spy_close[di-49:di+1]) / 50
    spy_4w = (spy_close[di] / spy_close[di-21] - 1) * 100 if di >= 21 else 0
    if spy_above_50 and spy_4w >= 4: return 1.10
    if spy_above_50 and spy_4w >= 1: return 1.00
    if spy_above_50: return 0.90
    if -3 <= spy_4w < 0: return 0.70
    if spy_4w < -5: return 0.30
    return 0.55


def run():
    import pandas as pd
    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    by_ticker = {u["ticker"]: u for u in universe}

    df = load_history()
    pv_close = df.pivot(index="date", columns="ticker", values="close").sort_index().ffill()
    dates = list(pv_close.index)
    date_to_idx = {d: i for i, d in enumerate(dates)}
    weeks = weekly_dates(pv_close)
    start_idx = next(i for i, d in enumerate(weeks) if date_to_idx.get(d, -1) >= MIN_BARS_BEFORE_START)
    bt_weeks = weeks[start_idx:]
    print(f"Backtest from {bt_weeks[0].date()} to {bt_weeks[-1].date()}  ({len(bt_weeks)} weeks)")

    tickers = [t for t in pv_close.columns if t != "QQQ"]
    close_arr = {t: pv_close[t].ffill().tolist() for t in tickers}
    spy_close = close_arr["SPY"]

    fund_score = {}
    for u in universe:
        s = (u.get("potential", 0) or 0) * 18
        s += {"龙头": 8, "二线": 4, "概念": 0}.get(u.get("role"), 0)
        fund_score[u["ticker"]] = min(100, s)

    eq = {"S1": [1.0], "S2": [1.0], "S3": [1.0], "S4": [1.0], "S5": [1.0], "S6": [1.0]}
    holdings = {"S2": [], "S3": [], "S4": [], "S5": [], "S6": []}
    last_positions = {"S2": [], "S3": [], "S4": [], "S5": [], "S6": []}

    universe_tickers = [t for t in tickers if t in by_ticker]

    for w_idx, week_date in enumerate(bt_weeks):
        di = date_to_idx.get(week_date)
        if di is None:
            continue
        if di < MIN_BARS_BEFORE_START:
            continue

        # Compute features at di
        feats = {}
        for t in universe_tickers:
            cl = close_arr[t]
            if di >= len(cl) or cl[di] is None or math.isnan(cl[di]):
                continue
            last = cl[di]
            if last <= 0:
                continue

            def ret(n):
                if di - n < 0: return None
                base = cl[di - n]
                return (last / base - 1) * 100 if base and not math.isnan(base) else None

            wk = ret(5); mo = ret(21); m3 = ret(63)
            spy_4w = (spy_close[di] / spy_close[di - 21] - 1) * 100 if di >= 21 else 0
            rs = (mo - spy_4w) if (mo is not None and spy_4w is not None) else 0
            avg6m = avg_monthly_6m(cl, di)

            high_4w = max(cl[max(0, di - 19):di + 1])
            high_52w = max(cl[max(0, di - 252):di + 1])
            sma20 = sum(cl[max(0, di - 19):di + 1]) / min(20, di + 1)
            sma50 = sum(cl[max(0, di - 49):di + 1]) / min(50, di + 1)

            mom_s = 50 + (wk or 0) * 1.5 + (mo or 0) * 0.8 + (m3 or 0) * 0.4 + rs * 0.6
            if last > sma20: mom_s += 5
            if last > sma50: mom_s += 5
            d4 = (last / high_4w - 1) * 100
            if d4 < -10: mom_s += d4 * 0.5
            mom_s = max(0, min(100, mom_s))
            comp = mom_s * 0.6 + fund_score.get(t, 0) * 0.4

            feats[t] = {
                "close": last, "wk": wk, "mo": mo, "m3": m3, "rs": rs,
                "avg6m": avg6m, "high_4w": high_4w, "high_52w": high_52w,
                "sma20": sma20, "sma50": sma50, "comp": comp,
                "dist_4w": d4, "dist_52w": (last / high_52w - 1) * 100,
            }

        # Realize prior week PnL
        if w_idx > 0:
            prev_week = bt_weeks[w_idx - 1]
            prev_di = date_to_idx.get(prev_week)
            if prev_di is None:
                prev_di = dates.index(prev_week)
            ret_spy = spy_close[di] / spy_close[prev_di] - 1
            eq["S1"].append(eq["S1"][-1] * (1 + ret_spy))

            for s in ["S2", "S3", "S4"]:
                basket = holdings[s]
                if not basket:
                    eq[s].append(eq[s][-1])
                    continue
                rets = []
                for h in basket:
                    cl = close_arr[h["ticker"]]
                    if di < len(cl) and not math.isnan(cl[di]):
                        rets.append(cl[di] / h["entry_price"] - 1)
                if not rets:
                    eq[s].append(eq[s][-1])
                else:
                    avg_ret = sum(rets) / len(rets)
                    eq[s].append(eq[s][-1] * (1 + avg_ret - 2 * TXN_COST))

            # S5 weighted
            basket5 = holdings["S5"]
            if not basket5:
                eq["S5"].append(eq["S5"][-1])
            else:
                weighted_ret = 0.0
                total_weight = sum(h.get("weight", 0) for h in basket5)
                for h in basket5:
                    cl = close_arr[h["ticker"]]
                    if di < len(cl) and not math.isnan(cl[di]):
                        weighted_ret += (cl[di] / h["entry_price"] - 1) * h["weight"]
                eq["S5"].append(eq["S5"][-1] * (1 + weighted_ret - 2 * TXN_COST * total_weight))

            # S6 weighted (v3.4f hybrid + stability)
            # Realize 1-WEEK return using last_price (not original entry_price)
            basket6 = holdings["S6"]
            if not basket6:
                eq["S6"].append(eq["S6"][-1])
            else:
                weighted_ret = 0.0
                txn_weight = 0.0  # only NEW or refreshed positions pay txn cost
                for h in basket6:
                    cl = close_arr[h["ticker"]]
                    if di < len(cl) and not math.isnan(cl[di]):
                        base_price = h.get("last_price", h["entry_price"])
                        weighted_ret += (cl[di] / base_price - 1) * h["weight"]
                        # update last_price after realizing
                        h["last_price"] = cl[di]
                        if h.get("entry_week_idx", -1) == w_idx - 1:
                            # this was a new entry last week -> pays txn cost on rebuild
                            txn_weight += h["weight"]
                eq["S6"].append(eq["S6"][-1] * (1 + weighted_ret - 2 * TXN_COST * txn_weight))
        # Align lengths
        target_len = w_idx + 1
        for k in eq:
            while len(eq[k]) < target_len:
                eq[k].append(eq[k][-1])

        # Pick baskets
        # S2
        s2_pool = [(t, f["avg6m"]) for t, f in feats.items() if f["avg6m"] is not None]
        s2_pool.sort(key=lambda x: x[1], reverse=True)
        holdings["S2"] = [{"ticker": t, "entry_price": feats[t]["close"]} for t, _ in s2_pool[:TOP_N_S2]]

        # S3
        s3_pool = [(t, f["comp"]) for t, f in feats.items()]
        s3_pool.sort(key=lambda x: x[1], reverse=True)
        holdings["S3"] = [{"ticker": t, "entry_price": feats[t]["close"]} for t, _ in s3_pool[:TOP_N_S3]]

        # S4: 3-gate
        spy_above_50 = spy_close[di] > sum(spy_close[max(0, di-49):di+1]) / min(50, di+1)
        eligible4 = []
        for t, f in feats.items():
            s_a = f["close"] >= f["high_4w"] * 0.99
            s_b = (f["wk"] or 0) >= 3
            g2 = sum([s_a, s_b]) >= 1
            g3 = f["comp"] >= 80 and f["rs"] >= 0 and f["dist_52w"] >= -25
            if spy_above_50 and g2 and g3:
                eligible4.append((t, f["comp"]))
        eligible4.sort(key=lambda x: x[1], reverse=True)
        seen_layers = set()
        s4_top = []
        for t, _ in eligible4:
            lk = by_ticker[t]["layer"].split("·")[0]
            if lk in seen_layers: continue
            seen_layers.add(lk); s4_top.append(t)
            if len(s4_top) >= TOP_N_S4: break
        holdings["S4"] = [{"ticker": t, "entry_price": feats[t]["close"]} for t in s4_top]

        # S5: Conviction-weighted
        rm = regime_modifier_for(spy_close, di)
        s5_cands = []
        for t, f in feats.items():
            cv, lk = compute_conviction(f, by_ticker[t], rm)
            s5_cands.append((t, cv, lk))
        s5_cands.sort(key=lambda x: x[1], reverse=True)
        layer_strong = set()
        layer_buy = {}
        s5_basket = []
        total_alloc = 0.0
        max_alloc = 0.85
        # v3.2 tightened thresholds matching engine_v32 (STRONG_BUY 85 / BUY 72 / TRY_BUY 58)
        for t, cv, lk in s5_cands:
            if len(s5_basket) >= TOP_N_S5 or total_alloc >= max_alloc: break
            if cv >= 85:
                if lk in layer_strong: continue
                layer_strong.add(lk)
                w = 0.12
            elif cv >= 72:
                if layer_buy.get(lk, 0) >= 2: continue
                layer_buy[lk] = layer_buy.get(lk, 0) + 1
                w = 0.08
            elif cv >= 58:
                w = 0.04
            else:
                continue
            if total_alloc + w > max_alloc:
                w = max_alloc - total_alloc
            s5_basket.append({"ticker": t, "entry_price": feats[t]["close"], "weight": w, "conviction": cv})
            total_alloc += w
        holdings["S5"] = s5_basket

        # S6 v3.4f: 6M-Mom x Conviction hybrid + stability
        #   - min_hold=4 weeks (forced minimum hold period)
        #   - hysteresis: held positions kept while conv>=45 (new entry requires >=50)
        #   - layer cap 2, tiered weights 12%/8%/4%
        MIN_HOLD = 4
        EXIT_CONV = 45
        # Step 1: keep prior holdings that pass hysteresis or min_hold
        prev_basket = holdings.get("S6", [])
        kept = []
        layer_count_s6 = {}
        for h in prev_basket:
            t = h["ticker"]
            if t not in feats:
                continue
            held_weeks = w_idx - h.get("entry_week_idx", w_idx)
            if held_weeks < MIN_HOLD or feats[t]["comp"] >= EXIT_CONV:
                lk = by_ticker[t]["layer"].split("·")[0]
                kept.append({
                    "ticker": t, "entry_price": h["entry_price"],
                    "entry_week_idx": h.get("entry_week_idx", w_idx),
                    "last_price": h.get("last_price", h["entry_price"]),
                    "weight": h.get("weight", 0.04),
                    "conviction": feats[t]["comp"],
                    "mom6m": feats[t].get("avg6m") or 0,
                    "lk": lk,
                })
                layer_count_s6[lk] = layer_count_s6.get(lk, 0) + 1
        # Step 2: fill remaining slots with new candidates
        kept_ticks = {h["ticker"] for h in kept}
        new_eligible = []
        for t, cv, lk in s5_cands:
            if t in kept_ticks: continue
            if cv < 50: continue
            avg6m = feats[t].get("avg6m")
            if avg6m is None or avg6m < 0.01: continue
            new_eligible.append((t, avg6m, cv, lk))
        new_eligible.sort(key=lambda x: x[1], reverse=True)
        for t, mom, cv, lk in new_eligible:
            if len(kept) >= 8: break
            if layer_count_s6.get(lk, 0) >= 2: continue
            kept.append({
                "ticker": t, "entry_price": feats[t]["close"],
                "entry_week_idx": w_idx, "weight": 0.04,
                "last_price": feats[t]["close"],
                "conviction": cv, "mom6m": mom, "lk": lk,
            })
            layer_count_s6[lk] = layer_count_s6.get(lk, 0) + 1
        # Step 3: assign tier weights by mom6m DESC
        kept.sort(key=lambda h: (-h.get("mom6m", 0), -h.get("conviction", 0)))
        for tier_idx, h in enumerate(kept[:8]):
            if tier_idx < 3: h["weight"] = 0.12
            elif tier_idx < 6: h["weight"] = 0.08
            else: h["weight"] = 0.04
        holdings["S6"] = kept[:8]

        # Last week snapshot
        if w_idx == len(bt_weeks) - 1:
            for s, basket in holdings.items():
                last_positions[s] = []
                for h in basket:
                    t = h["ticker"]
                    pos = {
                        "ticker": t, "layer": by_ticker[t]["layer"],
                        "score": round(h.get("conviction", feats[t]["comp"]), 1),
                        "entry_price": round(h["entry_price"], 2),
                        "current_price": round(feats[t]["close"], 2),
                        "return": (feats[t]["close"] / h["entry_price"] - 1),
                    }
                    if "weight" in h:
                        pos["weight_pct"] = round(h["weight"] * 100, 1)
                    last_positions[s].append(pos)

    per_strat = {k + "_metrics": equity_curve_metrics(eq[k], bt_weeks) for k in ["S1", "S2", "S3", "S4", "S5", "S6"]}
    # S6 is now the primary recommended strategy
    s6m = per_strat["S6_metrics"]
    s1m = per_strat["S1_metrics"]
    summary = {
        **per_strat,
        "strategy_total_return": s6m["total_return"],
        "spy_total_return": s1m["total_return"],
        "alpha": s6m["total_return"] - s1m["total_return"],
        "max_drawdown": s6m["max_drawdown"],
        "sharpe_ratio": s6m["sharpe"],
        "win_rate": s6m["win_rate"],
        "n_weeks": len(bt_weeks),
        "top_n": 8,
    }

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": summary,
        "equity_curve": {
            "dates": [d.strftime("%Y-%m-%d") for d in bt_weeks],
            "strategy": eq["S6"],
            "s4_3gate": eq["S4"],
            "s5_conviction": eq["S5"],
            "s6_hybrid": eq["S6"],
            "spy": eq["S1"],
            "s2_momentum_top3": eq["S2"],
            "s3_composite_top10": eq["S3"],
        },
        "latest_positions": last_positions["S6"],
        "strategies": {
            "S1": "Buy & Hold SPY",
            "S2": "6-month avg monthly momentum Top 3",
            "S3": "Composite score Top 10 (no gates)",
            "S4": "Three-Gate + ATR + per-layer cap",
            "S5": "Conviction Engine (graded sizing + regime modifier)",
            "S6": "★ 6M-Mom × Conviction hybrid (filter+rank+cap+ATR)",
        },
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nBacktest done. " + str(len(bt_weeks)) + " weeks.\n")
    print("%-16s %8s %8s %7s %8s %6s" % ("Strategy", "Total", "CAGR", "Sharpe", "MaxDD", "Win%"))
    rows = [("S1", "B&H SPY"), ("S2", "6M-Mom Top3"), ("S3", "Comp Top10"),
            ("S4", "3-Gate+ATR"), ("S5", "Conviction"), ("S6", "* Hybrid 6M+Conv")]
    for s, label in rows:
        m = equity_curve_metrics(eq[s], bt_weeks)
        print("%-16s %7.2f%% %7.2f%% %7.2f %7.2f%% %5.1f%%" %
              (label, m["total_return"]*100, m["cagr"]*100, m["sharpe"],
               m["max_drawdown"]*100, m["win_rate"]*100))


if __name__ == "__main__":
    run()
