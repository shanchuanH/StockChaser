"""Backtest 5 strategies on the AI-chain universe.

Strategies:
  S1  Buy & Hold SPY                          (baseline)
  S2  6-month avg monthly momentum, Top 3     (the article)
  S3  Composite score Top 10 (no gates)
  S4  Three-Gate + ATR sizing  (v2 — too binary)
  S5  Conviction Engine ★ NEW v3
        - Graded position: STRONG_BUY 12% / BUY 8% / TRY_BUY 4%
        - Regime modifier scales positions instead of binary cutoff
        - Per-layer cap: max 1 strong-buy per layer
        - Up to 8 concurrent positions

Mechanics:
  - Weekly rebalancing every Monday close
  - Entry/Exit at next Monday's close (no look-ahead)
  - Equal-weight inside each strategy's basket
  - Transaction cost: 5bps per trade (round-trip ~10bps)

Outputs:
  data/backtest_results.json  (matches the schema the HTML expects)
"""
from __future__ import annotations
import json
import math
from collections import Counter
from pathlib import Path
from datetime import datetime
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "data" / "universe.json"
HIST_PARQ = ROOT / "data" / "history.parquet"
HIST_CSV = ROOT / "data" / "history.csv"
OUT = ROOT / "data" / "backtest_results.json"

TXN_COST = 0.0005  # 5bps per side
TOP_N_S2 = 3
TOP_N_S3 = 10
TOP_N_S4 = 5
LOOKBACK_DAYS = 126   # 6 months
MIN_BARS_BEFORE_START = 252   # need 1 year before backtest begins


def load_history():
    try:
        import pandas as pd
        if HIST_PARQ.exists():
            df = pd.read_parquet(HIST_PARQ)
        else:
            df = pd.read_csv(HIST_CSV)
        df["date"] = pd.to_datetime(df["date"])
        return df
    except ImportError:
        # plain Python fallback
        import csv
        rows = []
        with open(HIST_CSV) as f:
            r = csv.DictReader(f)
            for row in r:
                rows.append(row)
        return rows


def to_pivot(df):
    """Return (dates_sorted, dict[ticker -> {date_idx -> close}])."""
    import pandas as pd
    pv = df.pivot(index="date", columns="ticker", values="close")
    pv = pv.sort_index()
    return pv


def weekly_dates(pv):
    """Return Mondays from the index (or first trading day of each week)."""
    dates = list(pv.index)
    weeks = {}
    for d in dates:
        # ISO week → first day we see in that week
        key = (d.isocalendar().year, d.isocalendar().week)
        if key not in weeks:
            weeks[key] = d
    return [weeks[k] for k in sorted(weeks)]


def avg_monthly_6m(close_series, end_idx, monthly_step=21, n_months=6):
    """6-month average monthly return at end_idx."""
    pts = []
    for k in range(n_months + 1):
        idx = end_idx - k * monthly_step
        if idx < 0:
            return None
        pts.append(close_series[idx])
    pts = list(reversed(pts))
    rets = [(pts[i] / pts[i - 1] - 1) for i in range(1, len(pts))]
    return sum(rets) / len(rets)


def compute_atr(high, low, close, end_idx, n=20):
    if end_idx < n:
        return None
    tr = []
    for i in range(end_idx - n + 1, end_idx + 1):
        tr.append(max(high[i] - low[i],
                      abs(high[i] - close[i - 1]),
                      abs(low[i] - close[i - 1])))
    return sum(tr) / n


def equity_curve_metrics(equity, dates):
    if not equity or len(equity) < 2:
        return {}
    total_return = equity[-1] / equity[0] - 1
    # annualized
    n_weeks = len(equity)
    n_years = n_weeks / 52
    cagr = (equity[-1] / equity[0]) ** (1 / max(n_years, 0.01)) - 1
    # weekly returns
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
    # max drawdown
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = e / peak - 1
        if dd < max_dd:
            max_dd = dd
    return {
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "win_rate": win_rate,
        "max_drawdown": max_dd,
        "n_weeks": n_weeks,
    }


