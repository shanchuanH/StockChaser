"""Mark tickers as 'high_conviction' — replace entry_ladder with dip_ladder,
clear profit_ladder, KEEP -8% hard stop.

high_conviction semantics:
  - 不止盈 (profit_ladder = None)
  - 越跌越买 (dip_ladder, -5/-10/-15 from buy_price)
  - 不追涨 (entry_ladder = None — don't add at +3%/+6%)
  - 防御底 (stop_loss_pct = -0.08 retained)

This script ONLY touches:
  - tickers in HIGH_CONVICTION set (to apply high_conviction config)
  - anti_martingale tickers NOT in the set (to restore standard ladder)
  - Martingale tickers are LEFT ALONE.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "data" / "my_holdings.json"
SEED = ROOT / "data_seed" / "my_holdings.json"

HIGH_CONVICTION = {
    "AAOI",   # 光模块龙头, 你说特别看好
    # Add more here as conviction develops:
    # "VRT", "HIMX", ...
}


def main():
    pf = json.loads(P.read_text(encoding="utf-8"))
    changes = []
    for ticker, h in pf.items():
        was = h.get("high_conviction", False)
        now = ticker in HIGH_CONVICTION
        h["high_conviction"] = now
        if now:
            # high_conviction: no profit-take, no entry_ladder, dip-buy on weakness, keep stop
            h["profit_ladder"] = None
            h["entry_ladder"] = None
            h["dip_ladder"] = [
                {"trigger_pct": -0.05, "buy_pct_of_initial": 0.30, "name": "dip_1"},
                {"trigger_pct": -0.10, "buy_pct_of_initial": 0.50, "name": "dip_2"},
                {"trigger_pct": -0.15, "buy_pct_of_initial": 1.00, "name": "dip_3"},
            ]
            if was != now:
                changes.append(f"  + {ticker}: high_conviction ON")
        else:
            if h.get("strategy") == "anti_martingale":
                # Standard anti-martingale ladder
                h["profit_ladder"] = [
                    {"trigger_pct": 0.10, "sell_pct_of_peak": 0.25, "name": "tier_1"},
                    {"trigger_pct": 0.20, "sell_pct_of_peak": 0.25, "name": "tier_2"},
                    {"trigger_pct": 0.30, "sell_pct_of_peak": 0.33, "name": "tier_3"},
                ]
                h["entry_ladder"] = [
                    {"trigger": "initial", "name": "step_1"},
                    {"trigger_pct": 0.03, "name": "step_2"},
                    {"trigger_pct": 0.06, "name": "step_3"},
                ]
                h["dip_ladder"] = None
                if was != now:
                    changes.append(f"  - {ticker}: high_conviction OFF, anti-martingale ladder restored")
            # else: martingale tickers (ETF/Mag7) — leave their config alone

    P.write_text(json.dumps(pf, ensure_ascii=False, indent=2), encoding="utf-8")
    SEED.write_text(json.dumps(pf, ensure_ascii=False, indent=2), encoding="utf-8")

    if changes:
        print("Changes:")
        for c in changes: print(c)
    else:
        print("No changes (all already correctly tagged)")

    print(f"\nHigh-conviction roster ({len(HIGH_CONVICTION)}):")
    for t in sorted(HIGH_CONVICTION):
        h = pf.get(t)
        if h:
            print(f"  {t}: held {h['shares']} 股 @ ${h['buy_price']}, stop ${h['buy_price']*0.92:.2f}")
        else:
            print(f"  {t}: (not in holdings)")


if __name__ == "__main__":
    main()
