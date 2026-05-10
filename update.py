"""StockChaser one-click update.

Run me each Monday morning before market open:
    python update.py

This will:
  1. Re-extract the ticker universe from AI_Chain_Watchlist.xlsx
  2. Fetch fresh price data via yfinance
  3. Recompute signals and composite scores
  4. Print top-15 highlights to stdout

Then open web/index.html in your browser.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"

USE_MOCK = "--mock" in sys.argv

steps = [
    ("提取 ticker 列表",      [sys.executable, str(SCRIPTS / "extract_universe.py")]),
    ("抓取 yfinance 价格",
        [sys.executable, str(SCRIPTS / ("fetch_mock.py" if USE_MOCK else "fetch_data.py"))]),
    ("计算追涨信号",          [sys.executable, str(SCRIPTS / "signal.py")]),
]

for name, cmd in steps:
    print(f"\n━━━ {name} ━━━")
    rc = subprocess.call(cmd)
    if rc != 0:
        print(f"\n❌ 步骤失败: {name}  (exit {rc})")
        sys.exit(rc)

print("\n✅ 全部完成。打开 web/index.html 查看看板。")
print(f"   {ROOT / 'web' / 'index.html'}")
