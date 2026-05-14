"""Make web/index.html renderPortfolioView() strategy-aware.

Problem: the dashboard hardcodes the +10/+20/+30 profit ladder + verb logic
for ALL holdings, ignoring h.strategy / h.high_conviction. Result:
  - martingale (AMZN/GOOGL/MSFT/ETF) wrongly shows 止盈阶梯
  - high_conviction (AAOI) wrongly shows 止盈阶梯

Fix: 2 precise replacements
  1. verb logic block → branch on strategy:
       martingale          → dip-buy verbs only, never sell
       anti_mart + high_conv → flash_8/stop/conv only, NO profit verbs
       anti_mart standard    → unchanged (flash_8/stop/conv + profit ladder)
  2. ladder display block → branch on strategy:
       martingale          → 💧 加仓阶梯 (-5/-10/-15), no 止损
       anti_mart + high_conv → 🔥 高确信 header + 止损阶梯 only
       anti_mart standard    → 📊 止盈阶梯 + 🛑 止损阶梯 (unchanged)

Run in WSL:  python3 scripts/patch_strategy_aware_ui.py
Idempotent — marker-guarded. Validates JS via `node --check` after.
"""
import re
import subprocess
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web" / "index.html"
src = WEB.read_text(encoding="utf-8")

# ============ Replacement 1: verb logic ============
OLD_VERB = """    else if(r){
      const conv=r.conviction_score||0;
      const pb=r.playbook;
      // Use user's actual entry-based stop (-8% from buy price)
      const personal_stop = h.buy_price * 0.92;
      // Single-day flash-crash protection (portfolio.json: 单日跌幅 > 8% 黑天鹅)
      // V2 增加的 -5% 档已被 4 年回测证伪 (156 次触发, 牛市损失 145pp), 已删除
      const daily_pct = r.daily_return_pct || 0;
      if(daily_pct <= -8){
        verb="SELL"; verb_color="#d63031";
        const cutN = Math.floor(h.shares * 0.7);
        action_text="🚨 减至 30%：卖 "+cutN+" 股 (70%)";
        reason="单日 "+daily_pct.toFixed(2)+"% 黑天鹅，剩 "+(h.shares-cutN)+" 股观察";
      }else if(px <= personal_stop){
        verb="SELL"; verb_color="#d63031";
        action_text="🛑 全清 "+h.shares+" 股 (100%)";
        reason="跌穿你买入价 $"+h.buy_price.toFixed(2)+" 的 -8% 止损 $"+personal_stop.toFixed(2);
      }else if(conv<45){
        verb="SELL"; verb_color="#f39c12";
        action_text="⚠️ 卖一半 "+Math.floor(h.shares/2)+" 股 (50%)";
        reason="Conv "+conv+" < 45，仓位减半";
      }else if(ret>=30){
        verb="SELL"; verb_color="#3fb950";
        const sellN = Math.floor(h.shares*0.33);
        action_text="💰 卖 "+sellN+" 股 (33%)";
        reason="+"+ret.toFixed(1)+"% 锁利润，剩 "+(h.shares-sellN)+" 股骑趋势";
      }else if(ret>=20){
        verb="SELL"; verb_color="#3fb950";
        const sellN = Math.floor(h.shares*0.25);
        action_text="💰 卖 "+sellN+" 股 (25%)";
        reason="+"+ret.toFixed(1)+"% 第二档止盈，剩 "+(h.shares-sellN)+" 股";
      }else if(pb && pb.entry_plan && h.batches && h.batches.length<pb.entry_plan.length){
        const next=pb.entry_plan[h.batches.length];
        if(next && px>=next.price*0.99){
          verb="BUY"; verb_color="#3fb950";
          const buyCash = (next.shares * next.price).toFixed(0);
          action_text="➕ 加仓 "+next.shares+" 股 (≈$"+buyCash+")";
          reason="第 "+next.step+" 批加仓点 $"+next.price+" 已触发";
        }
      }
      if(verb==="HOLD"){
        if(ret>=10){
          verb_color="#3fb950";
          action_text="⬆️ 卖 "+Math.floor(h.shares*0.25)+" 股 (25%) 锁首档利润";
          reason="+"+ret.toFixed(1)+"%，第一档止盈触发，止损上移到买入价 $"+h.buy_price.toFixed(2);
        }
        else if(ret<-3){ verb_color="#f39c12"; action_text="👀 盯紧止损 (-8% = $"+personal_stop.toFixed(2)+")"; reason=ret.toFixed(1)+"%，接近止损线"; }
        else { action_text="✓ 持有不动"; reason="新仓发酵中，未触发任何买卖点"; }
      }
    }"""

