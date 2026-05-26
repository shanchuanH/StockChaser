"""Minimal patch: change v1 hardcoded profit tiers +10/+20/+30 (25/25/33%)
to the reduced anti-martingale set +20/+40/+70 (15/15/20%).

v1 strategy-aware patch is already in git HEAD (martingale/high_conv branching
works). This ONLY swaps the 6 hardcoded numbers in the anti-martingale path.

Run in WSL: python3 scripts/patch_reduce_profit.py
Idempotent: checks for marker. Validates JS after.
"""
import re, subprocess, sys
from pathlib import Path
WEB = Path(__file__).resolve().parents[1] / "web" / "index.html"
src = WEB.read_text(encoding="utf-8")

if "__PROFIT_REDUCED_V2__" in src:
    print("- already applied"); sys.exit(0)

# Each tuple: (exact old substring, new substring). All must match exactly once.
SWAPS = [
    # --- verb logic ---
    ('}else if(!isHC && ret>=30){\n          verb="SELL"; verb_color="#3fb950";\n'
     '          const sellN = Math.floor(h.shares*0.33);\n'
     '          action_text="💰 卖 "+sellN+" 股 (33%)";\n'
     '          reason="+"+ret.toFixed(1)+"% 锁利润，剩 "+(h.shares-sellN)+" 股骑趋势";',
     '}else if(!isHC && ret>=70){\n          verb="SELL"; verb_color="#3fb950";\n'
     '          const sellN = Math.floor((h.peak_shares||h.shares)*0.20);\n'
     '          action_text="💰 卖 "+sellN+" 股 (tier_3 +70%)";\n'
     '          reason="+"+ret.toFixed(1)+"% tier_3 止盈，剩 "+(h.shares-sellN)+" 股骑趋势";'),
    ('}else if(!isHC && ret>=20){\n          verb="SELL"; verb_color="#3fb950";\n'
     '          const sellN = Math.floor(h.shares*0.25);\n'
     '          action_text="💰 卖 "+sellN+" 股 (25%)";\n'
     '          reason="+"+ret.toFixed(1)+"% 第二档止盈，剩 "+(h.shares-sellN)+" 股";',
     '}else if(!isHC && ret>=40){\n          verb="SELL"; verb_color="#3fb950";\n'
     '          const sellN = Math.floor((h.peak_shares||h.shares)*0.15);\n'
     '          action_text="💰 卖 "+sellN+" 股 (tier_2 +40%)";\n'
     '          reason="+"+ret.toFixed(1)+"% tier_2 止盈，剩 "+(h.shares-sellN)+" 股";'),
    ('          if(!isHC && ret>=10){\n            verb_color="#3fb950";\n'
     '            action_text="⬆️ 卖 "+Math.floor(h.shares*0.25)+" 股 (25%) 锁首档利润";\n'
     '            reason="+"+ret.toFixed(1)+"%，第一档止盈触发，止损上移到买入价 $"+h.buy_price.toFixed(2);',
     '          if(!isHC && ret>=20){\n            verb_color="#3fb950";\n'
     '            action_text="⬆️ 卖 "+Math.floor((h.peak_shares||h.shares)*0.15)+" 股 (tier_1 +20%) 锁首档";\n'
     '            reason="+"+ret.toFixed(1)+"%，tier_1 止盈触发，止损升保本";'),
    # --- ladder display ---
    ('const tp1_p = buyP*1.10, tp2_p = buyP*1.20, tp3_p = buyP*1.30;\n'
     '            const tp1_n = Math.floor(totShares*0.25), tp2_n = Math.floor(totShares*0.33);',
     'const tp1_p = buyP*1.20, tp2_p = buyP*1.40, tp3_p = buyP*1.70;\n'
     '            const pkN = c.h.peak_shares||totShares;\n'
     '            const tp1_n = Math.floor(pkN*0.15), tp2_n = Math.floor(pkN*0.20);'),
    ('html+=\'<span>\'+mark(c.px>=tp1_p)+\'</span><span class="num" style="color:#3fb950">+10%</span><span>$\'+tp1_p.toFixed(2)+\' → 卖 25% (\'+tp1_n+\'股)</span><span style="color:#8b949e">止损升保本</span>\';\n'
     '            html+=\'<span>\'+mark(c.px>=tp2_p)+\'</span><span class="num" style="color:#3fb950">+20%</span><span>$\'+tp2_p.toFixed(2)+\' → 卖 25% (\'+tp1_n+\'股)</span><span style="color:#8b949e">止损升+5%</span>\';\n'
     '            html+=\'<span>\'+mark(c.px>=tp3_p)+\'</span><span class="num" style="color:#3fb950">+30%</span><span>$\'+tp3_p.toFixed(2)+\' → 卖 33% (\'+tp2_n+\'股)</span><span style="color:#8b949e">余仓 SMA20 追踪</span>\';',
     'html+=\'<span>\'+mark(c.px>=tp1_p)+\'</span><span class="num" style="color:#3fb950">+20%</span><span>$\'+tp1_p.toFixed(2)+\' → 卖 15% (\'+tp1_n+\'股)</span><span style="color:#8b949e">止损升保本</span>\';\n'
     '            html+=\'<span>\'+mark(c.px>=tp2_p)+\'</span><span class="num" style="color:#3fb950">+40%</span><span>$\'+tp2_p.toFixed(2)+\' → 卖 15% (\'+tp1_n+\'股)</span><span style="color:#8b949e">止损升+15%</span>\';\n'
     '            html+=\'<span>\'+mark(c.px>=tp3_p)+\'</span><span class="num" style="color:#3fb950">+70%</span><span>$\'+tp3_p.toFixed(2)+\' → 卖 20% (\'+tp2_n+\'股)</span><span style="color:#8b949e">余仓 SMA20 追踪</span>\';'),
]

for i, (old, new) in enumerate(SWAPS):
    n = src.count(old)
    if n != 1:
        print(f"✗ swap {i+1}: matched {n}× (expected 1) — aborting, no write")
        sys.exit(1)
    src = src.replace(old, new)
    print(f"✓ swap {i+1} applied")

src = src.replace("</body>", "<!-- __PROFIT_REDUCED_V2__ -->\n</body>", 1)
WEB.write_text(src, encoding="utf-8")

scripts = re.findall(r"<script[^>]*>(.*?)</script>", src, re.DOTALL)
Path("/tmp/_v.js").write_text("\n".join(scripts))
r = subprocess.run(["node","--check","/tmp/_v.js"], capture_output=True, text=True)
if r.returncode == 0:
    print("✅ JS OK — 止盈档已改 +20/+40/+70, 15/15/20%")
else:
    print(f"🔴 JS ERROR: {r.stderr[:300]}\n   git checkout web/index.html"); sys.exit(1)
if src.count("</script>") != len(re.findall(r"<script", src)) or "</html>" not in src:
    print("🔴 HTML broken — git checkout web/index.html"); sys.exit(1)
print("✓ HTML structure intact")
