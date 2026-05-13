from __future__ import annotations
import json
import os
import sys
import time
import argparse
import threading
import subprocess
import urllib.error
import urllib.parse
import urllib.request
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
SEED = ROOT / "data_seed"

sys.path.insert(0, str(SCRIPTS))
try:
    import telegram_bot
except ImportError:
    telegram_bot = None  # type: ignore[assignment]

DATA.mkdir(exist_ok=True)


SEED_ALWAYS_REFRESH = {
    # Regenerated locally (e.g. by bt_v36.py) and pushed via git.
    # Render's persistent disk caches the old copy → must overwrite when
    # the seed file in git is newer, otherwise the dashboard freezes on
    # the version from the first deploy.
    "backtest_results.json",
    "conviction_history.json",
    # Universe config — when we add/remove AI-chain tickers (HIMX, DRAM…),
    # the new seed in git must override the cached disk copy. This is pure
    # config, never user-edited on the server.
    "universe.json",
}

# Fields we reconcile from seed → disk on every cold start (preserves
# user-edited fields like shares/buy_price/buy_date/batches).
# Used by SEED_MERGE_NEW_KEYS handler to handle "promote ETF to AI chain"
# (= remove category) and similar metadata flips.
SEED_RECONCILE_FIELDS = {"category", "name"}

# Files that should MERGE new entries from seed but never overwrite
# existing user-edited entries on disk.
# Example: data_seed/my_holdings.json gains new ETF positions → we want
# to add them to disk-side without trampling user's ✏️ edits on
# existing positions.
SEED_MERGE_NEW_KEYS = {
    "my_holdings.json",
}


def _merge_dict_new_keys(seed_path, disk_path):
    """Reconcile seed JSON → disk JSON.

    Two operations on every cold start:
      (a) Add keys from seed that don't exist on disk (e.g. new positions).
      (b) For keys that DO exist, sync only the fields listed in
          SEED_RECONCILE_FIELDS ({category, name}) — so a "promote ETF to
          AI chain" change in git (= seed removes category) actually
          propagates to the persistent disk.

    Never touches user-edited fields like shares, buy_price, buy_date,
    batches — those stay exactly as the user last edited them.
    """
    import shutil
    try:
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        if not isinstance(seed, dict):
            return False, "seed is not a dict"
        if not disk_path.exists():
            shutil.copy2(seed_path, disk_path)
            return True, f"seeded {len(seed)} entries"
        disk = json.loads(disk_path.read_text(encoding="utf-8"))
        if not isinstance(disk, dict):
            return False, "disk is not a dict"

        added = [k for k in seed if k not in disk]
        for k in added:
            disk[k] = seed[k]

        # (b) Reconcile config-like fields on EXISTING entries.
        reconciled = []
        for k in seed:
            if k not in disk or k in added:
                continue
            if not isinstance(disk[k], dict) or not isinstance(seed[k], dict):
                continue
            changed = []
            for field in SEED_RECONCILE_FIELDS:
                seed_has = field in seed[k]
                disk_has = field in disk[k]
                if seed_has and not disk_has:
                    disk[k][field] = seed[k][field]
                    changed.append(f"+{field}")
                elif disk_has and not seed_has:
                    # Seed removed this field → propagate the removal
                    # (e.g. category dropped to promote ETF to AI chain)
                    del disk[k][field]
                    changed.append(f"-{field}")
                elif seed_has and disk_has and seed[k][field] != disk[k][field]:
                    disk[k][field] = seed[k][field]
                    changed.append(f"~{field}")
            if changed:
                reconciled.append(f"{k}({','.join(changed)})")

        if not added and not reconciled:
            return False, "no new keys, no reconciliation needed"

        disk_path.write_text(
            json.dumps(disk, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        parts = []
        if added:
            parts.append(f"+{len(added)} new: {', '.join(added)}")
        if reconciled:
            parts.append(f"reconcile: {', '.join(reconciled)}")
        return True, "; ".join(parts)
    except Exception as exc:
        return False, str(exc)


def _seed_data_dir():
    """Bootstrap data/ from data_seed/ on container start.

    Render mounts an empty persistent disk over data/, which hides any
    files baked into the git build. We seed canonical files from
    data_seed/ which lives outside the mount.

    Three flavors:
      - always-refresh (backtest_results.json, conviction_history.json):
        overwrite when seed mtime is newer than on-disk so new local
        runs reach Render.
      - merge-new-keys (my_holdings.json): add entries from seed that
        don't exist on disk; leave user-edited entries alone.
      - seed-if-absent (universe.json, portfolio.json): copy only if
        absent so server-side edits survive across deploys.
    """
    if not SEED.exists():
        return
    import shutil
    for src in SEED.iterdir():
        if not src.is_file():
            continue
        dst = DATA / src.name
        try:
            if src.name in SEED_ALWAYS_REFRESH:
                if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                    shutil.copy2(src, dst)
                    print(f"refreshed {dst.name} from data_seed/ (always-refresh)")
            elif src.name in SEED_MERGE_NEW_KEYS:
                ok, msg = _merge_dict_new_keys(src, dst)
                if ok:
                    print(f"merge {dst.name}: {msg}")
            elif not dst.exists():
                shutil.copy2(src, dst)
                print(f"seeded {dst.name} from data_seed/")
        except Exception as exc:
            print(f"seed copy failed for {src.name}: {exc}")


_seed_data_dir()

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
    steps = [SCRIPTS / fetch_script, SCRIPTS / "engine_v35.py"]
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

    # Run alert detector — non-fatal if it errors out
    try:
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "alerts.py")],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            print("alerts.py warning: " + r.stderr[:200])
    except Exception as exc:
        print("alerts.py error: " + str(exc))

    # Dynamic stop-loss ratchet — monotonic upward as浮盈 hits +10/+20/+30 milestones
    try:
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "dynamic_stops.py")],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            print("dynamic_stops.py warning: " + r.stderr[:200])
    except Exception as exc:
        print("dynamic_stops.py error: " + str(exc))

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
    # Webhook registration is deferred to after all helper functions are
    # defined further down — see the bottom of the file.


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