NEW_VERB = """    else if(r){
      // __PATCH_STRATEGY_VERB__
      const conv=r.conviction_score||0;
      const pb=r.playbook;
      const personal_stop = h.buy_price * 0.92;
      const daily_pct = r.daily_return_pct || 0;
      const strat = h.strategy || "anti_martingale";
      const isHC = !!h.high_conviction;
      const initN = h.peak_shares || h.shares;
      if(strat === "martingale"){
        // 马丁: 永不卖, 只在跌幅档加仓
        if(ret <= -15){
          verb="BUY"; verb_color="#58a6ff";
          action_text="💧💧💧 加 "+Math.floor(initN*1.0)+" 股 (跌 -15%)";
          reason=ret.toFixed(1)+"% 马丁重仓档";
        }else if(ret <= -10){
          verb="BUY"; verb_color="#58a6ff";
          action_text="💧💧 加 "+Math.floor(initN*0.5)+" 股 (跌 -10%)";
          reason=ret.toFixed(1)+"% 马丁中额档";
        }else if(ret <= -5){
          verb="BUY"; verb_color="#58a6ff";
          action_text="💧 加 "+Math.floor(initN*0.3)+" 股 (跌 -5%)";
          reason=ret.toFixed(1)+"% 马丁小额档";
        }else{
          verb="HOLD"; verb_color="#8b949e";
          action_text="✓ 买入持有";
          reason="不止盈不止损，跌 -5% 才加仓";
        }
      }else{
        // 反马丁 (标准 + high_conviction)
        if(daily_pct <= -8){
          verb="SELL"; verb_color="#d63031";
          const cutN = Math.floor(h.shares * 0.7);
          action_text="🚨 减至 30%：卖 "+cutN+" 股 (70%)";
          reason="单日 "+daily_pct.toFixed(2)+"% 黑天鹅，剩 "+(h.shares-cutN)+" 股观察";
        }else if(px <= personal_stop){
          verb="SELL"; verb_color="#d63031";
          action_text="🛑 全清 "+h.shares+" 股 (100%)";
          reason="跌穿你买入价 $"+h.buy_price.toFixed(2)+" 的 -8% 止损 $"+personal_stop.toFixed(2);
        }else if(conv<45){
          verb="SELL"; verb_color="#f39c12";
          action_text="⚠️ 卖一半 "+Math.floor(h.shares/2)+" 股 (50%)";
          reason="Conv "+conv+" < 45，仓位减半";
        }else if(!isHC && ret>=30){
          verb="SELL"; verb_color="#3fb950";
          const sellN = Math.floor(h.shares*0.33);
          action_text="💰 卖 "+sellN+" 股 (33%)";
          reason="+"+ret.toFixed(1)+"% 锁利润，剩 "+(h.shares-sellN)+" 股骑趋势";
        }else if(!isHC && ret>=20){
          verb="SELL"; verb_color="#3fb950";
          const sellN = Math.floor(h.shares*0.25);
          action_text="💰 卖 "+sellN+" 股 (25%)";
          reason="+"+ret.toFixed(1)+"% 第二档止盈，剩 "+(h.shares-sellN)+" 股";
        }else if(pb && pb.entry_plan && h.batches && h.batches.length<pb.entry_plan.length){
          const next=pb.entry_plan[h.batches.length];
          if(next && px>=next.price*0.99){
            verb="BUY"; verb_color="#3fb950";
            const buyCash = (next.shares * next.price).toFixed(0);
            action_text="➕ 加仓 "+next.shares+" 股 (≈$"+buyCash+")";
            reason="第 "+next.step+" 批加仓点 $"+next.price+" 已触发";
          }
        }
        if(verb==="HOLD"){
          if(!isHC && ret>=10){
            verb_color="#3fb950";
            action_text="⬆️ 卖 "+Math.floor(h.shares*0.25)+" 股 (25%) 锁首档利润";
            reason="+"+ret.toFixed(1)+"%，第一档止盈触发，止损上移到买入价 $"+h.buy_price.toFixed(2);
          }
          else if(isHC && ret>=10){
            verb_color="#ffa500";
            action_text="🔥 持有 (高确信不止盈)";
            reason="+"+ret.toFixed(1)+"%，骑全程，只在 -8% 止损";
          }
          else if(ret<-3){ verb_color="#f39c12"; action_text="👀 盯紧止损 (-8% = $"+personal_stop.toFixed(2)+")"; reason=ret.toFixed(1)+"%，接近止损线"; }
          else { action_text="✓ 持有不动"; reason="新仓发酵中，未触发任何买卖点"; }
        }
      }
    }"""

