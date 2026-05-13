"""Compute dynamic stop-loss ratchet for each holding.

Rules (monotonic upward only):
  ret <  +10%  → step_1_price * 0.92        (initial -8%)
  ret >= +10%  → max(initial, step_1_price * 1.00)   (breakeven)
  ret >= +20%  → max(above, step_1_price * 1.05)     (lock +5%)
  ret >= +30%  → max(above, step_1_price * 1.10)     (trail at +10%)
  high_conviction: same rules but +30% also adds SMA20-trail if available.

Persists `current_stop_price` on each holding so dashboard + Fidelity stay in sync.
Run in WSL: python3 scripts/dynamic_stops.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOLDINGS = ROOT / "data" / "my_holdings.json"
SEED_HOLDINGS = ROOT / "data_seed" / "my_holdings.json"
PRICES = ROOT / "data" / "prices.json"
SIGNALS = ROOT / "data" / "signals.json"


def latest_price(t, prices, signals):
    """Get current price + SMA20 for ticker."""
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


def compute_stop(h, current_price, sma20):
    """Return (new_stop, ratchet_label) for the holding."""
    if h.get("strategy") != "anti_martingale":
        return None, "n/a (martingale - no hard stop)"

    # Anchor to step_1_price (first batch), NOT weighted avg
    step1 = h.get("step_1_price") or h.get("batches", [{}])[0].get("price") or h.get("buy_price")
    ret_pct = (current_price / step1 - 1) * 100 if step1 and current_price else 0

    # Compute candidates for each tier (monotonic max)
    initial = step1 * 0.92
    be = step1 * 1.00
    lock5 = step1 * 1.05
    lock10 = step1 * 1.10

    candidates = [initial]
    if ret_pct >= 10: candidates.append(be)
    if ret_pct >= 20: candidates.append(lock5)
    if ret_pct >= 30:
        candidates.append(lock10)
        if sma20: candidates.append(sma20)

    # Also: never lower than previously persisted stop (high-water-mark)
    prev = h.get("current_stop_price")
    if prev:
        candidates.append(prev)

    new_stop = max(candidates)

    # Label which ratchet tier we're in
    if ret_pct < 10: label = f"初始 -8% (浮盈 {ret_pct:+.1f}%)"
    elif ret_pct < 20: label = f"保本档 (浮盈 {ret_pct:+.1f}%, 等 +20%)"
    elif ret_pct < 30: label = f"锁+5% (浮盈 {ret_pct:+.1f}%, 等 +30%)"
    else: label = f"追踪 SMA20/+10% (浮盈 {ret_pct:+.1f}%)"
    return round(new_stop, 2), label


def main():
    hh = json.loads(HOLDINGS.read_text(encoding="utf-8"))
    prices = json.loads(PRICES.read_text(encoding="utf-8")) if PRICES.exists() else {}
    signals = json.loads(SIGNALS.read_text(encoding="utf-8")) if SIGNALS.exists() else {}

    updates = []
    table = []
    for t, h in hh.items():
        px, sma20 = latest_price(t, prices, signals)
        if not px: continue
        new_stop, label = compute_stop(h, px, sma20)
        if new_stop is None: continue
        old_stop = h.get("current_stop_price")
        h["current_stop_price"] = new_stop
        step1 = h.get("step_1_price") or h.get("buy_price")
        table.append({
            "ticker": t,
            "step1": step1,
            "current_px": px,
            "ret_pct": (px / step1 - 1) * 100 if step1 else 0,
            "old_stop": old_stop,
            "new_stop": new_stop,
            "label": label,
        })
        if old_stop != new_stop:
            updates.append(f"  {t}: {old_stop} → {new_stop} ({label})")

    HOLDINGS.write_text(json.dumps(hh, ensure_ascii=False, indent=2), encoding="utf-8")
    SEED_HOLDINGS.write_text(json.dumps(hh, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"=== Dynamic stop ratchet ({len(table)} anti-martingale holdings) ===\n")
    print(f"{'Ticker':<8} {'Step1':>8} {'Now':>8} {'Ret':>7} {'OldStop':>9} {'NewStop':>9}  {'Status'}")
    print("-" * 90)
    for r in table:
        print(f"{r['ticker']:<8} ${r['step1']:>7.2f} ${r['current_px']:>7.2f} "
              f"{r['ret_pct']:>+6.1f}% {('$'+str(r['old_stop'])) if r['old_stop'] else '   -   ':>9} "
              f"${r['new_stop']:>8.2f}  {r['label']}")

    if updates:
        print(f"\nChanges ({len(updates)}):")
        for u in updates: print(u)

    # === Fidelity 行动表 ===
    print(f"\n=== Fidelity OCO 更新指引 ===\n")
    print("Anti-martingale (high_conviction): 只挂 Stop Loss")
    print("Anti-martingale (ladder):          挂 OCO (Stop + Limit @ +30%)\n")
    for r in table:
        h = hh[r['ticker']]
        hc = h.get("high_conviction", False)
        tag = "🔥 high_conviction" if hc else "📊 ladder"
        print(f"  {r['ticker']:<6} {tag}  STOP: ${r['new_stop']:.2f}", end="")
        if not hc:
            tp = (r['step1'] * 1.30)
            sell_q = max(1, int(h["shares"] * 0.33))
            print(f"   TP: ${tp:.2f}  (Sell {sell_q} 股)")
        else:
            print(f"   TP: 无 (骑趋势)")


if __name__ == "__main__":
    main()
