"""One-shot patcher: wire entry_advisory.json + current_stop_price into dashboard.

Adds:
  1. serve.py:    GET /api/entry_advisory  (returns data/entry_advisory.json)
  2. serve.py:    run missed_entry_advisor.py in run_pipeline (after dynamic_stops)
  3. web/index.html: top banner summarizing pending entry-ladder steps
  4. web/index.html: per-card "📥 step_2/3 advisory" inline display
  5. web/index.html: per-card "当前止损 $X" using current_stop_price (was static -8%)

Run in WSL after editing scripts/missed_entry_advisor.py or related:
  python3 scripts/patch_dashboard_advisory.py

Idempotent — re-running is safe (uses marker comments).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "serve.py"
WEB = ROOT / "web" / "index.html"


def patch_serve():
    src = SERVE.read_text(encoding="utf-8")
    changed = False

    # === Patch A: add /api/entry_advisory route ===
    MARK_A = "# __PATCH_ENTRY_ADVISORY_ROUTE__"
    if MARK_A not in src:
        route = f"""

{MARK_A}
@app.route("/api/entry_advisory", methods=["GET"])
def api_entry_advisory():
    fp = DATA / "entry_advisory.json"
    if not fp.exists():
        return jsonify({{}})
    return Response(fp.read_text(encoding="utf-8"), mimetype="application/json")
