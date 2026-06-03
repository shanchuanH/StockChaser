"""Weekly rebalance backtest with realistic costs + in-sample/out-of-sample split.

v3.7 设计原则:
  - 用历史 5-7 年 daily 数据, 周一开盘换仓
  - 含**滑点**: 进/出场 10 bps + stop-loss 触发额外 20 bps
  - 含**手续费**: 5 bps round-trip per leg (10 bps total)
  - **IS/OOS 切分**: 前 70% 训练 (调参), 后 30% 验证 (独立测试)
  - 6 个策略并行: B&H SPY / 6M-Mom Top3 / Comp Top10 / 3-Gate+ATR /
                  Conviction / ★ Hybrid+Stop+MinHold

数据要求:
  data/history.parquet (或 .csv): 完整 OHLCV, 5+ 年, 覆盖整个 universe + SPY

输出:
  data/backtest_results.json — equity curves + IS/OOS metrics
"""
from __future__ import annotations
import json
import math
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone

from . import scoring, regime, framework, action as action_mod


# ── Cost / Slippage models ──────────────────────────────────────────────

class CostModel:
    """Round-trip transaction cost + slippage."""

    def __init__(self,
                 txn_cost_bps=5,          # 单边手续费 5 bps (买 + 卖各 5 bps)
                 base_slippage_bps=10,     # 普通进出场滑点 10 bps
                 stop_slippage_bps=30,     # stop loss 触发额外滑点 (跳过)
                 small_cap_extra_bps=5):   # 小票额外 spread
        self.txn = txn_cost_bps / 10000
        self.slip = base_slippage_bps / 10000
        self.stop_slip = stop_slippage_bps / 10000
        self.small_cap = small_cap_extra_bps / 10000

    def round_trip(self, is_stopped_out=False, is_small_cap=False):
        """Total cost for one entry+exit cycle as fraction (decimal)."""
        # entry: txn + slip + (spread if small cap)
        # exit:  txn + slip + (extra if stopped)
        cost = 2 * self.txn + 2 * self.slip
        if is_stopped_out:
            cost += self.stop_slip
        if is_small_cap:
            cost += 2 * self.small_cap
        return cost

    def __repr__(self):
        return (f"CostModel(txn={self.txn*10000:.0f}bps, slip={self.slip*10000:.0f}bps, "
                f"stop_slip={self.stop_slip*10000:.0f}bps, small_cap={self.small_cap*10000:.0f}bps)")


# ── Helpers ─────────────────────────────────────────────────────────────

def avg_monthly_6m(close, di, step=21, n=6):
    pts = []
    for k in range(n + 1):
        idx = di - k * step
        if idx < 0:
            return None
        pts.append(close[idx])
    pts = list(reversed(pts))
    rets = [(pts[i]/pts[i-1] - 1) for i in range(1, len(pts))]
    return sum(rets) / len(rets)


def weekly_dates(pv_index):
    """Pick Monday (or first trading day of each ISO week)."""
    weeks = {}
    for d in pv_index:
        key = (d.isocalendar().year, d.isocalendar().week)
        if key not in weeks:
            weeks[key] = d
    return [weeks[k] for k in sorted(weeks)]


def equity_metrics(equity, label=""):
    """Total return / CAGR / Sharpe / Max DD / Win rate from weekly equity."""
    if not equity or len(equity) < 2:
        return {"total_return": 0, "cagr": 0, "sharpe": 0, "max_dd": 0, "win_rate": 0, "n_weeks": 0}
    total = equity[-1] / equity[0] - 1
    n_weeks = len(equity)
    n_years = n_weeks / 52
    cagr = (equity[-1] / equity[0]) ** (1 / max(n_years, 0.01)) - 1
    rets = [equity[i] / equity[i-1] - 1 for i in range(1, len(equity))]
    avg = sum(rets) / len(rets) if rets else 0
    var = sum((r - avg) ** 2 for r in rets) / max(len(rets) - 1, 1)
    std = math.sqrt(var)
    sharpe = (avg / std) * math.sqrt(52) if std > 0 else 0
    peak = equity[0]; max_dd = 0
    for e in equity:
        if e > peak: peak = e
        dd = e / peak - 1
        if dd < max_dd: max_dd = dd
    win = sum(1 for r in rets if r > 0) / len(rets) if rets else 0
    return {
        "label": label,
        "total_return": round(total, 4),
        "cagr": round(cagr, 4),
        "sharpe": round(sharpe, 3),
        "max_dd": round(max_dd, 4),
        "win_rate": round(win, 3),
        "n_weeks": n_weeks,
    }


