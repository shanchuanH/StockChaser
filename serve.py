"""StockChaser local server with auto-refresh.

Run:
    python serve.py
Then open: http://localhost:8000

Background scheduler refreshes prices automatically:
  - Every 5 min during US market hours (Mon-Fri 09:30-16:00 ET)
  - Every 30 min outside market hours

API endpoints:
  GET  /                   → dashboard HTML
  GET  /api/data           → latest signals.json
  GET  /api/backtest       → latest backtest_results.json
  POST /api/refresh        → force immediate refresh (returns new data)
  GET  /api/status         → last refresh time + next scheduled

Use --port to override (default 8000), --no-auto to disable background refresh.
"""
from __future__ import annotations
import json
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

app = Flask(__name__, static_folder=str(WEB), static_url_path="")

_state = {
    "last_refresh": None,
    "last_status": "never",
    "next_refresh": None,
    "refreshing": False,
    "interval_min": 5,
}
_lock = threading.Lock()

ET = timezone(timedelta(hours=-4))   # US Eastern (rough; ignores DST gap)


def in_market_hours(now_utc: datetime) -> bool:
    et = now_utc.astimezone(ET)
    if et.weekday() >= 5:  # Sat/Sun
        return False
    h, m = et.hour, et.minute
    if h < 9 or (h == 9 and m < 30):
        return False
    if h > 16 or (h == 16 and m > 0):
        return False
    return True


def run_pipeline(use_mock: bool = False) -> tuple[bool, str]:
    """Run extract → fetch → compute. Return (ok, message)."""
    fetch_script = "fetch_mock.py" if use_mock else "fetch_data.py"
    steps = [
        SCRIPTS / "extract_universe.py",
        SCRIPTS / fetch_script,
        SCRIPTS / "signal.py",
    ]
    for s in steps:
        try:
            r = subprocess.run([sys.executable, str(s)], capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                return False, f"{s.name} failed: {r.stderr[:300]}"
        except subprocess.TimeoutExpired:
            return False, f"{s.name} timed out"
        except Exception as e:
            return False, f"{s.name} error: {e}"
    return True, "ok"


def background_refresh(use_mock: bool):
    """Run forever, refresh on a market-aware schedule."""
    while True:
        now_utc = datetime.now(timezone.utc)
        market_open = in_market_hours(now_utc)
        interval_min = 5 if market_open else 30
        _state["interval_min"] = interval_min

        with _lock:
            _state["refreshing"] = True
        ok, msg = run_pipeline(use_mock=use_mock)
        with _lock:
            _state["refreshing"] = False
            _state["last_refresh"] = now_utc.isoformat(timespec="seconds")
            _state["last_status"] = "ok" if ok else f"error: {msg}"
            _state["next_refresh"] = (now_utc + timedelta(minutes=interval_min)).isoformat(timespec="seconds")
        print(f"[{now_utc:%H:%M:%S}] refresh {_state['last_status']}  next in {interval_min}min  market_open={market_open}")
        time.sleep(interval_min * 60)


# ---------- routes ----------
@app.route("/")
def index():
    return send_file(WEB / "index.html")


@app.route("/api/data")
def api_data():
    fp = DATA / "signals.json"
    if not fp.exists():
        return jsonify({"error": "signals.json not generated yet — run python update.py first"}), 404
    return Response(fp.read_text(encoding="utf-8"), mimetype="application/json")


@app.route("/api/backtest")
def api_backtest():
    fp = DATA / "backtest_results.json"
    if not fp.exists():
        return jsonify({"error": "no backtest yet — run python scripts/backtest.py"}), 404
    return Response(fp.read_text(encoding="utf-8"), mimetype="application/json")


@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify(_state)


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
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-auto", action="store_true", help="disable background auto-refresh")
    ap.add_argument("--mock", action="store_true", help="use mock data instead of yfinance")
    args = ap.parse_args()

    if not args.no_auto:
        t = threading.Thread(target=background_refresh, args=(args.mock,), daemon=True)
        t.start()
        print(f"Background refresher started (mock={args.mock})")

    print(f"\n🚀 StockChaser running at  http://{args.host}:{args.port}")
    print(f"   Open this URL in your browser.\n")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
