"""一次性同步 Fidelity 截图里的持仓变化到 my_holdings.json。

变化:
  - AAOI: 30 → 50 股 (加仓 20, 新 avg $178.98)
  - DXYZ: 新增 70 股 @ $53.03 (Destiny Tech 100, external 类别)

跑法: python3 scripts/sync_fidelity.py
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sync_one(path: Path):
    h = json.loads(path.read_text(encoding="utf-8"))

    # ---- AAOI: 加仓 20 股, 新 avg $178.98 ----
    if "AAOI" in h:
        a = h["AAOI"]
        old_shares = a.get("shares", 0)
        old_avg = a.get("buy_price", 0)
        new_total = 50
        new_avg = 178.98
        if old_shares == new_total and abs(old_avg - new_avg) < 0.01:
            print(f"  {path.name}: AAOI 已经是 50@${new_avg}, 跳过")
        else:
            added_shares = new_total - old_shares
            if added_shares > 0:
                added_avg = (new_avg * new_total - old_avg * old_shares) / added_shares
            else:
                added_avg = new_avg
            a["shares"] = new_total
            a["buy_price"] = new_avg
            a.setdefault("batches", []).append({
                "price": round(added_avg, 2),
                "shares": added_shares,
                "date": "2026-05-26",
                "step": "manual",
            })
            a.setdefault("actions", []).append({
                "t": "add",
                "step": "manual",
                "shares": added_shares,
                "price": round(added_avg, 2),
                "date": "2026-05-26",
                "after_shares": new_total,
            })
            a["peak_shares"] = max(a.get("peak_shares") or 0, new_total)
            print(f"  {path.name}: AAOI {old_shares}@${old_avg} → "
                  f"{new_total}@${new_avg} (加 {added_shares} 股 @ ${round(added_avg,2)})")

    # ---- DXYZ: 新增 70 股 @ $53.03 ----
    if "DXYZ" not in h:
        h["DXYZ"] = {
            "ticker": "DXYZ",
            "name": "Destiny Tech100 (私有科技 CEF)",
            "shares": 70,
            "buy_price": 53.03,
            "buy_date": "2026-05-26",
            "batches": [{
                "price": 53.03, "shares": 70,
                "date": "2026-05-26", "step": "manual",
            }],
            "peak_shares": 70,
            "category": "external",  # 不参与 AI 链策略, 只追踪 P&L
            "strategy": "buy_hold",
            "actions": [{
                "t": "buy", "step": "manual",
                "shares": 70, "price": 53.03,
                "date": "2026-05-26", "after_shares": 70,
            }],
        }
        print(f"  {path.name}: DXYZ 新增 70 股 @ $53.03 (external 类别)")
    else:
        print(f"  {path.name}: DXYZ 已存在, 跳过")

    path.write_text(
        json.dumps(h, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  {path.name}: 共 {len(h)} 个持仓")


def main():
    for relpath in ("data/my_holdings.json", "data_seed/my_holdings.json"):
        p = ROOT / relpath
        if not p.exists():
            print(f"跳过 {relpath} (不存在)")
            continue
        print(f"\n== 同步 {relpath} ==")
        sync_one(p)


if __name__ == "__main__":
    main()
