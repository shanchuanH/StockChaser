"""Generate synthetic 24-month price history + summary stats.

Used when offline / for backtest dev. Same schema as fetch_data.py.
Writes:
  data/prices.json
  data/history.parquet (or .csv if pyarrow missing)
"""
from __future__ import annotations
import csv
import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "data" / "universe.json"
OUT = ROOT / "data" / "prices.json"
HIST = ROOT / "data" / "history.parquet"


def gbm(start: float, n: int, mu: float, sigma: float) -> list[float]:
    """Geometric Brownian Motion daily prices."""
    out = [start]
    for _ in range(n - 1):
        out.append(out[-1] * math.exp(random.gauss(mu / 252, sigma / math.sqrt(252))))
    return out


def main():
    random.seed(42)
    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))

    # 24 months ≈ 504 trading days, ending 2026-05-08 (Friday)
    n_days = 504
    end_date = datetime(2026, 5, 8)
    dates = []
    d = end_date
    while len(dates) < n_days:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    dates = list(reversed(dates))

    layer_bias = {
        "L1": 0.6, "L2": 1.0, "L3": 0.8, "L4": 1.2, "L5": 0.3,
        "L6": 0.7, "L7": 0.4, "L8": 0.5, "L9": 0.4, "L10": 1.1,
        "L11": 0.9, "L12": -0.2, "L13": -0.5,
    }

    spy_prices = gbm(420.0, n_days, mu=0.10, sigma=0.16)
    qqq_prices = gbm(380.0, n_days, mu=0.14, sigma=0.22)

    cols = ["ticker", "date", "open", "high", "low", "close", "volume"]
    rows = []
    for date, p in zip(dates, spy_prices):
        rows.append(("SPY", date, p * 0.998, p * 1.005, p * 0.992, p, 80_000_000))
    for date, p in zip(dates, qqq_prices):
        rows.append(("QQQ", date, p * 0.998, p * 1.005, p * 0.992, p, 50_000_000))

    for u in universe:
        t = u["ticker"]
        layer_key = u["layer"].split("·")[0]
        bias = layer_bias.get(layer_key, 0)
        role_bonus = {"龙头": 0.05, "二线": 0.0, "概念": -0.05}.get(u["role"], 0.0)
        pot_bonus = (u["potential"] - 3) * 0.04
        mu = 0.08 + bias * 0.06 + role_bonus + pot_bonus
        sigma = 0.30 + abs(bias) * 0.06 + (0.10 if u["role"] == "概念" else 0)
        start_p = random.uniform(20, 800)
        prices = gbm(start_p, n_days, mu=mu, sigma=sigma)
        for date, p in zip(dates, prices):
            o = p * (1 + random.gauss(0, 0.005))
            h = p * (1 + abs(random.gauss(0.005, 0.005)))
            lo = p * (1 - abs(random.gauss(0.005, 0.005)))
            v = random.randint(1_000_000, 50_000_000)
            rows.append((t, date, o, h, lo, p, v))

    # Persist history
    out_path = persist_history(rows, cols)

    # Latest summary stats
    by_t = {}
    for r in rows:
        by_t.setdefault(r[0], []).append(r)

    spy_close = [r[5] for r in by_t["SPY"]]
    spy_4w = round((spy_close[-1] / spy_close[-21] - 1) * 100, 2)
    spy_above_50 = spy_close[-1] > sum(spy_close[-50:]) / 50

    out = {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "spy_4w_return_pct": spy_4w,
        "spy_above_sma50": spy_above_50,
        "is_mock": True,
        "tickers": {},
    }

    for u in universe:
        t = u["ticker"]
        series = by_t.get(t)
        if not series or len(series) < 30:
            continue
        closes = [r[5] for r in series]
        highs = [r[3] for r in series]
        lows = [r[4] for r in series]
        vols = [r[6] for r in series]
        last = closes[-1]

        def ret(n):
            if len(closes) <= n:
                return None
            return round((closes[-1] / closes[-n - 1] - 1) * 100, 2)

        tr = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
              for i in range(1, len(series))]
        atr_20 = sum(tr[-20:]) / 20

        if len(closes) >= 126:
            monthly = closes[-126::21]
            mret = [(monthly[i] / monthly[i - 1] - 1) * 100 for i in range(1, len(monthly))]
            avg6m = round(sum(mret) / len(mret), 2) if mret else None
        else:
            avg6m = None

        high_4w = max(closes[-20:])
        high_52w = max(closes[-252:]) if len(closes) >= 252 else max(closes)
        sma_20 = sum(closes[-20:]) / 20
        sma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
        vol_20 = sum(vols[-20:]) / 20
        vol_5 = sum(vols[-5:]) / 5
        m_ret = ret(21)

        out["tickers"][t] = {
            "ticker": t,
            "latest_date": series[-1][1],
            "latest_price": round(last, 2),
            "daily_return_pct": ret(1),
            "week_return_pct": ret(5),
            "month_return_pct": m_ret,
            "m3_return_pct": ret(63),
            "m6_return_pct": ret(126),
            "avg_monthly_6m_pct": avg6m,
            "high_4w": round(high_4w, 2),
            "high_52w": round(high_52w, 2),
            "dist_4w_high_pct": round((last / high_4w - 1) * 100, 2),
            "dist_52w_high_pct": round((last / high_52w - 1) * 100, 2),
            "sma_20": round(sma_20, 2),
            "sma_50": round(sma_50, 2) if sma_50 else None,
            "above_sma_20": last > sma_20,
            "above_sma_50": last > sma_50 if sma_50 else None,
            "rs_vs_spy_4w_pp": round(m_ret - spy_4w, 2) if m_ret is not None else None,
            "atr_20": round(atr_20, 2),
            "atr_pct": round(atr_20 / last * 100, 2),
            "avg_volume_20d": int(vol_20),
            "vol_ratio_5_20": round(vol_5 / vol_20, 2) if vol_20 > 0 else None,
        }

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote MOCK prices for {len(out['tickers'])} tickers -> {OUT}")
    print(f"Saved history -> {out_path} ({len(rows):,} rows)")
    print(f"SPY 4w mock: {spy_4w:+.2f}%, above SMA50: {spy_above_50}")


def persist_history(rows, cols):
    try:
        import pandas as pd
    except ImportError:
        path = HIST.with_suffix(".csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
        return path
    df = pd.DataFrame(rows, columns=cols)
    try:
        df.to_parquet(HIST, index=False)
        return HIST
    except Exception:
        path = HIST.with_suffix(".csv")
        df.to_csv(path, index=False)
        return path


if __name__ == "__main__":
    main()