# ===== Portfolio Holdings API (server-side persistence) =====
HOLDINGS_FILE = DATA / "my_holdings.json"
_holdings_lock = threading.Lock()


def _load_holdings():
    if not HOLDINGS_FILE.exists():
        return {}
    try:
        return json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_holdings(d):
    HOLDINGS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


@app.route("/api/holdings", methods=["GET"])
def api_holdings_get():
    with _holdings_lock:
        return jsonify(_load_holdings())


@app.route("/api/holdings/upsert", methods=["POST"])
def api_holdings_upsert():
    body = request.get_json(force=True, silent=True) or {}
    ticker = (body.get("ticker") or "").upper()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    shares = float(body.get("shares") or 0)
    buy_price = float(body.get("buy_price") or 0)
    buy_date = body.get("buy_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Optional entry step from body — which strategic batch this purchase
    # satisfies (step_1 / step_2 / step_3 / dca / re_entry / manual)
    step = (body.get("step") or "").lower() or None
    VALID_STEPS = {"step_1", "step_2", "step_3", "dca", "re_entry", "manual"}
    if step and step not in VALID_STEPS:
        step = "manual"

    with _holdings_lock:
        pf = _load_holdings()
        if ticker in pf:
            # add batch — recalc weighted avg
            old = pf[ticker]
            total = old["shares"] + shares
            new_avg = (old["buy_price"] * old["shares"] + buy_price * shares) / total if total > 0 else buy_price
            old["shares"] = total
            old["buy_price"] = round(new_avg, 2)
            # Default to dca if user didn't specify (most adds are dollar-cost avg, not ladder)
            effective_step = step or "dca"
            old.setdefault("batches", []).append({
                "price": buy_price, "shares": shares, "date": buy_date, "step": effective_step,
            })
            # Track peak shares (max ever held) for tier-aware profit taking.
            old["peak_shares"] = max(old.get("peak_shares", 0) or 0, total)
            # Track explicit entry steps (parallel to tiers_executed)
            if effective_step in ("step_1", "step_2", "step_3"):
                ese = old.setdefault("entry_steps_executed", [])
                # de-dup by step id; if already in list, just record event in actions
                if effective_step not in [e.get("step") for e in ese if isinstance(e, dict)] \
                        and effective_step not in ese:
                    ese.append({
                        "step": effective_step, "date": buy_date,
                        "shares": shares, "price": buy_price,
                    })
            # Append to actions log for full audit trail
            old.setdefault("actions", []).append({
                "t": "add", "step": effective_step, "shares": shares, "price": buy_price,
                "date": buy_date, "after_shares": total,
            })
        else:
            # First purchase = step_1 unless explicitly told otherwise
            first_step = step if step in ("step_1", "manual", "re_entry") else "step_1"
            pf[ticker] = {
                "ticker": ticker, "shares": shares, "buy_price": buy_price, "buy_date": buy_date,
                "batches": [{"price": buy_price, "shares": shares, "date": buy_date, "step": first_step}],
                "peak_shares": shares,
                "entry_steps_executed": (
                    [{"step": "step_1", "date": buy_date, "shares": shares, "price": buy_price}]
                    if first_step == "step_1" else []
                ),
                "step_1_price": buy_price,  # anchor for step_2/3 thresholds (don't drift with playbook regens)
                "actions": [{
                    "t": "buy", "step": first_step, "shares": shares, "price": buy_price,
                    "date": buy_date, "after_shares": shares,
                }],
            }
        _save_holdings(pf)
        return jsonify({"ok": True, "holding": pf[ticker]})


VALID_TIERS = {"tier_1", "tier_2", "tier_3", "stop_loss",
               "conv_break", "flash_8", "manual"}


@app.route("/api/holdings/update_shares", methods=["POST"])
def api_holdings_update_shares():
    """Reduce (or fully close) a holding.

    Body:
      ticker      str    required
      shares      float  required — NEW total shares (set to 0 → close out)
      reason      str    optional — "tier_1" / "tier_2" / "tier_3" /
                                    "stop_loss" / "conv_break" / "flash_8" / "manual"
      price       float  optional — sale price (for actions log)

    If reason is in tier_1..tier_3, append to tiers_executed[] for tier-aware
    UI. Always append to actions[] for audit log.
    """
    body = request.get_json(force=True, silent=True) or {}
    ticker = (body.get("ticker") or "").upper()
    new_shares = float(body.get("shares") or 0)
    reason = body.get("reason") or "manual"
    if reason not in VALID_TIERS:
        reason = "manual"
    price = body.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with _holdings_lock:
        pf = _load_holdings()
        if ticker not in pf:
            return jsonify({"error": "not held"}), 404
        h = pf[ticker]
        old_shares = float(h.get("shares") or 0)
        shares_sold = max(0.0, old_shares - new_shares)

        # Append to actions log
        h.setdefault("actions", []).append({
            "t": "close" if new_shares <= 0 else "reduce",
            "shares": shares_sold,
            "price": price,
            "reason": reason,
            "date": today,
            "before_shares": old_shares,
            "after_shares": new_shares,
        })

        # Mark tier executed (if applicable)
        if reason in ("tier_1", "tier_2", "tier_3"):
            te = h.setdefault("tiers_executed", [])
            if reason not in te:
                te.append(reason)

        if new_shares <= 0:
            del pf[ticker]
        else:
            h["shares"] = new_shares
        _save_holdings(pf)
        return jsonify({
            "ok": True,
            "shares_sold": shares_sold,
            "reason": reason,
            "remaining": new_shares,
        })


@app.route("/api/portfolio_config", methods=["GET", "POST"])
def api_portfolio_config():
    """Read or update data/portfolio.json (cash / max positions / risk%)."""
    fp = DATA / "portfolio.json"
    if request.method == "GET":
        if not fp.exists():
            return jsonify({})
        try:
            return jsonify(json.loads(fp.read_text(encoding="utf-8")))
        except Exception:
            return jsonify({})
    body = request.get_json(force=True, silent=True) or {}
    with _holdings_lock:
        cfg = {}
        if fp.exists():
            try:
                cfg = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                cfg = {}
        for k in ("portfolio_cash_usd", "account_total_usd",
                  "max_positions", "risk_per_trade_pct",
                  "max_position_size_pct"):
            if k in body:
                try:
                    cfg[k] = float(body[k]) if isinstance(body[k], (int, float, str)) else body[k]
                except (TypeError, ValueError):
                    pass
        fp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return jsonify({"ok": True, "config": cfg})


@app.route("/api/conviction_history")
def api_conviction_history():
    fp = DATA / "conviction_history.json"
    if not fp.exists():
        return jsonify({})
    return Response(fp.read_text(encoding="utf-8"), mimetype="application/json")


# ===== Pending alerts (persistent trigger events) =====
@app.route("/api/alerts", methods=["GET"])
def api_alerts():
    """Return active (un-dismissed, un-snoozed) alerts."""
    try:
        from alerts import active_alerts
        return jsonify({"alerts": active_alerts()})
    except Exception as exc:
        return jsonify({"error": str(exc), "alerts": []}), 500


@app.route("/api/alerts/dismiss", methods=["POST"])
def api_alerts_dismiss():
    """Dismiss or snooze a specific alert.
    body: {ticker, id, snooze_hours?}
    """
    body = request.get_json(force=True, silent=True) or {}
    ticker = (body.get("ticker") or "").upper()
    alert_id = body.get("id")
    snooze_hours = body.get("snooze_hours")
    try:
        from alerts import dismiss
        ok = dismiss(ticker, alert_id, snooze_hours)
        return jsonify({"ok": bool(ok)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/alerts/scan", methods=["POST"])
def api_alerts_scan():
    """Manually trigger alert detection (e.g. from dashboard button)."""
    try:
        from alerts import detect_and_persist, active_alerts
        n_new = detect_and_persist()
        return jsonify({"ok": True, "new": n_new, "active": active_alerts()})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/holdings/export", methods=["GET"])
def api_holdings_export():
    """Download holdings as JSON file (backup)."""
    pf = _load_holdings()
    body = json.dumps(pf, ensure_ascii=False, indent=2)
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="my_holdings_backup.json"'}
    )


@app.route("/api/holdings/import", methods=["POST"])
def api_holdings_import():
    """Replace holdings with uploaded JSON (restore from backup)."""
    body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "expected dict {ticker: {...}}"}), 400
    with _holdings_lock:
        _save_holdings(body)
    return jsonify({"ok": True, "count": len(body)})


