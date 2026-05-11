"""Telegram notifier for StockChaser PRIORITY changes.

Reads data/signals.json, diffs the priority set (is_priority=True) against
data/priority_snapshot.json, and pushes a message to Telegram when the
membership or ranking changes. Writes the new snapshot on success.

Env vars (silently no-op when missing — safe to run anywhere):
  TELEGRAM_BOT_TOKEN   bot token from @BotFather
  TELEGRAM_CHAT_ID     target chat / channel id (numeric string)
  TELEGRAM_NOTIFY_ON_START
                       if "1", also send a message on cold start
                       (when no snapshot exists). Default: silent first run
                       so Render restarts don't spam.

Designed to never crash the pipeline:
  - returns exit 0 on any handled failure
  - prints diagnostic info to stdout
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

try:
    import urllib.request
    import urllib.parse
    import urllib.error
except ImportError:
    print("notify_telegram: urllib unavailable; skipping")
    sys.exit(0)

ROOT = Path(__file__).resolve().parents[1]
SIGNALS = ROOT / "data" / "signals.json"
SNAPSHOT = ROOT / "data" / "priority_snapshot.json"

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
HTTP_TIMEOUT = 10


_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🔥 PRIORITY", "callback_data": "pri"},
            {"text": "📊 Top 10",   "callback_data": "top"},
        ],
        [
            {"text": "📡 状态",     "callback_data": "sta"},
            {"text": "🔄 手动刷新", "callback_data": "ref"},
        ],
    ]
}


def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = TELEGRAM_API.format(token=token)
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "reply_markup": json.dumps(_KEYBOARD, ensure_ascii=False),
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 300:
                print(f"notify_telegram: HTTP {resp.status}: {body[:200]}")
                return False
            return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        print(f"notify_telegram: HTTPError {exc.code}: {body[:200]}")
        return False
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"notify_telegram: network error: {exc}")
        return False


def _load_priority_from_signals():
    if not SIGNALS.exists():
        return None, None
    try:
        d = json.loads(SIGNALS.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"notify_telegram: bad signals.json: {exc}")
        return None, None
    priority = [r for r in d.get("rows", []) if r.get("is_priority")]
    priority.sort(key=lambda r: r.get("priority_rank") or 999)
    return priority, d


def _load_snapshot():
    if not SNAPSHOT.exists():
        return None
    try:
        return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_snapshot(priority, generated_at):
    payload = {
        "generated_at": generated_at,
        "priority": [
            {"ticker": r["ticker"], "priority_rank": r.get("priority_rank"),
             "conviction_score": r.get("conviction_score"),
             "action": r.get("action")}
            for r in priority
        ],
    }
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")


def _diff(prev_list, curr_list):
    """Return (added, removed, rank_changed) lists of ticker strings.

    rank_changed entries: (ticker, old_rank, new_rank)
    """
    prev_map = {p["ticker"]: p for p in (prev_list or [])}
    curr_map = {p["ticker"]: p for p in curr_list}
    added = [t for t in curr_map if t not in prev_map]
    removed = [t for t in prev_map if t not in curr_map]
    rank_changed = []
    for t, cur in curr_map.items():
        if t in prev_map:
            old_r = prev_map[t].get("priority_rank")
            new_r = cur.get("priority_rank")
            if old_r != new_r:
                rank_changed.append((t, old_r, new_r))
    return added, removed, rank_changed


def _format_message(signals, priority, added, removed, rank_changed):
    """Build HTML-formatted Telegram message."""
    regime = signals.get("regime_label", "")
    rm = signals.get("regime_modifier")
    gen = signals.get("generated_at", "")
    lines = [
        "<b>🔥 PRIORITY 列表更新</b>",
        f"<i>{gen}  ·  {regime} ×{rm}</i>",
        "",
    ]

    curr_map = {r["ticker"]: r for r in priority}

    if added:
        lines.append("<b>➕ 新增</b>")
        for t in added:
            r = curr_map[t]
            lines.append(
                f"  #{r.get('priority_rank')}  <b>{t}</b>"
                f"  ·  Conv <b>{r.get('conviction_score')}</b>"
                f"  ·  {r.get('layer', '')}"
            )
        lines.append("")

    if removed:
        lines.append("<b>➖ 移除</b>")
        for t in removed:
            lines.append(f"  <b>{t}</b>")
        lines.append("")

    if rank_changed:
        lines.append("<b>🔁 排名变动</b>")
        for t, old_r, new_r in rank_changed:
            arrow = "↑" if (old_r or 999) > (new_r or 999) else "↓"
            lines.append(f"  <b>{t}</b>  #{old_r} {arrow} #{new_r}")
        lines.append("")

    lines.append("<b>📊 当前 Top</b>")
    for r in priority:
        lines.append(
            f"  #{r.get('priority_rank')}  <b>{r['ticker']}</b>"
            f"  ·  Conv {r.get('conviction_score')}"
            f"  ·  {r.get('layer', '')}"
        )
    return "\n".join(lines)


def _format_initial_message(signals, priority):
    regime = signals.get("regime_label", "")
    rm = signals.get("regime_modifier")
    gen = signals.get("generated_at", "")
    lines = [
        "<b>📡 StockChaser 已上线</b>",
        f"<i>{gen}  ·  {regime} ×{rm}</i>",
        "",
        "<b>当前 PRIORITY</b>",
    ]
    for r in priority:
        lines.append(
            f"  #{r.get('priority_rank')}  <b>{r['ticker']}</b>"
            f"  ·  Conv {r.get('conviction_score')}"
            f"  ·  {r.get('layer', '')}"
        )
    return "\n".join(lines)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("notify_telegram: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, skipping")
        return 0

    priority, signals = _load_priority_from_signals()
    if priority is None:
        print("notify_telegram: no signals.json available")
        return 0

    prev = _load_snapshot()
    notify_on_start = os.environ.get("TELEGRAM_NOTIFY_ON_START") == "1"

    if prev is None:
        if notify_on_start and priority:
            msg = _format_initial_message(signals, priority)
            if _send_telegram(token, chat_id, msg):
                print(f"notify_telegram: sent initial message ({len(priority)} priority)")
        else:
            print("notify_telegram: first run, snapshot seeded silently")
        _write_snapshot(priority, signals.get("generated_at", ""))
        return 0

    added, removed, rank_changed = _diff(prev.get("priority", []), priority)
    if not (added or removed or rank_changed):
        print("notify_telegram: no priority change")
        return 0

    msg = _format_message(signals, priority, added, removed, rank_changed)
    ok = _send_telegram(token, chat_id, msg)
    if ok:
        print(f"notify_telegram: sent (added={len(added)} removed={len(removed)} "
              f"rank_changed={len(rank_changed)})")
        _write_snapshot(priority, signals.get("generated_at", ""))
    else:
        print("notify_telegram: send failed, snapshot not updated (will retry next run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