# ============ Replacement 2: ladder display ============
OLD_LADDER = """        // ====== 止盈 + 止损 完整阶梯 (基于你的买入价) ======
        const buyP = c.h.buy_price;
        const totShares = c.h.shares;
        const stop_p_full = buyP * 0.92;
        const tp1_p = buyP * 1.10;
        const tp2_p = buyP * 1.20;
        const tp3_p = buyP * 1.30;
        const tp1_n = Math.floor(totShares * 0.25);
        const tp2_n = Math.floor(totShares * 0.33);
        const half_n = Math.floor(totShares / 2);
        const hit = (target) => c.px >= target;
        const lossHit = c.px <= stop_p_full;
        // Status emoji per row
        const mark = (done) => done ? '✅' : '⏳';
        html+='<div style="background:#0d1218;border:1px solid #21262d;border-radius:5px;padding:8px 10px;margin-bottom:8px;font-size:10.5px">';
        html+='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px"><strong style="color:#3fb950">📊 止盈阶梯</strong><span style="color:#8b949e;font-size:9.5px">(基于你买入价 $'+buyP.toFixed(2)+')</span></div>';
        html+='<div style="display:grid;grid-template-columns:auto auto 1fr auto;column-gap:8px;row-gap:3px;color:#c9d1d9">';
        html+='<span>'+mark(hit(tp1_p))+'</span><span class="num" style="color:#3fb950">+10%</span><span>$'+tp1_p.toFixed(2)+' → 卖 25% ('+tp1_n+'股)</span><span style="color:#8b949e">止损升保本</span>';
        html+='<span>'+mark(hit(tp2_p))+'</span><span class="num" style="color:#3fb950">+20%</span><span>$'+tp2_p.toFixed(2)+' → 卖 25% ('+tp1_n+'股)</span><span style="color:#8b949e">止损升+5%</span>';
        html+='<span>'+mark(hit(tp3_p))+'</span><span class="num" style="color:#3fb950">+30%</span><span>$'+tp3_p.toFixed(2)+' → 卖 33% ('+tp2_n+'股)</span><span style="color:#8b949e">余仓 SMA20 追踪</span>';
        html+='</div>';
        html+='<div style="margin-top:6px;padding-top:6px;border-top:1px dashed #30363d;display:flex;justify-content:space-between;align-items:center"><strong style="color:#ff6b6b">🛑 止损阶梯</strong><span style="color:#8b949e;font-size:9.5px">硬规则</span></div>';
        html+='<div style="display:grid;grid-template-columns:auto auto 1fr auto;column-gap:8px;row-gap:3px;margin-top:3px;color:#c9d1d9">';
        const daily_v = (c.r && c.r.daily_return_pct) || 0;
        const flashMark = daily_v <= -8 ? '🔴' : '⏳';
        html+='<span>'+flashMark+'</span><span class="num" style="color:#ff6b6b">单日≤-8%</span><span>今日 '+daily_v.toFixed(2)+'% → <strong>减至 30%（卖 70%）</strong></span><span style="color:#8b949e">黑天鹅</span>';
        const stopMark = lossHit ? '🔴' : '⏳';
        html+='<span>'+stopMark+'</span><span class="num" style="color:#ff6b6b">-8%</span><span>$'+stop_p_full.toFixed(2)+' → <strong>一次性全清 '+totShares+' 股 (100%)</strong></span><span style="color:#8b949e">硬止损</span>';
        const convVal = (c.r && c.r.conviction_score) || 100;
        const convMark = convVal < 45 ? '🔴' : '⏳';
        html+='<span>'+convMark+'</span><span class="num" style="color:#f39c12">Conv&lt;45</span><span>当前 '+convVal+' → <strong>卖一半 '+half_n+' 股 (50%)</strong></span><span style="color:#8b949e">基本面恶化</span>';
        html+='</div></div>';"""

