"""Tag each holding with strategy = 'anti_martingale' or 'martingale'.

anti_martingale (AI 单股): hard -8% stop, +10/+20/+30 profit ladder, no DCA.
martingale (ETF + Mag7):   no hard stop, -5/-10/-15 dip-buy ladder, ride forever.

Run once in WSL: python3 scripts/set_strategy_labels.py
Idempotent — re-running is safe.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "data" / "my_holdings.json"
SEED = ROOT / "data_seed" / "my_holdings.json"

# Strategy classification — edit here if your roster changes.
ANTI_MARTINGALE = {"AAOI", "ARM", "HIMX", "LSCC", "QCOM", "VRT"}
MARTINGALE = {
    # ETFs (always buy & hold, never sell)
    "DRAM", "QQQM", "SCHD", "VGT", "VOO",
    # Magnificent 7 (blue-chip, treat like core ETF)
    "AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA",
}


def assign(h, strategy):
    """Tag holding with strategy + appropriate ladder config."""
    h["strategy"] = strategy
    if strategy == "anti_martingale":
        h["stop_loss_pct"] = -0.08         # hard stop -8% from buy_price
        h["profit_ladder"] = [             # +10/+20/+30, 25%/25%/33%
            {"trigger_pct": 0.10, "sell_pct_of_peak": 0.25, "name": "tier_1"},
            {"trigger_pct": 0.20, "sell_pct_of_peak": 0.25, "name": "tier_2"},
            {"trigger_pct": 0.30, "sell_pct_of_peak": 0.33, "name": "tier_3"},
        ]
        h["entry_ladder"] = [              # standard 3-batch
            {"trigger": "initial",    "name": "step_1"},
            {"trigger_pct": 0.03,     "name": "step_2"},
            {"trigger_pct": 0.06,     "name": "step_3"},
        ]
        h["dip_ladder"] = None
    elif strategy == "martingale":
        h["stop_loss_pct"] = None           # NO hard stop
        h["profit_ladder"] = None           # NO take-profit (ride forever)
        h["entry_ladder"] = None            # NO ladder entry
        h["dip_ladder"] = [                 # buy more on -5/-10/-15 from buy_price
            {"trigger_pct": -0.05, "buy_pct_of_initial": 0.30, "name": "dip_1"},
            {"trigger_pct": -0.10, "buy_pct_of_initial": 0.50, "name": "dip_2"},
            {"trigger_pct": -0.15, "buy_pct_of_initial": 1.00, "name": "dip_3"},
        ]


def main():
    pf = json.loads(P.read_text(encoding="utf-8"))
    n_anti, n_mart, n_skip = 0, 0, 0
    for ticker, h in pf.items():
        if ticker in ANTI_MARTINGALE:
            assign(h, "anti_martingale"); n_anti += 1
        elif ticker in MARTINGALE:
            assign(h, "martingale"); n_mart += 1
        else:
            # Unclassified → default to anti_martingale for safety (hard stop)
            assign(h, "anti_martingale"); n_anti += 1
            print(f"  ! {ticker}: not classified, defaulting to anti_martingale")

    P.write_text(json.dumps(pf, ensure_ascii=False, indent=2), encoding="utf-8")
    SEED.write_text(json.dumps(pf, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n✓ Tagged {n_anti} anti-martingale + {n_mart} martingale")
    print(f"  data/my_holdings.json + data_seed/my_holdings.json synced")

    # Print Fidelity setup table for anti-martingale stocks
    print("\n=== Fidelity OCO setup (anti-martingale only) ===")
    print(f"{'Ticker':<8} {'Buy':>9}  {'Stop -8%':>10}  {'TP +30%':>10}  {'Sell qty 33%':>13}")
    for t in sorted(ANTI_MARTINGALE):
        h = pf.get(t)
        if not h: continue
        buy = h["buy_price"]
        shares = h["shares"]
        stop = buy * 0.92
        tp = buy * 1.30
        sell_qty = int(shares * 0.33)
        print(f"  {t:<6} ${buy:>8.2f}  ${stop:>8.2f}  ${tp:>8.2f}  {sell_qty:>13}")


if __name__ == "__main__":
    main()
