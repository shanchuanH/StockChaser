"""Backtest entry point — IS/OOS split with realistic slippage + costs.

Run:
    python3 scripts/backtest.py
    python3 scripts/backtest.py --oos 0.30 --txn-bps 5 --slip-bps 10
    python3 scripts/backtest.py --no-cost      # 理论上限对比 (无滑点无费用)

Requires:
    data/history.parquet (or .csv) covering 5+ years (推荐 7 年)
    data/universe.json
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.backtest import run_backtest, CostModel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oos", type=float, default=0.30,
                    help="样本外比例 (默认 0.30 = 后 30%% 用作 OOS)")
    ap.add_argument("--txn-bps", type=float, default=5,
                    help="单边手续费 bps (默认 5)")
    ap.add_argument("--slip-bps", type=float, default=10,
                    help="基础进出场滑点 bps (默认 10)")
    ap.add_argument("--stop-slip-bps", type=float, default=30,
                    help="stop-loss 触发额外滑点 bps (默认 30)")
    ap.add_argument("--small-cap-bps", type=float, default=5,
                    help="小票额外 spread bps (默认 5)")
    ap.add_argument("--no-cost", action="store_true",
                    help="无成本无滑点版 (理论上限对比)")
    args = ap.parse_args()

    if args.no_cost:
        cost = CostModel(txn_cost_bps=0, base_slippage_bps=0,
                        stop_slippage_bps=0, small_cap_extra_bps=0)
        print("⚠️ NO-COST 模式 — 仅作理论上限参考")
    else:
        cost = CostModel(
            txn_cost_bps=args.txn_bps,
            base_slippage_bps=args.slip_bps,
            stop_slippage_bps=args.stop_slip_bps,
            small_cap_extra_bps=args.small_cap_bps,
        )

    data_dir = ROOT / "data"
    history = data_dir / "history.parquet"
    if not history.exists():
        history = data_dir / "history.csv"
    if not history.exists():
        print("ERROR: data/history.{parquet,csv} 不存在.")
        print("先跑: python3 scripts/fetch_data.py --years 7")
        sys.exit(1)

    run_backtest(
        universe_path=data_dir / "universe.json",
        history_path=history,
        out_path=data_dir / "backtest_results.json",
        oos_pct=args.oos,
        cost_model=cost,
        verbose=True,
    )


if __name__ == "__main__":
    main()