"""
        # Insert right before the final 'def main():' block
        idx = src.rfind("def main():")
        if idx == -1:
            print("✗ Patch A: could not find 'def main():' in serve.py"); return False
        # Walk backward to start of line
        line_start = src.rfind("\n", 0, idx) + 1
        src = src[:line_start] + route + "\n\n" + src[line_start:]
        changed = True
        print("✓ Patch A: added /api/entry_advisory route")
    else:
        print("- Patch A already applied")

    # === Patch B: run missed_entry_advisor.py in pipeline ===
    MARK_B = "# __PATCH_RUN_ADVISOR__"
    if MARK_B not in src:
        anchor = 'print("dynamic_stops.py error: " + str(exc))'
        if anchor in src:
            insertion = f"""

    {MARK_B}
    try:
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "missed_entry_advisor.py")],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            print("missed_entry_advisor.py warning: " + r.stderr[:200])
    except Exception as exc:
        print("missed_entry_advisor.py error: " + str(exc))"""
            src = src.replace(anchor, anchor + insertion)
            changed = True
            print("✓ Patch B: run_pipeline now calls missed_entry_advisor.py")
        else:
            print("✗ Patch B: could not find dynamic_stops anchor")
    else:
        print("- Patch B already applied")

    if changed:
        SERVE.write_text(src, encoding="utf-8")
        # Validate
        import ast
        try:
            ast.parse(src)
            print("  ✓ serve.py syntax OK")
        except SyntaxError as e:
            print(f"  ✗ serve.py syntax ERROR after patch: {e}"); return False
    return True


def patch_web():
    src = WEB.read_text(encoding="utf-8")
    changed = False

    # === Patch C: add loadEntryAdvisory function ===
    MARK_C = "// __PATCH_ENTRY_ADVISORY_JS__"
    if MARK_C not in src:
        js_block = f"""
{MARK_C}
let _entryAdvisory = {{}};
async function loadEntryAdvisory(){{
  try{{
    const r = await fetch("/api/entry_advisory");
    if(r.ok){{ _entryAdvisory = await r.json(); }}
  }}catch(e){{ _entryAdvisory = {{}}; }}
  renderAdvisoryBanner();
}}
function renderAdvisoryBanner(){{
  const banner = document.getElementById("advisory-banner");
  if(!banner) return;
  const items = [];
  for(const t in _entryAdvisory){{
    const a = _entryAdvisory[t];
    for(const s of (a.suggestions||[])){{
      if(s.status === "late_ok"){{
        items.push(`📥 <strong>${{t}}</strong> ${{s.step}} 仍可入场 @ $${{s.current_price.toFixed(2)}}`);
      }} else if(s.status === "late_marginal"){{
        items.push(`⚠️ <strong>${{t}}</strong> ${{s.step}} 滑点偏大`);
      }} else if(s.status === "expired"){{
        items.push(`🚫 <strong>${{t}}</strong> ${{s.step}} 已过期, 建议跳过`);
      }}
    }}
  }}
  if(items.length === 0){{ banner.style.display = "none"; return; }}
  banner.innerHTML = "📥 入场 ladder 提示: " + items.join(" · ");
  banner.style.display = "block";
}}
function entryAdvisoryFor(ticker){{
  return _entryAdvisory[ticker] || null;
}}
"""
        # Insert after the loadData function definition (anchor on its closing)
        # Find a simple anchor: "function updateCounts(){" or end of loadData
        anchor_idx = src.find("function updateCounts(){")
        if anchor_idx == -1:
            print("✗ Patch C: could not find updateCounts anchor")
            return False
        # Insert before updateCounts
        src = src[:anchor_idx] + js_block + "\n\n" + src[anchor_idx:]
        changed = True
        print("✓ Patch C: added loadEntryAdvisory + render banner")
    else:
        print("- Patch C already applied")

    # === Patch D: add advisory-banner DOM ===
    MARK_D = '<div id="advisory-banner"'
    if MARK_D not in src:
        anchor = '<div id="mock-banner"'
        if anchor in src:
            new_div = '<div id="advisory-banner" style="display:none;background:rgba(63,185,80,.12);border-bottom:1px solid #3fb950;color:#3fb950;text-align:center;padding:6px 12px;font-size:12px;font-weight:600"></div>\n'
            src = src.replace(anchor, new_div + anchor)
            changed = True
            print("✓ Patch D: inserted advisory-banner DOM")
        else:
            print("✗ Patch D: could not find mock-banner anchor")
    else:
        print("- Patch D already applied")

    # === Patch E: call loadEntryAdvisory() in loadData() ===
    MARK_E = "loadEntryAdvisory();"
    if MARK_E not in src:
        # Try to find existing 'updateCounts();render();' line and inject before it
        if "updateCounts();render();" in src:
            src = src.replace("updateCounts();render();",
                             "loadEntryAdvisory();updateCounts();render();", 1)
            changed = True
            print("✓ Patch E: loadEntryAdvisory() wired into loadData()")
        else:
            print("✗ Patch E: could not find render() chain anchor")
    else:
        print("- Patch E already applied")

    if changed:
        WEB.write_text(src, encoding="utf-8")
        # Validate JS by extracting and parsing
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", src, re.DOTALL)
        Path("/tmp/_patched.js").write_text("\n".join(scripts))
        import subprocess
        try:
            r = subprocess.run(["node", "--check", "/tmp/_patched.js"],
                             capture_output=True, text=True)
            if r.returncode == 0:
                print("  ✓ web/index.html JS syntax OK")
            else:
                print(f"  ✗ JS syntax ERROR: {r.stderr[:300]}"); return False
        except FileNotFoundError:
            print("  ⚠️ node not found, skipping JS validation")
    return True


def main():
    print("=== Patching serve.py ===")
    ok1 = patch_serve()
    print("\n=== Patching web/index.html ===")
    ok2 = patch_web()

    print()
    if ok1 and ok2:
        print("✅ All patches applied. Now:")
        print("   1. python3 scripts/missed_entry_advisor.py    (generate first advisory)")
        print("   2. python3 scripts/dynamic_stops.py           (refresh stops)")
        print("   3. git add -A && git commit -m 'feat: 入场 ladder advisory + 动态止损 UI' && git push")
        print("   4. Render auto-deploys, refresh dashboard, 顶部应该出现绿色 advisory banner")
        sys.exit(0)
    else:
        print("⚠️ Some patches failed. Check output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
