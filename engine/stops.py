"""HWM trailing stop ladder (anti-martingale discipline).

Stops monotonically increase, never decrease:
  gain  <  +20%  → anchor × 0.92     (initial -8% hard stop)
  gain >= +20%  → anchor × 1.00     (breakeven)
  gain >= +50%  → HWM × 0.85         (peak -15% trail)
  gain >= +100% → HWM × 0.80         (peak -20% trail)

Any stop trigger → close 100% (no partial exits in anti-martingale).

Persists to my_holdings.json:
  current_stop_price       计算后的当前止损价
  hwm_price                历史最高收盘价 (自买入起)
  stop_label               档位 + 浮盈描述
  gain_pct_from_anchor     相对锚点的浮盈百分比
  peak_conv_while_held     持仓期间 Conv 峰值
  peak_6m_mom_while_held   持仓期间 6M-Mom 峰值
  entry_6m_mom             入场时的 6M-Mom 基准
"""
from __future__ import annotations
import csv
from pathlib import Path


def hwm_per_ticker(holdings, history_csv_path):
    """Read history.csv and find max close since buy_date for each held ticker."""
    out = {}
    p = Path(history_csv_path)
    if not p.exists():
        return out
    try:
        rows_by_t = {}
        with p.open(encoding="utf-8") as f:
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


def compute_stop(h, current_price, hwm):
    """Return (new_stop, label, gain_pct). None if not applicable."""
    if h.get("strategy") != "anti_martingale":
        return None, "n/a (martingale 不设硬止损)", 0
    if h.get("category") in ("etf", "external"):
        return None, "n/a (ETF/external)", 0

    anchor = h.get("step_1_price") or h.get("buy_price")
    if not anchor or anchor <= 0 or not current_price:
        return None, "missing anchor/price", 0

    peak = max(hwm or 0, current_price)
    gain_pct = (current_price / anchor - 1) * 100

    candidates = [anchor * 0.92]
    if gain_pct >= 20:
        candidates.append(anchor * 1.00)
    if gain_pct >= 50:
        candidates.append(peak * 0.85)
    if gain_pct >= 100:
        candidates.append(peak * 0.80)

    prev = h.get("current_stop_price")
    if prev:
        candidates.append(prev)
    new_stop = max(candidates)

    if gain_pct < 20:
        label = f"初始 -8% 硬止损 (浮盈 {gain_pct:+.1f}%, 等 +20% 升保本)"
    elif gain_pct < 50:
        label = f"保本档 (浮盈 {gain_pct:+.1f}%, 等 +50% 切峰值追踪)"
    elif gain_pct < 100:
        label = f"峰值 -15% 追踪 (浮盈 {gain_pct:+.1f}%, 等 +100% 收紧到 -20%)"
    else:
        label = f"峰值 -20% 追踪 (浮盈 {gain_pct:+.1f}%, 让赢家奔跑)"

    return round(new_stop, 2), label, round(gain_pct, 2)


def update_decay_peaks(h, sig_row):
    """Track Conv / 6M-Mom peaks while held + record entry 6M-Mom baseline.

    Used by alerts.py for growth_decay / conv_decay detection.
    """
    if not sig_row:
        return
    cur_conv = sig_row.get("conviction_score")
    cur_mom = sig_row.get("avg_monthly_6m_pct")

    if cur_conv is not None:
        prev_pk = h.get("peak_conv_while_held") or 0
        h["peak_conv_while_held"] = max(prev_pk, cur_conv)
    if cur_mom is not None:
        prev_pk = h.get("peak_6m_mom_while_held")
        if prev_pk is None or cur_mom > prev_pk:
            h["peak_6m_mom_while_held"] = cur_mom
        if "entry_6m_mom" not in h:
            h["entry_6m_mom"] = cur_mom


def latest_price_from_pipeline(t, prices, signals):
    """Get (current_price, sma20) from signals.json first, fallback prices.json."""
    px = sma20 = None
    if signals:
        for r in signals.get("rows", []):
            if r["ticker"] == t:
                px = r.get("latest_price")
                sma20 = r.get("sma_20")
                break
    if px is None and prices:
        p = prices.get("tickers", {}).get(t) or prices.get("extra_prices", {}).get(t)
        if p:
            px = p.get("latest_price")
            sma20 = p.get("sma_20")
    return px, sma20


def update_all_stops(holdings, prices, signals, history_csv_path):
    """Compute new stops for all anti-mart holdings. Returns list of update dicts.

    Mutates `holdings` in place — caller is responsible for persisting.
    """
    hwm_map = hwm_per_ticker(holdings, history_csv_path)
    sig_lookup = {r["ticker"]: r for r in (signals.get("rows") or [])}

    table = []
    for t, h in holdings.items():
        # 衰减峰值跟踪 (无论 strategy, anti-mart 才会被 alerts 用到)
        update_decay_peaks(h, sig_lookup.get(t))

        px, _sma20 = latest_price_from_pipeline(t, prices, signals)
        if not px:
            continue
        hwm = hwm_map.get(t, px)
        prev_hwm = h.get("hwm_price") or 0
        hwm = max(hwm, prev_hwm, px)

        new_stop, label, gain_pct = compute_stop(h, px, hwm)
        if new_stop is None:
            continue

        old_stop = h.get("current_stop_price")
        h["current_stop_price"] = new_stop
        h["hwm_price"] = round(hwm, 2)
        h["stop_label"] = label
        h["gain_pct_from_anchor"] = gain_pct

        anchor = h.get("step_1_price") or h.get("buy_price")
        table.append({
            "ticker": t,
            "anchor": anchor,
            "current_px": px,
            "hwm": hwm,
            "ret_pct": gain_pct,
            "old_stop": old_stop,
            "new_stop": new_stop,
            "label": label,
            "changed": old_stop != new_stop,
        })
    return table
