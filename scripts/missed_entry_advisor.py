"""Detect missed entry-ladder steps and suggest action.

For each anti-martingale holding, check:
  - which steps are still pending (in entry_ladder but not in entry_steps_executed)
  - what the current price is vs the trigger price
  - whether we're still "in window" or "expired"

Output a per-ticker advisory: late_ok / late_window_expired / on_time / not_yet.

Writes data/entry_advisory.json — dashboard can read this if it wants.
Also prints actionable steps for the user.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOLDINGS = ROOT / "data" / "my_holdings.json"
PRICES = ROOT / "data" / "prices.json"
SIGNALS = ROOT / "data" / "signals.json"
OUT = ROOT / "data" / "entry_advisory.json"

# Tolerance windows
LATE_OK_BAND = 0.02       # within +2% of trigger → still ok to enter "late"
EXPIRED_BAND = 0.05       # past +5% of trigger → expired, suggest re-entry/skip


def get_price(t):
    """Pull current price from prices.json or signals.json."""
    try:
        sig = json.loads(SIGNALS.read_text(encoding="utf-8"))
        for r in sig.get("rows", []):
            if r["ticker"] == t:
                return r.get("latest_price")
        ex = sig.get("extra_prices", {}).get(t)
        if ex: return ex.get("latest_price")
    except Exception:
        pass
    try:
        pr = json.loads(PRICES.read_text(encoding="utf-8"))
        p = pr.get("tickers", {}).get(t) or pr.get("extra_prices", {}).get(t)
        if p: return p.get("latest_price")
    except Exception:
        pass
    return None


def advise_one(ticker, h):
    """Return advisory dict or None."""
    if h.get("strategy") != "anti_martingale":
        return None
    px = get_price(ticker)
    if not px: return None
    step1 = h.get("step_1_price") or h.get("batches", [{}])[0].get("price")
    if not step1: return None

    executed = h.get("entry_steps_executed") or []
    executed_names = {e.get("step") if isinstance(e, dict) else e for e in executed}
    # Implicit step_1 from first batch
    if h.get("batches") and "step_1" not in executed_names:
        executed_names.add("step_1")

    ladder = h.get("entry_ladder") or [
        {"name": "step_1"},
        {"name": "step_2", "trigger_pct": 0.03},
        {"name": "step_3", "trigger_pct": 0.06},
    ]
    suggestions = []
    for stp in ladder:
        name = stp.get("name")
        if name == "step_1" or name in executed_names: continue
        trigger_pct = stp.get("trigger_pct", 0)
        trigger_price = step1 * (1 + trigger_pct)
        current_ret = (px / trigger_price - 1)
        if px < trigger_price:
            # Not yet triggered
            dist_pct = (trigger_price / px - 1) * 100
            suggestions.append({
                "step": name,
                "status": "not_yet",
                "trigger_price": round(trigger_price, 2),
                "current_price": px,
                "msg": f"等触发 ${trigger_price:.2f} (距+{dist_pct:.1f}%)",
            })
        elif current_ret <= LATE_OK_BAND:
            suggestions.append({
                "step": name,
                "status": "late_ok",
                "trigger_price": round(trigger_price, 2),
                "current_price": px,
                "msg": f"⏰ {name} 迟到, 现价 ${px:.2f} > 触发 ${trigger_price:.2f} (滑点 {current_ret*100:.1f}%), 还可入场",
            })
        elif current_ret <= EXPIRED_BAND:
            suggestions.append({
                "step": name,
                "status": "late_marginal",
                "trigger_price": round(trigger_price, 2),
                "current_price": px,
                "msg": f"⚠️ {name} 滑点已大 (现 ${px:.2f} 高过触发 {current_ret*100:.1f}%), 慎入或跳过",
            })
        else:
            suggestions.append({
                "step": name,
                "status": "expired",
                "trigger_price": round(trigger_price, 2),
                "current_price": px,
                "msg": f"❌ {name} 已过期 (现 ${px:.2f} 高过触发 {current_ret*100:.1f}%), 建议跳过或重置 ladder",
            })
    return {"ticker": ticker, "step1_price": step1, "current_price": px,
            "ret_from_step1_pct": round((px/step1-1)*100, 2),
            "suggestions": suggestions}


def main():
    pf = json.loads(HOLDINGS.read_text(encoding="utf-8"))
    advisories = {}
    for t, h in pf.items():
        a = advise_one(t, h)
        if a and a["suggestions"]:
            advisories[t] = a

    OUT.write_text(json.dumps(advisories, ensure_ascii=False, indent=2), encoding="utf-8")

    if not advisories:
        print("No anti-martingale holdings with pending entry steps.")
        return

    print(f"=== Entry ladder advisory ({len(advisories)} tickers) ===\n")
    for t, a in advisories.items():
        print(f"{t}  step_1=${a['step1_price']:.2f}  now=${a['current_price']:.2f}  ({a['ret_from_step1_pct']:+.1f}%)")
        for s in a["suggestions"]:
            status_emoji = {"not_yet":"⏳","late_ok":"⏰","late_marginal":"⚠️","expired":"❌"}.get(s["status"],"")
            print(f"  {status_emoji} {s['msg']}")
        print()

    # Action plan
    print("=== Action plan ===")
    for t, a in advisories.items():
        for s in a["suggestions"]:
            if s["status"] == "late_ok":
                print(f"  📥 {t} {s['step']}: 仍可入场 @ 现价 ${s['current_price']:.2f}")
            elif s["status"] == "late_marginal":
                print(f"  🤔 {t} {s['step']}: 滑点偏大, 看好就入, 担心就跳")
            elif s["status"] == "expired":
                print(f"  🚫 {t} {s['step']}: 跳过此档. 总仓位将比目标小 {30 if s['step']=='step_2' else 30}%")


if __name__ == "__main__":
    main()