def split_is_oos(weeks, oos_pct=0.30):
    """Split weeks into in-sample / out-of-sample by week count."""
    n = len(weeks)
    cut = int(n * (1 - oos_pct))
    return list(range(cut)), list(range(cut, n))


# ── Conviction calc (mirrors engine logic at backtest time) ─────────────

def compute_backtest_conviction(f, u, rm_val):
    """Replicate engine_v37 conviction logic for one ticker at one week_idx."""
    # Build a mock p dict for engine.scoring
    p = {
        "latest_price": f["close"],
        "week_return_pct": f["wk"], "month_return_pct": f["mo"],
        "m3_return_pct": f["m3"], "rs_vs_spy_4w_pp": f["rs"],
        "avg_monthly_6m_pct": (f["avg6m"] * 100) if f["avg6m"] is not None else None,
        "above_sma_20": f["close"] > f["sma20"],
        "above_sma_50": f["close"] > f["sma50"],
        "dist_4w_high_pct": f["dist_4w"],
        "dist_52w_high_pct": f["dist_52w"],
        "atr_pct": None,  # backtest doesn't track ATR
        "vol_ratio_5_20": None,
        "sma_20": f["sma20"],
        "high_4w": f["high_4w"],
        "daily_return_pct": f.get("daily"),
    }
    ts = scoring.trend_strength(p)
    eq = scoring.entry_quality(p)
    ds_bonus = framework.downstream_bonus(u)
    nn_bonus = framework.new_node_bonus(u)
    fc = scoring.fundamental_conviction(u, f["comp"], ds_bonus, nn_bonus)
    cv = scoring.conviction_score(ts, eq, fc, rm_val)
    lk = framework.layer_key(u)
    return cv, lk, ts, eq, fc


def backtest_regime(spy_close, di):
    """Backtest-time regime modifier (mirrors engine.regime)."""
    if di < 50:
        return 0.7
    spy_above_50 = spy_close[di] > sum(spy_close[di-49:di+1]) / 50
    if di >= 200:
        sma200 = sum(spy_close[di-199:di+1]) / 200
        spy_above_200 = spy_close[di] > sma200
        dist_200 = (spy_close[di] / sma200 - 1) * 100
    else:
        spy_above_200 = True
        dist_200 = 5.0
    spy_4w = (spy_close[di] / spy_close[di-21] - 1) * 100 if di >= 21 else 0

    if not spy_above_200:
        if dist_200 < -15 or spy_4w < -12: return 0.40
        if spy_4w < -5:                    return 0.50
        return 0.60
    if spy_above_50 and spy_4w >= 4: return 1.10
    if spy_above_50 and spy_4w >= 1: return 1.00
    if spy_above_50: return 0.90
    if -3 <= spy_4w < 0: return 0.80
    if spy_4w < -5: return 0.65
    return 0.75


# ── Main backtest ───────────────────────────────────────────────────────

TOP_N_S2 = 3
TOP_N_S3 = 10
TOP_N_S4 = 5
TOP_N_S5 = 8
TOP_N_S6 = 8
MIN_BARS = 252
MIN_HOLD_WEEKS_S6 = 4
EXIT_CONV_S6 = 45
STOP_PCT_S6 = 0.08
REGIME_POSITION_FLOOR = 0.30   # v3.7: 熊市仓位地板, 不低于 30%


