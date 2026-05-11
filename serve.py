from __future__ import annotations
import json
import os
import sys
import time
import argparse
import threading
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

try:
    from flask import Flask, jsonify, send_file, request, Response
except ImportError:
    print("ERROR: Flask not installed. Run: pip install flask")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent
WEB  = ROOT / "web"
DATA = ROOT / "data"
SCRIPTS = ROOT / "scripts"

DATA.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(WEB), static_url_path="")

_state = {
    "last_refresh": None,
    "last_status":  "never",
    "next_refresh": None,
    "refreshing":   False,
    "interval_min": 5,
}
_lock = threading.Lock()


def in_market_hours(now_utc):
    if ZoneInfo is not None:
        et = now_utc.astimezone(ZoneInfo("America/New_York"))
    else:
        et = now_utc.astimezone(timezone(timedelta(hours=-5)))
    if et.weekday() >= 5:
        return False
    h, m = et.hour, et.minute
    if h < 9 or (h == 9 and m < 30):
        return False
    if h > 16 or (h == 16 and m > 0):
        return False
    return True


def run_pipeline(use_mock=False):
    fetch_script = "fetch_mock.py" if use_mock else "fetch_data.py"
    steps = [SCRIPTS / fetch_script, SCRIPTS / "engine_v32.py"]
    for s in steps:
        try:
            r = subprocess.run(
                [sys.executable, str(s)],
                capture_output=True, text=True, timeout=300,
            )
            if r.returncode != 0:
                return False, s.name + " failed: " + r.stderr[:400]
        except Exception as exc:
            return False, s.name + " error: " + str(exc)

    # Best-effort: push priority change to Telegram. Never fail the pipeline.
    try:
        subprocess.run(
            [sys.executable, str(SCRIPTS / "notify_telegram.py")],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        print("notify_telegram dispatch error: " + str(exc))

    return True, "ok"


def _record_pipeline_result(ok, msg):
    now = datetime.now(timezone.utc)
    interval_min = 5 if in_market_hours(now) else 30
    with _lock:
        _state["refreshing"] = False
        _state["last_refresh"] = now.isoformat(timespec="seconds")
        _state["last_status"] = "ok" if ok else ("error: " + msg)
        _state["interval_min"] = interval_min
        _state["next_refresh"] = (now + timedelta(minutes=interval_min)).isoformat(timespec="seconds")
    print("[" + now.strftime("%H:%M:%S") + "] Pipeline " + ("ok" if ok else "ERROR: " + msg))


def _do_refresh(use_mock):
    ok, msg = False, "unknown"
    try:
        ok, msg = run_pipeline(use_mock=use_mock)
    except Exception as exc:
        ok, msg = False, str(exc)
    _record_pipeline_result(ok, msg)


def _signals_healthy():
    fp = DATA / "signals.json"
    if not fp.exists():
        return False
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
        return bool(d.get("rows")) and not d.get("_stale")
    except Exception:
        return False


def background_refresh(use_mock=False):
    if not _signals_healthy():
        print("signals.json missing/corrupt/empty - running initial pipeline...")
        with _lock:
            _state["refreshing"] = True
        _do_refresh(use_mock)

    while True:
        now_utc = datetime.now(timezone.utc)
        interval_min = 5 if in_market_hours(now_utc) else 30
        with _lock:
            _state["interval_min"] = interval_min
            _state["next_refresh"] = (now_utc + timedelta(minutes=interval_min)).isoformat(timespec="seconds")

        time.sleep(interval_min * 60)

        with _lock:
            if _state["refreshing"]:
                continue
            _state["refreshing"] = True

        _do_refresh(use_mock)


_IS_RENDER = bool(os.environ.get("RENDER"))
_USE_MOCK  = os.environ.get("USE_MOCK") == "1"

if _IS_RENDER:
    _t = threading.Thread(target=background_refresh, args=(_USE_MOCK,), daemon=True)
    _t.start()
    print("Background refresher started (Render mode).")


@app.route("/")
def index():
    return send_file(WEB / "index.html")


@app.route("/api/data")
def api_data():
    fp = DATA / "signals.json"
    if not fp.exists():
        return jsonify({"error": "Data not ready."}), 404
    return Response(fp.read_text(encoding="utf-8"), mimetype="application/json")


@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify(dict(_state))


@app.route("/api/backtest")
def api_backtest():
    fp = DATA / "backtest_results.json"
    if not fp.exists():
        return jsonify({"error": "Backtest not run yet."}), 404
    return Response(fp.read_text(encoding="utf-8"), mimetype="application/json")


@app.route("/api/refresh", methods=["GET", "POST"])
def api_refresh():
    use_mock = request.args.get("mock") == "1" or _USE_MOCK
    with _lock:
        if _state["refreshing"]:
            return jsonify({"status": "already_refreshing"}), 429
        _state["refreshing"] = True

    ok, msg = False, "unknown"
    try:
        ok, msg = run_pipeline(use_mock=use_mock)
    except Exception as exc:
        ok, msg = False, str(exc)
    _record_pipeline_result(ok, msg)

    if not ok:
        return jsonify({"status": "error", "message": msg}), 500

    fp = DATA / "signals.json"
    return Response(fp.read_text(encoding="utf-8"), mimetype="application/json")


@app.route("/api/universe", methods=["GET"])
def api_universe_get():
    fp = DATA / "universe.json"
    if not fp.exists():
        return jsonify({"error": "universe.json not found"}), 404
    return Response(fp.read_text(encoding="utf-8"), mimetype="application/json")


@app.route("/api/universe", methods=["POST"])
def api_universe_post():
    try:
        data = request.get_json(force=True)
        if not isinstance(data, list):
            return jsonify({"error": "Expected a JSON array"}), 400
        fp = DATA / "universe.json"
        fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return jsonify({"status": "ok", "count": len(data)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port",    type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host",    default="0.0.0.0")
    ap.add_argument("--no-auto", action="store_true")
    ap.add_argument("--mock",    action="store_true")
    args = ap.parse_args()

    if not args.no_auto and not _IS_RENDER:
        t = threading.Thread(target=background_refresh, args=(args.mock,), daemon=True)
        t.start()
        print("Background refresher started (local mode).")

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
