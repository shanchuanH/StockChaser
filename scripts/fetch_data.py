"""Fetch 24-month daily price history for the AI-chain universe via yfinance.

Now also computes ATR(20) and 20-day average volume.
Stores both summary stats AND the full price history (compact form), so the
backtest engine can replay history without re-fetching.

Outputs:
  data/prices.json         - latest summary stats per ticker
  data/history.parquet     - full daily OHLCV for backtest
"""
from __future__ import annotations
import json, sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIVERSE = ROOT / "data" / "universe.json"
OUT = ROOT / "data" / "prices.json"
HIST = ROOT / "data" / "history.parquet"


def true_range(high, low, prev_close):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def fetch():
    try:
        import yfinance as yf
        import pandas as pd
        import numpy as np
    except ImportError:
        print("ERROR: pip install yfinance pandas numpy openpyxl pyarrow")
        sys.exit(1)

    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    tickers = sorted(set([u["ticker"] for u in universe] + ["SPY", "QQQ"]))
    print(f"Fetching {len(tickers)} tickers (24 months)…")

    end = datetime.utcnow()
    start = end - timedelta(days=730)
    df = yf.download(
        tickers,
        start=start.strftime("%Y-%m-%d"),
        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
        auto_adjust=True, progress=True, group_by="ticker", threads=True,
    )

    # Build a long-format history dataframe and persist it
    long_rows = []
    for t in tickers:
        try:
            sub = df[t][["Open", "High", "Low", "Close", "Volume"]].dropna()
        except Exception:
            continue
        for date, row in sub.iterrows():
            long_rows.append({
                "ticker": t, "date": date.strftime("%Y-%m-%d"),
                "open": float(row.Open), "high": float(row.High),
                "low": float(row.Low), "close": float(row.Close),
                "volume": float(row.Volume),
            })
    hist_df = pd.DataFrame(long_rows)
    HIST.parent.mkdir(exist_ok=True)
    try:
        hist_df.to_parquet(HIST, index=False)
    except Exception:
        # parquet engine missing → fallback CSV
        hist_df.to_csv(HIST.with_suffix(".csv"), index=False)
        print(f"  (parquet engine missing, fell back to {HIST.with_suffix('.csv').name})")

    # SPY benchmark return
    spy_close = df["SPY"]["Close"].dropna()
    spy_ret_4w = float((spy_close.iloc[-1] / spy_close.iloc[-21] - 1) * 100) if len(spy_close) >= 21 else 0.0
    spy_above_sma50 = bool(spy_close.iloc[-1] > spy_close.iloc[-50:].mean()) if len(spy_close) >= 50 else False

    out = {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "spy_4w_return_pct": round(spy_ret_4w, 2),
        "spy_above_sma50": spy_above_sma50,
        "is_mock": False,
        "tickers": {},
    }

    for u in universe:
        t = u["ticker"]
        try:
            sub = df[t][["Open", "High", "Low", "Close", "Volume"]].dropna()
        except Exception:
            print(f"  ! {t}: no data")
            continue
        if len(sub) < 30:
            continue
        close = sub["Close"]; high = sub["High"]; low = sub["Low"]; vol = sub["Volume"]
        last = float(close.iloc[-1])

        def ret(n):
            if len(close) <= n: return None
            return round(float((close.iloc[-1] / close.iloc[-n - 1] - 1) * 100), 2)

        # ATR(20) using Wilder's smoothing approximation = simple mean of TR
        tr = [true_range(float(high.iloc[i]), float(low.iloc[i]), float(close.iloc[i-1])) for i in range(1, len(sub))]
        atr_20 = float(sum(tr[-20:]) / 20) if len(tr) >= 20 else float(sum(tr)/len(tr))

        # 6m monthly avg
        if len(close) >= 126:
            monthly = close.iloc[::-1].iloc[::21][:7][::-1]
            mret = monthly.pct_change().dropna() * 100
            avg6m = round(float(mret.mean()), 2) if len(mret) > 0 else None
        else:
            avg6m = None

        high_4w = float(close.iloc[-20:].max())
        high_52w = float(close.iloc[-252:].max()) if len(close) >= 252 else float(close.max())
        sma_20 = float(close.iloc[-20:].mean())
        sma_50 = float(close.iloc[-50:].mean()) if len(close) >= 50 else None
        vol_20 = float(vol.iloc[-20:].mean())
        vol_5  = float(vol.iloc[-5:].mean())

        m_ret = ret(21)
        out["tickers"][t] = {
            "ticker": t,
            "latest_date": str(close.index[-1].date()),
            "latest_price": round(last, 2),
            "daily_return_pct": ret(1),
            "week_return_pct": ret(5),
            "month_return_pct": m_ret,
            "m3_return_pct": ret(63),
            "m6_return_pct": ret(126),
            "avg_monthly_6m_pct": avg6m,
            "high_4w": round(high_4w, 2),
            "high_52w": round(high_52w, 2),
            "dist_4w_high_pct": round((last/high_4w - 1) * 100, 2),
            "dist_52w_high_pct": round((last/high_52w - 1) * 100, 2),
            "sma_20": round(sma_20, 2),
            "sma_50": round(sma_50, 2) if sma_50 else None,
            "above_sma_20": last > sma_20,
            "above_sma_50": (last > sma_50) if sma_50 else None,
            "rs_vs_spy_4w_pp": round((m_ret - spy_ret_4w), 2) if m_ret is not None else None,
            "atr_20": round(atr_20, 2),
            "atr_pct": round(atr_20 / last * 100, 2),
            "avg_volume_20d": int(vol_20),
            "vol_ratio_5_20": round(vol_5 / vol_20, 2) if vol_20 > 0 else None,
        }

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote prices for {len(out['tickers'])} tickers → {OUT}")
    print(f"Saved history → {HIST}")
    print(f"SPY 4-week: {spy_ret_4w:+.2f}%   above SMA50: {spy_above_sma50}")


if __name__ == "__main__":
    fetch()