def run_backtest(universe_path, history_path, out_path,
                  oos_pct=0.30, cost_model=None, verbose=True):
    """Run all 6 strategies, split IS/OOS, write results."""
    import pandas as pd

    cost_model = cost_model or CostModel()
    universe = json.loads(Path(universe_path).read_text(encoding="utf-8"))
    by_ticker = {u["ticker"]: u for u in universe}

    # Load history
    p = Path(history_path)
    if p.suffix == ".parquet":
        try:
            df = pd.read_parquet(p)
        except Exception:
            df = pd.read_csv(p.with_suffix(".csv"))
    else:
        df = pd.read_csv(p)
    df["date"] = pd.to_datetime(df["date"])
    pv_close = df.pivot(index="date", columns="ticker", values="close").sort_index().ffill()
    dates = list(pv_close.index)
    date_to_idx = {d: i for i, d in enumerate(dates)}
    weeks = weekly_dates(dates)
    start_idx = next((i for i, d in enumerate(weeks) if date_to_idx.get(d, -1) >= MIN_BARS), None)
    if start_idx is None:
        raise RuntimeError("Not enough history (need 252+ days warmup)")
    bt_weeks = weeks[start_idx:]
    if verbose:
        print(f"Backtest window: {bt_weeks[0].date()} → {bt_weeks[-1].date()} ({len(bt_weeks)} weeks)")
        print(f"Cost model: {cost_model}")

    tickers = [t for t in pv_close.columns if t in by_ticker and t != "SPY"]
    close_arr = {t: pv_close[t].ffill().tolist() for t in tickers}
    if "SPY" not in pv_close.columns:
        raise RuntimeError("SPY not in history — required for regime + B&H baseline")
    spy_close = pv_close["SPY"].ffill().tolist()

    # Static fundamental score (for composite calc)
    fund_score = {}
    for u in universe:
        s = (u.get("potential", 0) or 0) * 18 + {"龙头": 8, "二线": 4, "概念": 0}.get(u.get("role"), 0)
        fund_score[u["ticker"]] = min(100, s)

    # Equity curves per strategy
    eq = {k: [1.0] for k in ["S1", "S2", "S3", "S4", "S5", "S6"]}
    holdings = {k: [] for k in ["S2", "S3", "S4", "S5", "S6"]}

    for w_idx, wk in enumerate(bt_weeks):
        di = date_to_idx.get(wk)
        if di is None or di < MIN_BARS:
            continue

        # Compute weekly features for all tickers
        feats = {}
        for t in tickers:
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

            wk_r = ret(5); mo_r = ret(21); m3_r = ret(63)
            spy_4w = (spy_close[di] / spy_close[di - 21] - 1) * 100 if di >= 21 else 0
            rs = (mo_r - spy_4w) if (mo_r is not None and spy_4w is not None) else 0
            avg6m = avg_monthly_6m(cl, di)
            high_4w = max(cl[max(0, di - 19):di + 1])
            high_52w = max(cl[max(0, di - 252):di + 1]) if di >= 252 else max(cl[:di+1])
            sma20 = sum(cl[max(0, di - 19):di + 1]) / min(20, di + 1)
            sma50 = sum(cl[max(0, di - 49):di + 1]) / min(50, di + 1)

            mom_s = (50 + (wk_r or 0) * 1.5 + (mo_r or 0) * 0.8 + (m3_r or 0) * 0.4 + rs * 0.6)
            if last > sma20: mom_s += 5
            if last > sma50: mom_s += 5
            d4 = (last / high_4w - 1) * 100
            if d4 < -10: mom_s += d4 * 0.5
            mom_s = max(0, min(100, mom_s))
            comp = mom_s * 0.6 + fund_score.get(t, 0) * 0.4

            feats[t] = {
                "close": last, "wk": wk_r, "mo": mo_r, "m3": m3_r, "rs": rs,
                "avg6m": avg6m, "high_4w": high_4w, "sma20": sma20, "sma50": sma50,
                "comp": comp, "dist_4w": d4,
                "dist_52w": (last / high_52w - 1) * 100,
            }

        # Realize PnL for prior week's baskets (with cost model)
        if w_idx > 0:
            prev_wk = bt_weeks[w_idx - 1]
            prev_di = date_to_idx.get(prev_wk, dates.index(prev_wk))
            ret_spy = spy_close[di] / spy_close[prev_di] - 1
            eq["S1"].append(eq["S1"][-1] * (1 + ret_spy))  # B&H SPY 无成本

            # Equal-weight baskets with round-trip cost on entry+exit
            for s_id in ["S2", "S3", "S4"]:
                basket = holdings[s_id]
                if not basket:
                    eq[s_id].append(eq[s_id][-1])
                    continue
                rets = []
                for h in basket:
                    cl = close_arr[h["ticker"]]
                    if di < len(cl) and not math.isnan(cl[di]):
                        rets.append(cl[di] / h["entry_price"] - 1)
                if not rets:
                    eq[s_id].append(eq[s_id][-1])
                else:
                    avg_ret = sum(rets) / len(rets)
                    eq[s_id].append(eq[s_id][-1] * (1 + avg_ret - cost_model.round_trip()))

            # S5 weighted
            b5 = holdings["S5"]
            if not b5:
                eq["S5"].append(eq["S5"][-1])
            else:
                wret = 0.0; tw = 0.0
                for h in b5:
                    cl = close_arr[h["ticker"]]
                    if di < len(cl) and not math.isnan(cl[di]):
                        wret += (cl[di] / h["entry_price"] - 1) * h["weight"]
                        tw += h["weight"]
                eq["S5"].append(eq["S5"][-1] * (1 + wret - cost_model.round_trip() * tw))

            # S6 with stop-loss + extra slip when stopped
            b6 = holdings["S6"]
            if not b6:
                eq["S6"].append(eq["S6"][-1])
            else:
                wret = 0.0; cost_w = 0.0
                for h in b6:
                    cl = close_arr[h["ticker"]]
                    if di < len(cl) and not math.isnan(cl[di]):
                        base = h.get("last_price", h["entry_price"])
                        stop_price = h["entry_price"] * (1 - STOP_PCT_S6)
                        if cl[di] <= stop_price:
                            wret += (stop_price / base - 1) * h["weight"]
                            h["_stopped"] = True
                            h["last_price"] = stop_price
                            cost_w += h["weight"] * cost_model.round_trip(is_stopped_out=True)
                        else:
                            wret += (cl[di] / base - 1) * h["weight"]
                            h["last_price"] = cl[di]
                            if h.get("entry_week_idx", -1) == w_idx - 1:
                                cost_w += h["weight"] * cost_model.round_trip()
                eq["S6"].append(eq["S6"][-1] * (1 + wret - cost_w))

        # Align lengths
        target_len = w_idx + 1
        for k in eq:
            while len(eq[k]) < target_len:
                eq[k].append(eq[k][-1])

        # Select baskets for this week
        # S2: 6M-Mom Top 3
        pool2 = sorted(
            ((t, f["avg6m"]) for t, f in feats.items() if f["avg6m"] is not None),
            key=lambda x: x[1], reverse=True,
        )
        holdings["S2"] = [{"ticker": t, "entry_price": feats[t]["close"]} for t, _ in pool2[:TOP_N_S2]]

        # S3: Composite Top 10
        pool3 = sorted(((t, f["comp"]) for t, f in feats.items()),
                       key=lambda x: x[1], reverse=True)
        holdings["S3"] = [{"ticker": t, "entry_price": feats[t]["close"]} for t, _ in pool3[:TOP_N_S3]]

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
        seen_layers = set(); s4_top = []
        for t, _ in eligible4:
            lk = (by_ticker[t]["layer"] or "").split("·")[0]
            if lk in seen_layers: continue
            seen_layers.add(lk); s4_top.append(t)
            if len(s4_top) >= TOP_N_S4: break
        holdings["S4"] = [{"ticker": t, "entry_price": feats[t]["close"]} for t in s4_top]

        # S5: Conviction-weighted (engine v3.7 thresholds) + regime position scaling
        rm_val = backtest_regime(spy_close, di)
        # v3.7: 熊市自动缩仓位. rm=1.0 → 满仓权重; rm=0.4 深熊 → 权重 ×0.4
        pos_scale = max(rm_val, REGIME_POSITION_FLOOR)
        s5_cands = []
        for t, f in feats.items():
            cv, lk, _ts, _eq, _fc = compute_backtest_conviction(f, by_ticker[t], rm_val)
            s5_cands.append((t, cv, lk))
        s5_cands.sort(key=lambda x: x[1], reverse=True)
        layer_strong = set(); layer_buy = {}; s5_basket = []
        total_alloc = 0.0; max_alloc = 0.85 * pos_scale  # 仓位上限缩
        for t, cv, lk in s5_cands:
            if len(s5_basket) >= TOP_N_S5 or total_alloc >= max_alloc: break
            if cv >= 82:
                if lk in layer_strong: continue
                layer_strong.add(lk); w = 0.12 * pos_scale
            elif cv >= 70:
                if layer_buy.get(lk, 0) >= 2: continue
                layer_buy[lk] = layer_buy.get(lk, 0) + 1
                w = 0.08 * pos_scale
            elif cv >= 55:
                w = 0.04 * pos_scale
            else:
                continue
            if total_alloc + w > max_alloc:
                w = max_alloc - total_alloc
            s5_basket.append({"ticker": t, "entry_price": feats[t]["close"], "weight": w, "conviction": cv})
            total_alloc += w
        holdings["S5"] = s5_basket

        # S6: 6M-Mom × Conviction hybrid + min-hold + hysteresis
        prev_b6 = [h for h in holdings.get("S6", []) if not h.get("_stopped")]
        kept = []; layer_count = {}
        for h in prev_b6:
            t = h["ticker"]
            if t not in feats:
                continue
            held = w_idx - h.get("entry_week_idx", w_idx)
            if held < MIN_HOLD_WEEKS_S6 or feats[t]["comp"] >= EXIT_CONV_S6:
                lk = (by_ticker[t]["layer"] or "").split("·")[0]
                kept.append({
                    "ticker": t, "entry_price": h["entry_price"],
                    "entry_week_idx": h.get("entry_week_idx", w_idx),
                    "last_price": h.get("last_price", h["entry_price"]),
                    "weight": h.get("weight", 0.04),
                    "mom6m": feats[t].get("avg6m") or 0,
                    "lk": lk,
                })
                layer_count[lk] = layer_count.get(lk, 0) + 1
        kept_t = {h["ticker"] for h in kept}
        new_eligible = []
        for t, cv, lk in s5_cands:
            if t in kept_t: continue
            if cv < 50: continue
            avg6 = feats[t].get("avg6m")
            if avg6 is None or avg6 < 0.01: continue
            new_eligible.append((t, avg6, cv, lk))
        new_eligible.sort(key=lambda x: x[1], reverse=True)
        for t, mom, cv, lk in new_eligible:
            if len(kept) >= TOP_N_S6: break
            if layer_count.get(lk, 0) >= 2: continue
            kept.append({
                "ticker": t, "entry_price": feats[t]["close"],
                "entry_week_idx": w_idx, "weight": 0.04,
                "last_price": feats[t]["close"], "mom6m": mom, "lk": lk,
            })
            layer_count[lk] = layer_count.get(lk, 0) + 1
        kept.sort(key=lambda h: -(h.get("mom6m") or 0))
        # v3.7: 同 S5, S6 tier weights × pos_scale (regime-aware)
        for tier_idx, h in enumerate(kept[:TOP_N_S6]):
            if tier_idx < 3: base_w = 0.12
            elif tier_idx < 6: base_w = 0.08
            else: base_w = 0.04
            h["weight"] = round(base_w * pos_scale, 4)
        holdings["S6"] = kept[:TOP_N_S6]

    # ── Compute IS/OOS metrics ──────────────────────────────────────────
    is_idx, oos_idx = split_is_oos(bt_weeks, oos_pct=oos_pct)
    summary = {}
    for s_id in ["S1", "S2", "S3", "S4", "S5", "S6"]:
        curve = eq[s_id]
        # 取 IS / OOS / Full 三个子区间, equity 用相对值 (re-normalize)
        is_eq = curve[is_idx[0]:is_idx[-1]+1]
        is_eq = [x / is_eq[0] for x in is_eq] if is_eq else []
        oos_eq = curve[oos_idx[0]:oos_idx[-1]+1]
        oos_eq = [x / oos_eq[0] for x in oos_eq] if oos_eq else []
        summary[s_id] = {
            "full": equity_metrics(curve, "full"),
            "is": equity_metrics(is_eq, "in_sample"),
            "oos": equity_metrics(oos_eq, "out_of_sample"),
        }

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {
            "start": bt_weeks[0].date().isoformat(),
            "end": bt_weeks[-1].date().isoformat(),
            "n_weeks": len(bt_weeks),
            "is_weeks": len(is_idx),
            "oos_weeks": len(oos_idx),
            "oos_start": bt_weeks[oos_idx[0]].date().isoformat() if oos_idx else None,
        },
        "cost_model": {
            "txn_bps": cost_model.txn * 10000,
            "slip_bps": cost_model.slip * 10000,
            "stop_slip_bps": cost_model.stop_slip * 10000,
            "small_cap_extra_bps": cost_model.small_cap * 10000,
        },
        "equity_curves": {
            "dates": [d.date().isoformat() for d in bt_weeks],
            **{s_id: eq[s_id] for s_id in ["S1", "S2", "S3", "S4", "S5", "S6"]},
        },
        "summary": summary,
        "strategy_labels": {
            "S1": "B&H SPY (baseline)",
            "S2": "6M-Mom Top 3 (article)",
            "S3": "Composite Top 10 (no gates)",
            "S4": "Three-Gate + ATR + layer cap",
            "S5": "Conviction Engine (v3.7 thresholds)",
            "S6": "★ 6M-Mom × Conviction hybrid + 8% stop + min-hold",
        },
    }
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    if verbose:
        _print_report(summary, out["window"])
    return out


