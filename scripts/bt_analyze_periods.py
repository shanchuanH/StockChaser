"""Break down bt_exit_variants.json into per-year + per-regime segments.

Answers the question: did V2's aggressive triggers help during the
2022 bear market specifically?

Reads:
  data/bt_exit_variants.json   (output of bt_exit_variants.py)
Prints:
  per-year returns + bear/bull regime split + final verdict.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    d = json.loads((ROOT / "data" / "bt_exit_variants.json").read_text(encoding="utf-8"))
    curves = d["equity_curves"]
    spy_eq = d["spy_equity"]
    n = len(curves["V0"])
    years = d["years"]
    weeks_per_yr = n / years
    bt_start = datetime.strptime(d["start"], "%Y-%m-%d")

    # 1. Annual breakdown
    print(f"Backtest: {d['start']} → {d['end']}  ({n} weeks, {years:.1f} years)")
    print()
    print(f"{'Period':<35} {'V0':>9} {'V1':>9} {'V2':>9} {'V3':>9} {'SPY':>9}")
    print("-" * 92)
    annual_starts = [int(weeks_per_yr * k) for k in range(int(years) + 1)]
    annual_starts[-1] = min(annual_starts[-1], n - 1)
    for k in range(len(annual_starts) - 1):
        i, j = annual_starts[k], annual_starts[k + 1]
        if i >= j: continue
        d1 = bt_start + timedelta(weeks=i)
        d2 = bt_start + timedelta(weeks=j)
        label = f"{d1.date()} → {d2.date()}"
        ret = lambda arr: (arr[j] / arr[i] - 1) * 100
        print(f"{label:<35} "
              f"{ret(curves['V0']):>+8.1f}% "
              f"{ret(curves['V1']):>+8.1f}% "
              f"{ret(curves['V2']):>+8.1f}% "
              f"{ret(curves['V3']):>+8.1f}% "
              f"{ret(spy_eq):>+8.1f}%")

    # 2. Bear vs Bull regime split using SPY
    print()
    print("Bear vs Bull regime (SPY drawdown from peak ≥ 10%):")
    print("-" * 92)
    # Identify weeks where SPY is in drawdown ≥ 10%
    spy_peak = spy_eq[0]
    bear_weeks = []
    bull_weeks = []
    for i, e in enumerate(spy_eq):
        if e > spy_peak: spy_peak = e
        dd = e / spy_peak - 1
        if dd <= -0.10:
            bear_weeks.append(i)
        else:
            bull_weeks.append(i)

    def segment_return(arr, weeks):
        if not weeks: return 0
        # Sum log returns across the indices' transitions
        total = 1.0
        prev = weeks[0]
        for w in weeks[1:]:
            if w == prev + 1:
                total *= arr[w] / arr[prev]
            prev = w
        return (total - 1) * 100

    print(f"  Bear weeks: {len(bear_weeks):>3d} ({len(bear_weeks)/n*100:.0f}%)")
    print(f"    V0 in bear:  {segment_return(curves['V0'], bear_weeks):>+7.1f}%")
    print(f"    V1 in bear:  {segment_return(curves['V1'], bear_weeks):>+7.1f}%")
    print(f"    V2 in bear:  {segment_return(curves['V2'], bear_weeks):>+7.1f}%  ← 关键: V2 是否救命？")
    print(f"    V3 in bear:  {segment_return(curves['V3'], bear_weeks):>+7.1f}%")
    print(f"    SPY in bear: {segment_return(spy_eq, bear_weeks):>+7.1f}%")
    print()
    print(f"  Bull weeks: {len(bull_weeks):>3d} ({len(bull_weeks)/n*100:.0f}%)")
    print(f"    V0 in bull:  {segment_return(curves['V0'], bull_weeks):>+7.1f}%")
    print(f"    V1 in bull:  {segment_return(curves['V1'], bull_weeks):>+7.1f}%")
    print(f"    V2 in bull:  {segment_return(curves['V2'], bull_weeks):>+7.1f}%  ← V2 牛市损失多少？")
    print(f"    V3 in bull:  {segment_return(curves['V3'], bull_weeks):>+7.1f}%")
    print(f"    SPY in bull: {segment_return(spy_eq, bull_weeks):>+7.1f}%")

    # 3. Worst drawdown for each variant (when + how long)
    print()
    print("每个变体的最大回撤区间:")
    print("-" * 92)
    for v in ["V0", "V1", "V2", "V3"]:
        arr = curves[v]
        peak = arr[0]; peak_i = 0
        max_dd = 0; max_dd_i = 0; max_dd_peak_i = 0
        for i, e in enumerate(arr):
            if e > peak:
                peak = e; peak_i = i
            dd = e / peak - 1
            if dd < max_dd:
                max_dd = dd; max_dd_i = i; max_dd_peak_i = peak_i
        peak_d = bt_start + timedelta(weeks=max_dd_peak_i)
        trough_d = bt_start + timedelta(weeks=max_dd_i)
        weeks_dur = max_dd_i - max_dd_peak_i
        print(f"  {v}: 峰值 {peak_d.date()} → 谷底 {trough_d.date()}  "
              f"({weeks_dur} 周, MaxDD {max_dd*100:+.1f}%)")

    # 4. Verdict logic
    print()
    print("=" * 92)
    print("结论:")
    bear_v1 = segment_return(curves["V1"], bear_weeks)
    bear_v2 = segment_return(curves["V2"], bear_weeks)
    bull_v1 = segment_return(curves["V1"], bull_weeks)
    bull_v2 = segment_return(curves["V2"], bull_weeks)
    if bear_weeks and bear_v2 > bear_v1 + 5:
        print(f"  ✅ V2 在熊市段比 V1 多保住 {bear_v2 - bear_v1:.1f}%, 牛市损失 {bull_v1 - bull_v2:.1f}%")
        print(f"     → 推荐 regime-aware: SPY < SMA200 时启用 V2 规则，反之用 V1")
    else:
        print(f"  ❌ V2 在熊市段并未显著保护 (V2 {bear_v2:+.1f}% vs V1 {bear_v1:+.1f}%)")
        print(f"     V2 牛市段损失 {bull_v1 - bull_v2:.1f}% 无法被熊市段优势对冲")
        print(f"     → 建议回退到 V1, 删掉 flash_5 / hwm_trail / time_stop")


if __name__ == "__main__":
    main()
