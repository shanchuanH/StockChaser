"""Mark specific tickers as 'high_conviction' → disable profit ladder.

High-conviction means: you believe the thesis is multi-bagger.
Standard profit-ladder (+10/+20/+30 sell) would force you out of the best move.
Instead, ride trend till -8% hard stop OR Conv < 45.

Mirrors V1 backtest behavior (which didn't take profits): CAGR +30.4% over 4y.

Edit the HIGH_CONVICTION set below as your conviction list evolves.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "data" / "my_holdings.json"
SEED = ROOT / "data_seed" / "my_holdings.json"

# Tickers you're MOST bullish on — disable profit ladder, ride to trend break.
# Optical modules (L6) historically have 5-20x moves — top candidates.
HIGH_CONVICTION = {
    "AAOI",      # 光模块龙头 (you mentioned 特别看好)
    # Add more here as conviction develops:
    # "ALAB", "CRDO", "MTSI", "COHR", "POET", ...
}


def main():
    pf = json.loads(P.read_text(encoding="utf-8"))
    changes = []
    for ticker, h in pf.items():
        was = h.get("high_conviction", False)
        now = ticker in HIGH_CONVICTION
        if was != now:
            h["high_conviction"] = now
            if now:
                # Clear profit_ladder so verb logic skips +10/+20/+30 sell suggestions
                h["profit_ladder"] = None
                changes.append(f"  ✓ {ticker}: high_conviction=True, profit_ladder cleared (ride to trend-break)")
            else:
                # Restore standard ladder if previously high-conviction
                h["profit_ladder"] = [
                    {"trigger_pct": 0.10, "sell_pct_of_peak": 0.25, "name": "tier_1"},
                    {"trigger_pct": 0.20, "sell_pct_of_peak": 0.25, "name": "tier_2"},
                    {"trigger_pct": 0.30, "sell_pct_of_peak": 0.33, "name": "tier_3"},
                ]
                changes.append(f"  - {ticker}: high_conviction=False, profit_ladder restored")

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

    print("\nRules summary:")
    print("  反马丁 + high_conviction=True:")
    print("    sell triggers:  -8% stop only (no profit-take)")
    print("    verb behavior:  HOLD all the way until trend breaks")
    print("    踏空 risk:      0% (capture full upside)")
    print("  反马丁 + high_conviction=False:")
    print("    sell triggers:  -8% stop + +10/+20/+30 ladder")
    print("    verb behavior:  尝试 lock 33% at each tier")
    print("    踏空 risk:      ~26% of upside above +30%")
    print("  Martingale:")
    print("    sell triggers:  none (ride forever)")
    print("    buy triggers:   -5/-10/-15 dip ladder")


if __name__ == "__main__":
    main()
