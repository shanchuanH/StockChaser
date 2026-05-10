from __future__ import annotations
import json
import os  # 新增
import sys
import time
import argparse
import threading
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from flask import Flask, jsonify, send_file, request, Response
except ImportError:
    print("ERROR: Flask not installed. Run: pip install flask")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
SCRIPTS = ROOT / "scripts"

# 确保 data 目录存在，否则云端运行会报错
DATA.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(WEB), static_url_path="")

_state = {
    "last_refresh": None,
    "last_status": "never",
    "next_refresh": None,
    "refreshing": False,
    "interval_min": 5,
}
_lock = threading.Lock()

ET = timezone(timedelta(hours=-4)) 

def in_market_hours(now_utc: datetime) -> bool:
    et = now_utc.astimezone(ET)
    if et.weekday() >= 5: 
        return False
    h, m = et.hour, et.minute
    if h < 9 or (h == 9 and m < 30):
        return False
    if h > 16 or (h == 16 and m > 0):
        return False
    return True

def run_pipeline(use_mock: bool = False) -> tuple[bool, str]:
    fetch_script = "fetch_mock.py" if use_mock else "fetch_data.py"
    steps = [
        SCRIPTS / "extract_universe.py",
        SCRIPTS / fetch_script,
        SCRIPTS / "signal.py",
    ]
    for s in steps:
        try:
            # 使用 sys.executable 确保调用云端环境的 python
            r = subprocess.run([sys.executable, str(s)], capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                return False, f"{s.name} failed: {r.stderr[:300]}"
        except Exception as e:
            return False, f"{s.name} error: {str(e)}"
    return True, "ok"

def background_refresh(use_mock: bool):
    while True:
        now_utc = datetime.now(timezone.utc)
        market_open = in_market_hours(now_utc)
        interval_min = 5 if market_open else 30
        _state["interval_min"] = interval_min

        with _lock:
            if not _state["refreshing"]:
                _state["refreshing"] = True
                ok, msg = run_pipeline(use_mock=use_mock)
                with _lock:
                    _state["refreshing"] = False
                    _state["last_refresh"] = now_utc.isoformat(timespec="seconds")
                    _state["last_status"] = "ok" if ok else f"error: {msg}"
                    _state["next_refresh"] = (now_utc + timedelta(minutes=interval_min)).isoformat(timespec="seconds")
                print(f"[{now_utc:%H:%M:%S}] Auto-refresh complete.")
        
        time.sleep(interval_min * 60)

# --- 关键修改：针对 Gunicorn 启动后台线程 ---
# 如果不是通过命令行 main 启动（比如用 gunicorn），我们需要在这里启动线程
if os.environ.get("RENDER"):
    t = threading.Thread(target=background_refresh, args=(False,), daemon=True)
    t.start()
    print("Cloud background refresher started.")

# ---------- routes ----------
@app.route("/")
def index():
    return send_file(WEB / "index.html")

@app.route("/api/data")
def api_data():
    fp = DATA / "signals.json"
    if not fp.exists():
        # 云端优化：如果没有文件，尝试跑一次更新而不是直接报错
        return jsonify({"error": "No data yet. Hit /api/refresh"}), 404
    return Response(fp.read_text(encoding="utf-8"), mimetype="application/json")

# ... (api_backtest 和 api_status 保持不变) ...

@app.route("/api/refresh", methods=["POST", "GET"])
def api_refresh():
    use_mock = request.args.get("mock") == "1"
    with _lock:
        if _state["refreshing"]:
            return jsonify({"status": "already_refreshing"}), 429
        _state["refreshing"] = True
    
    ok, msg = run_pipeline(use_mock=use_mock)
    
    with _lock:
        _state["refreshing"] = False
        _state["last_refresh"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _state["last_status"] = "ok" if ok else f"error: {msg}"
    
    if not ok:
        return jsonify({"status": "error", "message": msg}), 500
    
    fp = DATA / "signals.json"
    return Response(fp.read_text(encoding="utf-8"), mimetype="application/json")

def main():
    ap = argparse.ArgumentParser()
    # 适配环境变量中的 PORT
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host", default="0.0.0.0") # 云端必须监听 0.0.0.0
    ap.add_argument("--no-auto", action="store_true")
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()

    if not args.no_auto and not os.environ.get("RENDER"):
        t = threading.Thread(target=background_refresh, args=(args.mock,), daemon=True)
        t.start()
        print(f"Local Background refresher started.")

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()