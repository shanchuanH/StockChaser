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
from datetime import datetime, timedelta, timezone
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
    except ImportError:
        print("ERROR: pip install yfinance pandas openpyxl pyarrow")
        sys.exit(1)

    universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    # v3.7: also fetch ETF / external positions from my_holdings.json
    # so the portfolio view shows real P&L for buy-and-hold ETFs / HIMX.
    extra_tickers = set()
    holdings_path = ROOT / "data" / "my_holdings.json"
    if holdings_path.exists():
        try:
            holdings = json.loads(holdings_path.read_text(encoding="utf-8"))
            universe_tickers_set = {u["ticker"] for u in universe}
            for tk, hh in holdings.items():
                if hh.get("category") in ("etf", "external") and tk not in universe_tickers_set:
                    extra_tickers.add(tk)
        except Exception:
            pass
    tickers = sorted(set([u["ticker"] for u in universe] + ["SPY", "QQQ"]) | extra_tickers)
    print(f"Fetching {len(tickers)} tickers ({len(extra_tickers)} extras for ETF tracking)…")

    end = datetime.now(timezone.utc)
    # 36 months — covers 2022 bear market for stress-testing strategies
    start = end - timedelta(days=1100)
    df = yf.download(
        tickers,
        start=start.strftime("%Y-%m-%d"),
        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
        auto_adjust=True, progress=True, group_by="ticker", threads=True,
    )

    # Build long-format history (batch per ticker — no iterrows loop)
    pieces = []
    for t in tickers:
        try:
            sub = df[t][["Open", "High", "Low", "Close", "Volume"]].dropna(how="any")
        except (KeyError, TypeError, AttributeError):
            continue
        if len(sub) == 0:
            continue
        piece = sub.reset_index()
        date_col = piece.columns[0]
        piece = piece.rename(columns={
            date_col: "date",
            "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume",
        })
        piece["ticker"] = t
        piece["date"] = pd.to_datetime(piece["date"]).dt.strftime("%Y-%m-%d")
        for col in ("open", "high", "low", "close", "volume"):
            piece[col] = pd.to_numeric(piece[col], errors="coerce")
        piece = piece.dropna(subset=["open", "high", "low", "close", "volume"])
        pieces.append(piece[["ticker", "date", "open", "high", "low", "close", "volume"]])
    hist_df = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame(
        columns=["ticker", "date", "open", "high", "low", "close", "volume"],
    )

    HIST.parent.mkdir(exist_ok=True)
    # Always write CSV (universally readable). Try parquet too if engine available.
    hist_df.to_csv(HIST.with_suffix(".csv"), index=False)
    try:
        hist_df.to_parquet(HIST, index=False)
    except Exception:
        print(f"  (no parquet engine — CSV only at {HIST.with_suffix('.csv').name})")

    # SPY benchmark — used for regime detection
    spy_close = df["SPY"]["Close"].dropna()
    spy_ret_4w = float((spy_close.iloc[-1] / spy_close.iloc[-21] - 1) * 100) if len(spy_close) >= 21 else 0.0
    spy_above_sma50 = bool(spy_close.iloc[-1] > spy_close.iloc[-50:].mean()) if len(spy_close) >= 50 else False
    # NEW v3.5: SMA200 is the bull/bear dividing line — used for kill switch
    spy_above_sma200 = bool(spy_close.iloc[-1] > spy_close.iloc[-200:].mean()) if len(spy_close) >= 200 else True
    spy_dist_sma200_pct = round(float((spy_close.iloc[-1] / spy_close.iloc[-200:].mean() - 1) * 100), 2) if len(spy_close) >= 200 else 0.0

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "spy_4w_return_pct": round(spy_ret_4w, 2),
        "spy_above_sma50": spy_above_sma50,
        "spy_above_sma200": spy_above_sma200,
        "spy_dist_sma200_pct": spy_dist_sma200_pct,
        "is_mock": False,
        "tickers": {},
        "extra_prices": {},
    }

    # Fetch ETF / external ticker prices for portfolio P&L (no strategy)
    for tk in sorted(extra_tickers):
        try:
            sub_e = df[tk][["Close"]].dropna()
            if len(sub_e) < 1:
                continue
            close_e = sub_e["Close"]
            last_e = float(close_e.iloc[-1])
            wk_e = round(float((close_e.iloc[-1] / close_e.iloc[-5] - 1) * 100), 2) if len(close_e) >= 5 else None
            mo_e = round(float((close_e.iloc[-1] / close_e.iloc[-21] - 1) * 100), 2) if len(close_e) >= 21 else None
            out["extra_prices"][tk] = {
                "latest_price": round(last_e, 2),
                "latest_date": str(close_e.index[-1].date()),
                "week_return_pct": wk_e,
                "month_return_pct": mo_e,
            }
        except Exception as e:
            print(f"  ! extra {tk}: {e}")

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