@app.route("/api/holdings/edit", methods=["POST"])
def api_holdings_edit():
    """Edit individual fields of an existing holding (fix data errors)."""
    body = request.get_json(force=True, silent=True) or {}
    ticker = (body.get("ticker") or "").upper()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    with _holdings_lock:
        pf = _load_holdings()
        if ticker not in pf:
            return jsonify({"error": "not held"}), 404
        h = pf[ticker]
        if "buy_price" in body:
            try: h["buy_price"] = round(float(body["buy_price"]), 4)
            except (TypeError, ValueError): pass
        if "shares" in body:
            try: h["shares"] = float(body["shares"])
            except (TypeError, ValueError): pass
        if "buy_date" in body:
            h["buy_date"] = str(body["buy_date"])[:10]
        _save_holdings(pf)
        return jsonify({"ok": True, "holding": h})


@app.route("/api/holdings/remove", methods=["POST", "DELETE"])
def api_holdings_remove():
    body = request.get_json(force=True, silent=True) or {}
    ticker = (body.get("ticker") or "").upper()
    with _holdings_lock:
        pf = _load_holdings()
        if ticker in pf:
            del pf[ticker]
            _save_holdings(pf)
        return jsonify({"ok": True})


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


# ---------- Telegram interactive bot ----------

_TG_API = "https://api.telegram.org/bot{token}/{method}"


