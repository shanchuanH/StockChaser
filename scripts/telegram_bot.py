"""Pure command handlers for the StockChaser Telegram bot.

This module is intentionally side-effect-free: it reads signals.json,
parses incoming command text, and returns a dict of {text, reply_markup}.
serve.py is responsible for the actual HTTP I/O (sending replies,
answering callbacks, triggering refreshes).

Telegram payload contracts:
  text         HTML-formatted (we set parse_mode=HTML on send)
  reply_markup an inline keyboard dict (Telegram sendMessage param)

Commands handled:
  /start /help            list commands
  /top [N]                top N rows by conviction (default 10, cap 25)
  /priority /pri          current is_priority list
  /ticker SYM             single ticker detail card
  /status                 service state + regime + action distribution
  /refresh                kick off async pipeline run

Inline-button callback codes (kept short to fit 64-byte data limit):
  top   /top
  pri   /priority
  sta   /status
  ref   /refresh
"""
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIGNALS = ROOT / "data" / "signals.json"

MAX_TOP = 25

CMD_HELP = (
    "<b>🤖 StockChaser Bot 指令</b>\n\n"
    "<code>/top [N]</code>     按 conviction Top N（默认 10）\n"
    "<code>/priority</code>    当前 PRIORITY 列表\n"
    "<code>/ticker SYM</code>  某个 ticker 的详情\n"
    "<code>/status</code>      服务状态 + regime\n"
    "<code>/refresh</code>     手动触发后台刷新\n"
    "<code>/help</code>        显示本帮助\n\n"
    "<i>也可以点下面的按钮直接操作。</i>"
)

ACTION_TAGS = {
    "STRONG_BUY": ("🔥", "强力买入"),
    "BUY":        ("🟢", "买入"),
    "TRY_BUY":    ("🟡", "试仓"),
    "WATCH":      ("👀", "观察"),
    "AVOID":      ("⚪", "回避"),
}


# ---------- formatting helpers ----------

def _fmt_pct(v, digits=2, signed=True):
    if v is None:
        return "-"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    sign = "+" if (signed and v > 0) else ""
    return f"{sign}{v:.{digits}f}%"


def _fmt_price(v):
    if v is None:
        return "-"
    try:
        return f"${float(v):.2f}"
    except (TypeError, ValueError):
        return "-"


def _action_tag(action):
    e, lbl = ACTION_TAGS.get(action, ("·", action or "?"))
    return f"{e} {lbl}"


def _header(d):
    regime = d.get("regime_label", "")
    rm = d.get("regime_modifier")
    gen = d.get("generated_at", "")
    return f"<i>{gen}  ·  {regime} ×{rm}</i>"