def _print_report(summary, window):
    """Print IS vs OOS comparison table."""
    labels = {
        "S1": "B&H SPY", "S2": "6M-Mom Top3", "S3": "Comp Top10",
        "S4": "3-Gate+ATR", "S5": "Conviction", "S6": "★ Hybrid+Stop",
    }
    print(f"\n窗口: {window['start']} → {window['end']} ({window['n_weeks']} 周)")
    print(f"IS = 前 {window['is_weeks']} 周, OOS = 后 {window['oos_weeks']} 周 (起 {window.get('oos_start')})")
    print()
    print(f"{'Strategy':<16} {'Window':<8} {'Total':>10} {'CAGR':>9} {'Sharpe':>7} {'MaxDD':>9} {'Win%':>6}")
    print("-" * 70)
    for s_id in ["S1", "S2", "S3", "S4", "S5", "S6"]:
        label = labels[s_id]
        for window_key, window_label in [("is", "IS"), ("oos", "OOS"), ("full", "Full")]:
            m = summary[s_id][window_key]
            print(f"{label:<16} {window_label:<8} "
                  f"{m['total_return']*100:>9.1f}% {m['cagr']*100:>8.1f}% "
                  f"{m['sharpe']:>7.2f} {m['max_dd']*100:>8.1f}% {m['win_rate']*100:>5.0f}%")
        print()