def _telegram_call(method, payload, timeout=10):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return False
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        _TG_API.format(token=token, method=method),
        data=data, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 300
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"telegram {method} failed: {exc}")
        return False


def _telegram_send(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return _telegram_call("sendMessage", payload, timeout=10)


def _telegram_answer_callback(cb_id):
    if not cb_id:
        return
    _telegram_call("answerCallbackQuery", {"callback_query_id": cb_id}, timeout=5)


def _trigger_async_refresh():
    """Fire-and-forget pipeline run (used by /refresh button & command)."""
    with _lock:
        if _state["refreshing"]:
            return False
        _state["refreshing"] = True
    threading.Thread(target=_do_refresh, args=(_USE_MOCK,), daemon=True).start()
    return True


def _state_snapshot():
    with _lock:
        return dict(_state)


def _authorized(chat_id, from_id):
    """Only the chat_id we were configured for can drive the bot."""
    authed = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not authed:
        return False
    return str(chat_id) == authed or str(from_id) == authed


@app.route("/api/telegram/webhook", methods=["POST"])
def api_telegram_webhook():
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if expected_secret:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != expected_secret:
            return jsonify({"error": "unauthorized"}), 401

    if telegram_bot is None:
        return jsonify({"ok": True})

    update = request.get_json(silent=True) or {}

    callback = update.get("callback_query")
    if callback:
        msg = callback.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        from_id = (callback.get("from") or {}).get("id")
        _telegram_answer_callback(callback.get("id"))
        if not _authorized(chat_id, from_id):
            return jsonify({"ok": True})
        try:
            result = telegram_bot.dispatch_callback(callback.get("data", ""), _state_snapshot())
        except Exception as exc:
            print(f"telegram dispatch_callback error: {exc}")
            return jsonify({"ok": True})
        if result:
            if result.get("side_effect") == "refresh":
                _trigger_async_refresh()
            _telegram_send(chat_id, result["text"], result.get("reply_markup"))
        return jsonify({"ok": True})

    msg = update.get("message") or update.get("edited_message")
    if msg:
        chat_id = (msg.get("chat") or {}).get("id")
        from_id = (msg.get("from") or {}).get("id")
        text = msg.get("text", "")
        if not _authorized(chat_id, from_id):
            return jsonify({"ok": True})
        try:
            result = telegram_bot.dispatch(text, _state_snapshot())
        except Exception as exc:
            print(f"telegram dispatch error: {exc}")
            return jsonify({"ok": True})
        if result:
            if result.get("side_effect") == "refresh":
                _trigger_async_refresh()
            _telegram_send(chat_id, result["text"], result.get("reply_markup"))
        return jsonify({"ok": True})

    return jsonify({"ok": True})


def _register_telegram_webhook():
    """Auto-register webhook with Telegram on Render boot."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    base = (os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("BASE_URL", "")).strip()
    if not token or not base:
        print("telegram webhook: skipped (no token or external URL)")
        return
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    url = base.rstrip("/") + "/api/telegram/webhook"
    payload = {
        "url": url,
        "drop_pending_updates": "true",
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if secret:
        payload["secret_token"] = secret
    try:
        ok = _telegram_call("setWebhook", payload, timeout=10)
        print(f"telegram setWebhook → {url} ({'ok' if ok else 'failed'})")
    except Exception as exc:
        print(f"telegram setWebhook error: {exc}")


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


# All helper functions are now defined; safe to register the webhook.
if _IS_RENDER:
    _register_telegram_webhook()


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
