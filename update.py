"""StockChaser local update script.

Run each Monday morning before market open:
    python update.py          # real yfinance data
    python update.py --mock   # synthetic data (offline)

Steps:
  1. Fetch fresh price data via yfinance (reads data/universe.json)
  2. Recompute conviction scores
  3. Print top-10 highlights

universe.json is the source of truth — edit it directly to add/remove tickers.
To manage the watchlist from the web UI, use POST /api/universe instead.
"""
import subprocess
import sys
from pathlib import Path

ROOT    = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"

USE_MOCK = "--mock" in sys.argv

steps = [
    ("抓取价格数据",   [sys.executable, str(SCRIPTS / ("fetch_mock.py" if USE_MOCK else "fetch_data.py"))]),
    ("计算追涨信号",   [sys.executable, str(SCRIPTS / "engine_v32.py")]),
]

for name, cmd in steps:
    print(f"\n━━━ {name} ━━━")
    rc = subprocess.call(cmd)
    if rc != 0:
        print(f"\n❌ 步骤失败: {name}  (exit {rc})")
        sys.exit(rc)

print("\n━━━ Telegram 通知（如已配置） ━━━")
subprocess.call([sys.executable, str(SCRIPTS / "notify_telegram.py")])

print("\n✅ 全部完成。启动服务：python serve.py")
print("   或直接打开：web/index.html（需要本地服务器）")