NEW_LADDER = """        // ====== 阶梯显示 — strategy-aware __PATCH_STRATEGY_LADDER__ ======
        const buyP = c.h.buy_price;
        const totShares = c.h.shares;
        const stratL = c.h.strategy || "anti_martingale";
        const isHCL = !!c.h.high_conviction;
        const stop_p_full = buyP * 0.92;
        const half_n = Math.floor(totShares / 2);
        const lossHit = c.px <= stop_p_full;
        const mark = (done) => done ? '✅' : '⏳';
        const daily_v = (c.r && c.r.daily_return_pct) || 0;
        const convVal = (c.r && c.r.conviction_score) || 100;
        html+='<div style="background:#0d1218;border:1px solid #21262d;border-radius:5px;padding:8px 10px;margin-bottom:8px;font-size:10.5px">';
        if(stratL === "martingale"){
          // 马丁: 加仓阶梯 (越跌越买), 无止损无止盈
          const initN = c.h.peak_shares || totShares;
          const dip1 = buyP*0.95, dip2 = buyP*0.90, dip3 = buyP*0.85;
          html+='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px"><strong style="color:#58a6ff">💧 加仓阶梯 (越跌越买)</strong><span style="color:#8b949e;font-size:9.5px">(基于买入价 $'+buyP.toFixed(2)+')</span></div>';
          html+='<div style="display:grid;grid-template-columns:auto auto 1fr auto;column-gap:8px;row-gap:3px;color:#c9d1d9">';
          html+='<span>'+mark(c.px<=dip1)+'</span><span class="num" style="color:#58a6ff">-5%</span><span>$'+dip1.toFixed(2)+' → 加 '+Math.floor(initN*0.30)+' 股 (30%)</span><span style="color:#8b949e">小额</span>';
          html+='<span>'+mark(c.px<=dip2)+'</span><span class="num" style="color:#58a6ff">-10%</span><span>$'+dip2.toFixed(2)+' → 加 '+Math.floor(initN*0.50)+' 股 (50%)</span><span style="color:#8b949e">中额</span>';
          html+='<span>'+mark(c.px<=dip3)+'</span><span class="num" style="color:#58a6ff">-15%</span><span>$'+dip3.toFixed(2)+' → 加 '+Math.floor(initN*1.00)+' 股 (100%)</span><span style="color:#8b949e">重额</span>';
          html+='</div>';
          html+='<div style="margin-top:6px;padding-top:6px;border-top:1px dashed #30363d;color:#8b949e;font-size:9.5px">📌 买入持有 · 不止盈不止损 · 跌 -5% 才加仓</div>';
        }else{
          // 反马丁: 止损阶梯 always; 止盈阶梯 only if NOT high_conviction
          if(!isHCL){
            const tp1_p = buyP*1.10, tp2_p = buyP*1.20, tp3_p = buyP*1.30;
            const tp1_n = Math.floor(totShares*0.25), tp2_n = Math.floor(totShares*0.33);
            html+='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px"><strong style="color:#3fb950">📊 止盈阶梯</strong><span style="color:#8b949e;font-size:9.5px">(基于买入价 $'+buyP.toFixed(2)+')</span></div>';
            html+='<div style="display:grid;grid-template-columns:auto auto 1fr auto;column-gap:8px;row-gap:3px;color:#c9d1d9">';
            html+='<span>'+mark(c.px>=tp1_p)+'</span><span class="num" style="color:#3fb950">+10%</span><span>$'+tp1_p.toFixed(2)+' → 卖 25% ('+tp1_n+'股)</span><span style="color:#8b949e">止损升保本</span>';
            html+='<span>'+mark(c.px>=tp2_p)+'</span><span class="num" style="color:#3fb950">+20%</span><span>$'+tp2_p.toFixed(2)+' → 卖 25% ('+tp1_n+'股)</span><span style="color:#8b949e">止损升+5%</span>';
            html+='<span>'+mark(c.px>=tp3_p)+'</span><span class="num" style="color:#3fb950">+30%</span><span>$'+tp3_p.toFixed(2)+' → 卖 33% ('+tp2_n+'股)</span><span style="color:#8b949e">余仓 SMA20 追踪</span>';
            html+='</div>';
            html+='<div style="margin-top:6px;padding-top:6px;border-top:1px dashed #30363d;display:flex;justify-content:space-between;align-items:center"><strong style="color:#ff6b6b">🛑 止损阶梯</strong><span style="color:#8b949e;font-size:9.5px">硬规则</span></div>';
          }else{
            html+='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px"><strong style="color:#ffa500">🔥 高确信 — 不止盈, 骑全程</strong><span style="color:#8b949e;font-size:9.5px">单一 -8% 止损</span></div>';
          }
          html+='<div style="display:grid;grid-template-columns:auto auto 1fr auto;column-gap:8px;row-gap:3px;margin-top:3px;color:#c9d1d9">';
          const flashMark = daily_v <= -8 ? '🔴' : '⏳';
          html+='<span>'+flashMark+'</span><span class="num" style="color:#ff6b6b">单日≤-8%</span><span>今日 '+daily_v.toFixed(2)+'% → <strong>减至 30%（卖 70%）</strong></span><span style="color:#8b949e">黑天鹅</span>';
          const stopMark = lossHit ? '🔴' : '⏳';
          html+='<span>'+stopMark+'</span><span class="num" style="color:#ff6b6b">-8%</span><span>$'+stop_p_full.toFixed(2)+' → <strong>一次性全清 '+totShares+' 股 (100%)</strong></span><span style="color:#8b949e">硬止损</span>';
          const convMark = convVal < 45 ? '🔴' : '⏳';
          html+='<span>'+convMark+'</span><span class="num" style="color:#f39c12">Conv&lt;45</span><span>当前 '+convVal+' → <strong>卖一半 '+half_n+' 股 (50%)</strong></span><span style="color:#8b949e">基本面恶化</span>';
          html+='</div>';
        }
        html+='</div>';"""


