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
            # high_conviction = standard anti-martingale MINUS the profit ladder.
            # Anti-martingale invariant: ONE hard stop, NEVER buy dips.
            #   - stop_loss_pct stays -0.08 (the single stop)
            #   - entry_ladder kept (+3%/+6% pyramid-in on strength — consistent)
            #   - dip_ladder stays None (buying dips = martingale, contradicts anti-mart)
            #   - profit_ladder removed → ride the winner, no forced selling
            h["profit_ladder"] = None
            h["entry_ladder"] = [
                {"trigger": "initial", "name": "step_1"},
                {"trigger_pct": 0.03, "name": "step_2"},
                {"trigger_pct": 0.06, "name": "step_3"},
            ]
            h["dip_ladder"] = None
            if was != now:
                changes.append(f"  + {ticker}: high_conviction ON (anti-mart − profit_ladder, 单一 -8% 止损)")
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

    p
