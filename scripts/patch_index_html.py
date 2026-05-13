"""One-shot patcher: re-add prompt functions + key UI fixes to web/index.html.

Run AFTER `git checkout 7373da3 -- web/index.html`. Idempotent —
checks for marker comments and skips if already applied.
"""
from pathlib import Path
import re
import sys

P = Path(__file__).resolve().parents[1] / "web" / "index.html"
src = P.read_text(encoding="utf-8")

# === Patch 1: add promptAddBatch + promptReduce after promptBuy ===
MARK_1 = "// __PATCH_PROMPT_FUNCS__"
if MARK_1 not in src:
    patch_1 = f"""{MARK_1}
async function promptAddBatch(ticker, currentPrice){{
  const pf = await pfLoad();
  const h = pf[ticker];
  if(!h){{ alert('未找到 '+ticker+' 持仓'); return; }}
  const cp = typeof currentPrice === 'number' ? currentPrice : parseFloat(currentPrice) || h.buy_price;
  const sharesStr = prompt(
    "➕ 加仓 "+ticker+"\\n现价: $"+cp.toFixed(2)+
    "\\n当前持仓: "+h.shares+" 股 @ 均价 $"+h.buy_price.toFixed(2)+
    "\\n\\n本次加仓多少股?", "5");
  if(!sharesStr) return;
  const shares = parseFloat(sharesStr);
  if(isNaN(shares) || shares <= 0){{ alert('股数无效'); return; }}
  const pxStr = prompt("成交价 ($)?", cp.toFixed(2));
  if(!pxStr) return;
  const px = parseFloat(pxStr);
  if(isNaN(px) || px <= 0){{ alert('价格无效'); return; }}
  try{{
    await pfAddBatch(ticker, px, shares);
    const newH = (await pfLoad())[ticker];
    alert("✓ 已加仓 "+shares+" 股 @ $"+px.toFixed(2)+
          "\\n新持仓: "+newH.shares+" 股 @ 均价 $"+newH.buy_price.toFixed(2));
    renderPortfolioView();
    render();
  }}catch(e){{ alert("加仓失败: "+e.message); }}
}}

async function promptReduce(ticker){{
  const pf = await pfLoad();
  const h = pf[ticker];
  if(!h){{ alert('未找到 '+ticker+' 持仓'); return; }}
  const sharesStr = prompt(
    "➖ 减仓 "+ticker+"\\n当前持仓: "+h.shares+" 股 @ 均价 $"+h.buy_price.toFixed(2)+
    "\\n\\n卖出多少股? (输入 "+h.shares+" 清仓)",
    Math.floor(h.shares/2).toString());
  if(!sharesStr) return;
  const sell = parseFloat(sharesStr);
  if(isNaN(sell) || sell <= 0){{ alert('股数无效'); return; }}
  if(sell > h.shares){{ alert('卖出股数不能超过持仓 '+h.shares); return; }}
  const remaining = +(h.shares - sell).toFixed(6);
  if(!confirm("确认 "+(remaining<=0?"清仓":"减至 "+remaining+" 股")+" "+ticker+"? (卖 "+sell+" 股)")) return;
  try{{
    await pfUpdateShares(ticker, remaining);
    alert("✓ "+(remaining<=0?"已清仓":"已减 "+sell+" 股，剩 "+remaining+" 股"));
    renderPortfolioView();
    render();
  }}catch(e){{ alert("减仓失败: "+e.message); }}
}}

"""
    # Insert right after promptBuy's closing brace
    needle = re.search(r"(function promptBuy\([^)]*\)\{[^}]*alert\([^)]*\);\s*\}\s*\n)", src, re.DOTALL)
    if needle:
        src = src[:needle.end()] + "\n" + patch_1 + src[needle.end():]
        print("✓ Patch 1: added promptAddBatch + promptReduce")
    else:
        print("✗ Patch 1: couldn't find promptBuy anchor"); sys.exit(1)
else:
    print("- Patch 1 already applied (marker found)")

P.write_text(src, encoding="utf-8")
print(f"\nSaved {P} ({len(src)} chars, {src.count(chr(10))+1} lines)")
print("Validate:")
print(f"  </script> count: {src.count('</script>')}")
print(f"  </body> count:   {src.count('</body>')}")
print(f"  promptAddBatch:  {'function promptAddBatch' in src}")
print(f"  promptReduce:    {'async function promptReduce' in src}")