def run():
    import pandas as pd
    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    by_ticker = {u["ticker"]: u for u in universe}

    df = load_history()
    pv_close = df.pivot(index="date", columns="ticker", values="close").sort_index()
    pv_high  = df.pivot(index="date", columns="ticker", values="high").sort_index()
    pv_low   = df.pivot(index="date", columns="ticker", values="low").sort_index()
    pv_close = pv_close.fillna(method="ffill")

    dates = list(pv_close.index)
    weeks = weekly_dates(pv_close)
    # Start backtest only after enough history for momentum lookbacks
    start_idx = next(i for i, d in enumerate(weeks) if dates.index(d) >= MIN_BARS_BEFORE_START)
    bt_weeks = weeks[start_idx:]
    print(f"Backtest from {bt_weeks[0].date()} to {bt_weeks[-1].date()}  ({len(bt_weeks)} weeks)")

    # Build per-ticker arrays (close/high/low) for indexing speed
    tickers = [t for t in pv_close.columns if t != "QQQ"]
    close_arr = {t: pv_close[t].fillna(method="ffill").tolist() for t in tickers}
    high_arr = {t: pv_high[t].fillna(method="ffill").tolist() for t in tickers}
    low_arr = {t: pv_low[t].fillna(method="ffill").tolist() for t in tickers}
    spy_close = close_arr["SPY"]

    # Pre-compute fundamental scores
    fund_score = {}
    for u in universe:
        s = (u.get("potential", 0) or 0) * 18
        s += {"龙头": 8, "二线": 4, "概念": 0}.get(u.get("role"), 0)
        fund_score[u["ticker"]] = min(100, s)

    # Equity curves for each strategy
    eq = {"S1": [1.0], "S2": [1.0], "S3": [1.0], "S4": [1.0], "S5": [1.0], "SPY": [1.0]}
    holdings = {"S2": [], "S3": [], "S4": [], "S5": []}  # current basket; for S5 also has 'weight'
    last_positions = {"S2": [], "S3": [], "S4": [], "S5": []}

    universe_tickers = [t for t in tickers if t in by_ticker]

    for w_idx, week_date in enumerate(bt_weeks):
        di = dates.index(week_date)
        if di < MIN_BARS_BEFORE_START:
            continue

        # ---- Compute features at di ----
        feats = {}
        for t in universe_tickers:
            cl = close_arr[t]
            if di >= len(cl) or cl[di] is None or math.isnan(cl[di]):
                continue
            last = cl[di]
            if last <= 0:
                continue

            # Returns
            def ret(n):
                if di - n < 0:
                    return None
                base = cl[di - n]
                return (last / base - 1) * 100 if base and not math.isnan(base) else None

            wk = ret(5)
            mo = ret(21)
            m3 = ret(63)
            spy_4w = (spy_close[di] / spy_close[di - 21] - 1) * 100 if di >= 21 else 0
            rs = (mo - spy_4w) if (mo is not None and spy_4w is not None) else 0

            # 6-month avg monthly
            avg6m = avg_monthly_6m(cl, di)

            # 4w high
            high_4w = max(cl[max(0, di - 19):di + 1])
            # 52w high
            high_52w = max(cl[max(0, di - 252):di + 1])
            # SMA20/50
            sma20 = sum(cl[max(0, di - 19):di + 1]) / min(20, di + 1)
            sma50 = sum(cl[max(0, di - 49):di + 1]) / min(50, di + 1)
            # ATR
            atr = compute_atr(high_arr[t], low_arr[t], cl, di)

            # Composite (mirror compute_signals.py)
            mom_s = 50 + (wk or 0) * 1.5 + (mo or 0) * 0.8 + (m3 or 0) * 0.4 + rs * 0.6
            if last > sma20: mom_s += 5
            if last > sma50: mom_s += 5
            dist_4w_pct = (last / high_4w - 1) * 100
            if dist_4w_pct < -10: mom_s += dist_4w_pct * 0.5
            mom_s = max(0, min(100, mom_s))
            comp = mom_s * 0.6 + fund_score.get(t, 0) * 0.4

            feats[t] = {
                "close": last, "wk": wk, "mo": mo, "m3": m3,
                "rs": rs, "avg6m": avg6m, "atr": atr,
                "high_4w": high_4w, "high_52w": high_52w,
                "sma20": sma20, "sma50": sma50,
                "comp": comp, "dist_4w": dist_4w_pct,
                "dist_52w": (last / high_52w - 1) * 100,
            }

        # ---- Realize last week's holdings PnL ----
        if w_idx > 0:
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
                    continue
                avg_ret = sum(rets) / len(rets)
                eq[s].append(eq[s][-1] * (1 + avg_ret - 2 * TXN_COST))

            # S5: weighted by conviction-tier
            basket5 = holdings["S5"]
            if not basket5:
                eq["S5"].append(eq["S5"][-1])
            else:
                weighted_ret = 0.0
                total_weight = sum(h.get("weight", 0) for h in basket5)
                cash_weight = max(0, 1 - total_weight)
                for h in basket5:
                    cl = close_arr[h["ticker"]]
                    if di < len(cl) and not math.isnan(cl[di]):
                        weighted_ret += (cl[di] / h["entry_price"] - 1) * h["weight"]
                # Cash earns 0; transaction costs only on invested portion
                eq["S5"].append(eq["S5"][-1] * (1 + weighted_ret - 2 * TXN_COST * total_weight))

            # SPY (S1) just buy & hold
            ret_spy = spy_close[di] / spy_close[dates.index(bt_weeks[w_idx - 1])] - 1
            eq["S1"].append(eq["S1"][-1] * (1 + ret_spy))
            eq["SPY"].append(eq["SPY"][-1] * (1 + ret_spy))
        else:
            for s in ["S1", "SPY"]:
                pass  # already initialized at 1.0
        # Make sure all curves stay aligned in length
        target_len = w_idx + 1
        for k in eq:
            while len(eq[k]) < target_len:
                eq[k].append(eq[k][-1])

        # ---- Pick new baskets ----
        # S2: top-N by 6M avg monthly return
        s2_pool = [(t, f["avg6m"]) for t, f in feats.items() if f["avg6m"] is not None]
        s2_pool.sort(key=lambda x: x[1], reverse=True)
        s2_top = [t for t, _ in s2_pool[:TOP_N_S2]]
        holdings["S2"] = [{"ticker": t, "entry_price": feats[t]["close"]} for t in s2_top]

        # S3: top-N by composite (no gates)
        s3_pool = [(t, f["comp"]) for t, f in feats.items()]
        s3_pool.sort(key=lambda x: x[1], reverse=True)
        s3_top = [t for t, _ in s3_pool[:TOP_N_S3]]
        holdings["S3"] = [{"ticker": t, "entry_price": feats[t]["close"]} for t in s3_top]

        # S4: three-gate + per-layer cap + composite ranking
        spy_above_50 = spy_close[di] > sum(spy_close[max(0, di-49):di+1]) / min(50, di+1)
        eligible = []
        for t, f in feats.items():
            # gate 2: 2-of-3 startup
            s_a = f["close"] >= f["high_4w"] * 0.99
            s_b = (f["wk"] or 0) >= 3
            # vol surge requires 5d vs 20d average — approximate from history later
            s_c = False  # we don't have volume in feats; skip — (gate is more conservative)
            g2 = sum([s_a, s_b, s_c]) >= 1  # relaxed to 1-of-2 since we lack vol
            # gate 3: quality
            g3 = f["comp"] >= 80 and f["rs"] >= 0 and f["dist_52w"] >= -25
            if spy_above_50 and g2 and g3:
                eligible.append((t, f["comp"]))
        eligible.sort(key=lambda x: x[1], reverse=True)
        # Per-layer cap: max 1 per layer
        seen_layers = set()
        s4_top = []
        for t, _ in eligible:
            layer = by_ticker[t]["layer"].split("·")[0]
            if layer in seen_layers:
                continue
            seen_layers.add(layer)
            s4_top.append(t)
            if len(s4_top) >= TOP_N_S4:
                break
        holdings["S4"] = [{"ticker": t, "entry_price": feats[t]["close"]} for t in s4_top]

        # ---- S5: Conviction-weighted strategy ----
        # Compute conviction sub-scores at this point in time
        spy_4w_now = (spy_close[di] / spy_close[di - 21] - 1) * 100 if di >= 21 else 0
        if spy_above_50 and spy_4w_now >= 2:    rm = 1.20
        elif spy_above_50 and spy_4w_now >= 0:  rm = 1.05
        elif spy_above_50:                       rm = 0.90
        elif -3 <= spy_4w_now < 0:               rm = 0.70
        elif spy_4w_now < -5:                    rm = 0.30
        else:                                     rm = 0.55

        s5_candidates = []
        for t, f in feats.items():
            # Trend strength
            ts_s = 50
            sma20_v = f["sma20"]; sma50_v = f["sma50"]
            if f["close"] > sma20_v: ts_s += 8
            if f["close"] > sma50_v: ts_s += 8
            d4 = f["dist_4w"]
            if d4 >= -1: ts_s += 15
            elif d4 >= -5: ts_s += 8
            elif d4 < -15: ts_s -= 12
            rs_v = f["rs"]
            if rs_v >= 8: ts_s += 12
            elif rs_v >= 3: ts_s += 6
            elif rs_v <= -5: ts_s -= 10
            aligned = sum(1 for x in (f["wk"], f["mo"], f["m3"]) if (x or 0) > 0)
            ts_s += [0, 2, 5, 10][min(aligned, 3)]
            ts_s = max(0, min(100, ts_s))

            # Entry quality
            eq_s = 50
            wkv = f["wk"] or 0
            if 3 <= wkv <= 8: eq_s += 18
            elif 8 < wkv <= 12: eq_s += 6
            elif wkv > 12: eq_s -= 8
            elif 0 <= wkv < 3: eq_s += 4
            elif wkv < -3: eq_s -= 12
            if d4 > 3: eq_s -= 8
            if sma20_v and f["close"] / sma20_v > 1.12: eq_s -= 15
            elif sma20_v and f["close"] / sma20_v > 1.08: eq_s -= 7
            eq_s = max(0, min(100, eq_s))

            # Fundamental
            u = by_ticker[t]
            fc_s = (u.get("potential", 0) or 0) * 12 + {"龙头": 12, "二线": 6, "概念": 0}.get(u.get("role"), 0)
            lay