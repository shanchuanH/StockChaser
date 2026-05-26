"""动态止损追踪 — 反马丁纪律 v2 (HWM-based)

纪律 (anti-martingale, 只升不降):
  浮盈 < +20%   → 入场价 × 0.92          (初始 -8% 硬止损)
  浮盈 ≥ +20%   → 保本 (入场价)
  浮盈 ≥ +50%   → max(prev, HWM × 0.85)  (峰值回撤 15% 退出)
  浮盈 ≥ +100%  → max(prev, HWM × 0.80)  (峰值回撤 20% 退出)

任何一档触发 → 全清 (反马丁不分批退场)。
HWM = 自买入日起的最高收盘价 (取自 history.csv)。
止损价单调递增，不允许下移。

Persists `current_stop_price` 和 `hwm_price` 到 my_holdings.json。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOLDINGS = ROOT / "data" / "my_holdings.json"
SEED_HOLDINGS = ROOT / "data_seed" / "my_holdings.json"
PRICES = ROOT / "data" / "prices.json"
SIGNALS = ROOT / "data" / "signals.json"
HISTORY_CSV = ROOT / "data" / "history.csv"


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


def hwm_per_ticker(holdings):
    """读 history.csv 算每只持仓自买入日起的最高收盘价 (high-water mark)."""
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
                d = row.get("date") or ""
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
    except Exception as exc:
        print(f"  hwm calc failed: {exc}")
    return out


def compute_stop(h, current_price, hwm):
    """Return (new_stop, label, gain_pct) for the holding.

    反马丁纪律: HWM 追踪止损, 不设固定止盈。
    任何一档触发 = 全清。
    """
    if h.get("strategy") != "anti_martingale":
        return None, "n/a (martingale - 不设硬止损)", 0
    if h.get("category") in ("etf", "external"):
        return None, "n/a (ETF/external)", 0

    # 锚点: 优先 step_1_price (首批入场价), 否则 buy_price
    anchor = h.get("step_1_price") or h.get("buy_price")
    if not anchor or anchor <= 0 or not current_price:
        return None, "missing anchor/price", 0

    # 用 max(current, hwm) 防止 history.csv 比当前价滞后
    peak = max(hwm or 0, current_price)
    gain_pct = (current_price / anchor - 1) * 100

    # 候选止损价 (取最大值, 单调递增)
    candidates = [anchor * 0.92]  # 初始 -8%

    if gain_pct >= 20:
        candidates.append(anchor * 1.00)  # 保本
    if gain_pct >= 50:
        candidates.append(peak * 0.85)    # HWM -15%
    if gain_pct >= 100:
        candidates.append(peak * 0.80)    # HWM -20%

    # 单调递增: 不允许低于已持久化的止损
    prev = h.get("current_stop_price")
    if prev:
        candidates.append(prev)

    new_stop = max(candidates)

    # 标签 — 告诉用户当前处于哪一档
    if gain_pct < 20:
        label = f"初始 -8% 硬止损 (浮盈 {gain_pct:+.1f}%, 等 +20% 升保本)"
    elif gain_pct < 50:
        label = f"保本档 (浮盈 {gain_pct:+.1f}%, 等 +50% 切峰值追踪)"
    elif gain_pct < 100:
        label = f"峰值 -15% 追踪 (浮盈 {gain_pct:+.1f}%, 等 +100% 收紧到 -20%)"
    else:
        label = f"峰值 -20% 追踪 (浮盈 {gain_pct:+.1f}%, 让赢家奔跑)"

    return round(new_stop, 2), label, round(gain_pct, 2)


def _signals_lookup(signals):
    """Build {ticker: row_with_conv_and_mom} for fast lookup."""
    out = {}
    for r in (signals.get("rows") or []):
        out[r["ticker"]] = r
    return out


def update_decay_peaks(h, sig_row):
    """v3.6: 追踪持仓期间的 Conv / 6M-Mom 峰值 (单调递增) 和入场时基准。

    用于 conv_decay / growth_decay 判断:
      - peak_conv_while_held: 自买入以来 conv 的最高点
      - peak_6m_mom_while_held: 自买入以来 6M-Mom 的最高点
      - entry_6m_mom: 入场时的 6M-Mom (用于"跌超 50% from 入场"判断)
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
        # 第一次有 mom 数据时记下入场基准
        if "entry_6m_mom" not in h:
            h["entry_6m_mom"] = cur_mom


def main():
    if not HOLDINGS.exists():
        print("no holdings file")
        return
    hh = json.loads(HOLDINGS.read_text(encoding="utf-8"))
    prices = json.loads(PRICES.read_text(encoding="utf-8")) if PRICES.exists() else {}
    signals = json.loads(SIGNALS.read_text(encoding="utf-8")) if SIGNALS.exists() else {}
    hwm_map = hwm_per_ticker(hh)
    sig_lookup = _signals_lookup(signals)

    updates = []
    table = []
    for t, h in hh.items():
        # v3.6: 更新 conv/mom 峰值 (无论 strategy, 给 anti-mart 用)
        update_decay_peaks(h, sig_lookup.get(t))

        px, _sma20 = latest_price(t, prices, signals)
        if not px:
            continue
        hwm = hwm_map.get(t, px)
        # HWM 也要单调递增
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
        })
        if old_stop != new_stop:
            updates.append(f"  {t}: {old_stop} → {new_stop} ({label})")

    HOLDINGS.write_text(json.dumps(hh, ensure_ascii=False, indent=2), encoding="utf-8")
    if SEED_HOLDINGS.exists():
        SEED_HOLDINGS.write_text(json.dumps(hh, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"=== 反马丁动态止损 ({len(table)} 只 anti-martingale 持仓) ===\n")
    print(f"{'Ticker':<8} {'锚点':>8} {'现价':>8} {'峰值':>8} {'浮盈':>7} {'旧止损':>9} {'新止损':>9}  {'档位'}")
    print("-" * 110)
    for r in table:
        print(f"{r['ticker']:<8} ${r['anchor']:>7.2f} ${r['current_px']:>7.2f} "
              f"${r['hwm']:>7.2f} {r['ret_pct']:>+6.1f}% "
              f"{('$'+str(r['old_stop'])) if r['old_stop'] else '   -   ':>9} "
              f"${r['new_stop']:>8.2f}  {r['label']}")

    if updates:
        print(f"\n变更 ({len(updates)}):")
        for u in updates:
            print(u)

    # Fidelity 行动表 — 反马丁纪律: 只挂 Stop Loss, 不挂 Take Profit
    print(f"\n=== Fidelity 挂单指引 (反马丁: 只挂 STOP, 不设 TP) ===\n")
    for r in table:
        h = hh[r['ticker']]
        hc = "🔥" if h.get("high_conviction") else "  "
        print(f"  {hc} {r['ticker']:<6}  STOP ${r['new_stop']:.2f}   ← {r['label']}")
    print(f"\n  止盈策略: 只在止损被触发时全清, 不设固定 TP — 让赢家奔跑。")


if __name__ == "__main__":
    main()