def apply_patch(name, old, new):
    global src
    if "__PATCH_STRATEGY_VERB__" in src and name == "verb":
        print(f"- {name}: already patched (marker found)"); return True
    if "__PATCH_STRATEGY_LADDER__" in src and name == "ladder":
        print(f"- {name}: already patched (marker found)"); return True
    if old not in src:
        print(f"✗ {name}: OLD block not found — file may have drifted")
        return False
    cnt = src.count(old)
    if cnt != 1:
        print(f"✗ {name}: OLD block found {cnt}× (expected 1) — ambiguous, aborting")
        return False
    src = src.replace(old, new)
    print(f"✓ {name}: replaced ({len(old)} → {len(new)} chars)")
    return True


def main():
    ok1 = apply_patch("verb", OLD_VERB, NEW_VERB)
    ok2 = apply_patch("ladder", OLD_LADDER, NEW_LADDER)
    if not (ok1 and ok2):
        print("\n⚠️ Aborted — no changes written.")
        sys.exit(1)

    WEB.write_text(src, encoding="utf-8")

    # Validate JS
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", src, re.DOTALL)
    Path("/tmp/_sv.js").write_text("\n".join(scripts))
    try:
        r = subprocess.run(["node", "--check", "/tmp/_sv.js"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print("\n✅ JS syntax OK. web/index.html is now strategy-aware.")
            print("   - martingale → 💧 加仓阶梯, no 止盈/止损")
            print("   - high_conviction → 🔥 止损阶梯 only, no 止盈")
            print("   - anti_martingale 标准 → 📊 止盈 + 🛑 止损 (unchanged)")
        else:
            print(f"\n🔴 JS SYNTAX ERROR after patch:\n{r.stderr[:400]}")
            print("   web/index.html written but BROKEN — run: git checkout web/index.html")
            sys.exit(1)
    except FileNotFoundError:
        print("\n⚠️ node not found — cannot validate JS. Check manually before pushing.")

    # Final structural check
    if src.count("</script>") != len(re.findall(r"<script", src)):
        print("🔴 script tag mismatch — DO NOT push, run git checkout web/index.html")
        sys.exit(1)
    if "</body>" not in src or "</html>" not in src:
        print("🔴 missing closing tags — DO NOT push")
        sys.exit(1)
    print("✓ HTML structure intact (<script> balanced, </body></html> present)")


if __name__ == "__main__":
    main()