def _load_signals():
    if not SIGNALS.exists():
        return None
    try:
        return json.loads(SIGNALS.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------- views ----------

def format_top(d, n=10):
    if not d:
        return "⏳ 数据尚未就绪，请稍候再试。"
    rows = d.get("rows", [])[:n]
    if not rows:
        return "暂无数据"
    lines = [f"<b>📊 Top {len(rows)}（按置信度）</b>", _header(d), ""]
    for i, r in enumerate(rows, 1):
        emoji, _ = ACTION_TAGS.get(r.get("action"), ("·", ""))
        pri = f"🔥#{r['priority_rank']} " if r.get("is_priority") else ""
        lines.append(
            f"<code>{i:>2}.</code> {pri}<b>{r.get('ticker','?')}</b> {emoji} "
            f"Conv <b>{r.get('conviction_score','-')}</b>  ·  "
            f"{_fmt_price(r.get('latest_price'))}  ·  "
            f"周 {_fmt_pct(r.get('week_return_pct'), 1)}"
        )
    return "\n".join(lines)


def format_priority(d):
    if not d:
        return "⏳ 数据尚未就绪。"
    priority = [r for r in d.get("rows", []) if r.get("is_priority")]
    priority.sort(key=lambda r: r.get("priority_rank") or 999)
    if not priority:
        return "当前无 PRIORITY 标的（regime 偏防御 或 信号不足）。"
    lines = [f"<b>🔥 PRIORITY 列表（{len(priority)}）</b>", _header(d), ""]
    for r in priority:
        emoji, _ = ACTION_TAGS.get(r.get("action"), ("·", ""))
        lines.append(
            f"  #{r.get('priority_rank')}  <b>{r.get('ticker')}</b> {emoji}  "
            f"Conv {r.get('conviction_score')}  ·  {r.get('layer','')}"
        )
    return "\n".join(lines)


def format_ticker(d, sym):
    if not d:
        return "⏳ 数据尚未就绪。"
    sym = (sym or "").upper().strip()
    if not sym:
        return "用法：<code>/ticker NVDA</code>"
    for r in d.get("rows", []):
        if r.get("ticker") == sym:
            return _format_ticker_card(d, r)
    return f"找不到 <code>{sym}</code>。试 /top 看在跟踪的列表。"


def _format_ticker_card(d, r):
    stars = "★" * int(r.get("potential") or 0)
    sig_meta = d.get("signal_meta", {})
    sig_strs = []
    for code in r.get("signals", []):
        m = sig_meta.get(code, {})
        sig_strs.append(f"{m.get('emoji','')}{m.get('label', code)}")
    pri_tag = f"🔥 <b>PRIORITY #{r['priority_rank']}</b>\n" if r.get("is_priority") else ""
    lines = [
        f"<b>🔍 {r.get('ticker')}</b>  ·  {r.get('layer','')}  ·  {r.get('role','')} {stars}",
        _header(d),
        "",
        pri_tag + f"Conv <b>{r.get('conviction_score')}</b>  ·  <b>{_action_tag(r.get('action'))}</b>",
        f"  TS {r.get('trend_strength')}  ·  EQ {r.get('entry_quality')}  ·  FC {r.get('fund_conviction')}",
        "",
        f"价 {_fmt_price(r.get('latest_price'))}   "
        f"周 {_fmt_pct(r.get('week_return_pct'), 2)}   "
        f"月 {_fmt_pct(r.get('month_return_pct'), 2)}   "
        f"3月 {_fmt_pct(r.get('m3_return_pct'), 2)}",
        f"距4W高 {_fmt_pct(r.get('dist_4w_high_pct'), 2)}   "
        f"RS vs SPY {_fmt_pct(r.get('rs_vs_spy_4w_pp'), 2, signed=True).replace('%','pp')}",
        f"ATR(20) {_fmt_price(r.get('atr_20'))} ({_fmt_pct(r.get('atr_pct'), 1, signed=False)} 日波动)",
        "",
        f"仓位 <b>{r.get('size_class','-')} {r.get('size_pct_recommended','-')}%</b>"
        f"  ·  止损 {_fmt_price(r.get('suggested_stop_price'))}"
        f"  ·  目标 {_fmt_price(r.get('suggested_target_price'))}",
        f"周期 {r.get('holding_horizon','-')} ({r.get('horizon_days','-')})",
    ]
    if sig_strs:
        lines += ["", "信号  " + "  ".join(sig_strs)]
    if r.get("logic"):
        lines += ["", f"<i>逻辑：{r['logic']}</i>"]
    return "\n".join(lines)


def format_status(d, state):
    state = state or {}
    if not d:
        return (
            "<b>✅ 服务在线</b>\n"
            "<i>但数据尚未就绪（首次 pipeline 仍在跑）。</i>"
        )
    counts = {}
    for r in d.get("rows", []):
        a = r.get("action", "?")
        counts[a] = counts.get(a, 0) + 1
    counts_str = "  ·  ".join(
        f"{ACTION_TAGS.get(k, ('·',k))[0]} {k} {v}"
        for k, v in counts.items()
    )
    n_pri = sum(1 for r in d.get("rows", []) if r.get("is_priority"))
    lines = [
        "<b>✅ Live</b>",
        _header(d),
        "",
        f"上次刷新: <code>{state.get('last_refresh','-')}</code>",
        f"下次预计: <code>{state.get('next_refresh','-')}</code>",
        f"刷新间隔: 每 <b>{state.get('interval_min','-')}</b> 分钟",
        f"状态: <code>{state.get('last_status','-')}</code>",
        "",
        f"共 <b>{len(d.get('rows', []))}</b> 行  ·  PRIORITY <b>{n_pri}</b>",
        f"Action: {counts_str}",
    ]
    return "\n".join(lines)


# ---------- inline keyboard ----------

def default_keyboard():
    return {
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


# ---------- dispatch ----------

def dispatch(text, state):
    """Parse text command. Returns {text, reply_markup, side_effect} or None."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split(None, 1)
    cmd = parts[0].lstrip("/").split("@", 1)[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    d = _load_signals()

    if cmd in ("start", "help"):
        return {"text": CMD_HELP, "reply_markup": default_keyboard()}

    if cmd == "top":
        n = 10
        if args:
            try:
                n = max(1, min(MAX_TOP, int(args.split()[0])))
            except ValueError:
                pass
        return {"text": format_top(d, n), "reply_markup": default_keyboard()}

    if cmd in ("priority", "pri"):
        return {"text": format_priority(d), "reply_markup": default_keyboard()}

    if cmd in ("ticker", "tk", "t"):
        if not args:
            return {"text": "用法：<code>/ticker NVDA</code>"}
        return {"text": format_ticker(d, args.split()[0]),
                "reply_markup": default_keyboard()}

    if cmd == "status":
        return {"text": format_status(d, state), "reply_markup": default_keyboard()}

    if cmd == "refresh":
        if (state or {}).get("refreshing"):
            return {"text": "⏳ 已经在刷新中，请等几十秒。"}
        return {"text": "⏳ 已触发后台刷新，约 30–90s 后会有新数据。"
                        "\n下次 priority 变化会自动推送。",
                "side_effect": "refresh"}

    return None


CALLBACK_TO_CMD = {"top": "/top", "pri": "/priority", "sta": "/status", "ref": "/refresh"}


def dispatch_callback(data, state):
    cmd = CALLBACK_TO_CMD.get(data)
    if not cmd:
        return None
    return dispatch(cmd, state)
