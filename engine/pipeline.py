"""Main pipeline: orchestrates score → tag → priority → stops → alerts.

Reads:
  data/universe.json      ticker metadata
  data/prices.json        latest price + indicators (from fetch_data.py)
  data/portfolio.json     user cash + risk config

Writes:
  data/signals.json           ranked + tagged signals + playbooks
  data/conviction_history.json  per-ticker Conv time-series (≤ 90 entries)
  data/my_holdings.json       updated stops + decay peaks (via stops.update_all_stops)
  data/pending_alerts.json    new alert events appended (via alerts.detect_and_persist)
"""
from __future__ import annotations
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import scoring, regime, framework, action as action_mod, stops, alerts, playbook, volume_profile

ENGINE_VERSION = "v3.7"


# ── Path resolution ─────────────────────────────────────────────────────

class Paths:
    """Resolve data/ relative to repo root (parent of engine/)."""
    def __init__(self, root=None):
        self.root = Path(root) if root else Path(__file__).resolve().parents[1]
        self.data = self.root / "data"
        self.universe = self.data / "universe.json"
        self.prices = self.data / "prices.json"
        self.signals = self.data / "signals.json"
        self.holdings = self.data / "my_holdings.json"
        self.portfolio = self.data / "portfolio.json"
        self.pending = self.data / "pending_alerts.json"
        self.history_csv = self.data / "history.csv"
        self.conv_hist = self.data / "conviction_history.json"
        self.volume_profile = self.data / "volume_profile.json"
        self.priority_history = self.data / "priority_history.json"


def _load_prior_priority(signals_path):
    p = Path(signals_path)
    if not p.exists():
        return set()
    try:
        prior = json.loads(p.read_text(encoding="utf-8"))
        return {r["ticker"] for r in prior.get("rows", []) if r.get("is_priority")}
    except Exception:
        return set()


def _compute_cash_available(holdings_path, portfolio_config):
    """cash_available = account_total - sum(buy_price × shares) for all holdings."""
    p = Path(holdings_path)
    total_invested = 0
    if p.exists():
        try:
            h = json.loads(p.read_text(encoding="utf-8"))
            total_invested = sum(
                (pos.get("buy_price", 0) or 0) * (pos.get("shares", 0) or 0)
                for pos in h.values()
            )
        except Exception:
            pass
    account_total = (portfolio_config.get("account_total_usd")
                     or portfolio_config.get("portfolio_cash_usd") or 20000)
    cash_available = max(0, account_total - total_invested)
    return round(cash_available, 2), round(total_invested, 2), round(account_total, 2)


# ── Per-row scoring ─────────────────────────────────────────────────────

def _score_row(u, p, rm, signals_meta):
    """Compute one row's full signal+score+action data."""
    flags = scoring.detect_signals(p)
    signal_bonus = sum(signals_meta[f]["weight"] for f in flags)
    comp, m_score, f_score = scoring.momentum_composite(p, signal_bonus, u)

    ts = round(scoring.trend_strength(p), 1)
    eq = round(scoring.entry_quality(p), 1)

    # Preliminary conviction for framework heuristic
    ds_bonus = framework.downstream_bonus(u)
    nn_bonus = framework.new_node_bonus(u)
    fc_prelim = (u.get("potential", 0) or 0) * 12 + {"龙头": 12, "二线": 6}.get(u.get("role"), 0)
    prelim_conv = (ts * 0.4 + eq * 0.3 + fc_prelim * 0.3) * rm
    framework_tags = framework.detect_framework_tags(u, p, comp, prelim_conv)

    fc = round(scoring.fundamental_conviction(u, comp, ds_bonus, nn_bonus), 1)
    conv = scoring.conviction_score(ts, eq, fc, rm)

    act = action_mod.map_action(conv, ts, eq, fc)
    horizon = action_mod.map_horizon(p)
    pos = action_mod.position_advice(p, act)

    return {
        **u, **p,
        "signals": flags,
        "momentum_score": m_score,
        "fundamental_score": f_score,
        "composite_score": comp,
        "trend_strength": ts,
        "entry_quality": eq,
        "fund_conviction": fc,
        "regime_modifier": rm,
        "conviction_score": conv,
        "action": act,
        "size_class": action_mod.ACTION_META[act]["size_class"],
        "size_pct_recommended": pos["size_pct_recommended"],
        "suggested_stop_price": pos["stop_price"],
        "suggested_target_price": pos["target_price"],
        "n_shares_at_10k": pos["n_shares_at_10k"],
        "holding_horizon": horizon,
        "horizon_days": action_mod.HORIZON_META[horizon]["days"],
        "framework_tags": framework_tags,
        "is_priority": False,
        "priority_rank": None,
    }


# ── Main run ────────────────────────────────────────────────────────────

def run(paths=None, *, run_stops=True, run_alerts=True, run_volume_profile=True, verbose=True):
    """Full pipeline. Returns the signals dict that was written to disk."""
    paths = paths or Paths()
    universe = json.loads(paths.universe.read_text(encoding="utf-8"))
    prices = json.loads(paths.prices.read_text(encoding="utf-8"))
    pdata = prices.get("tickers", {})
    rm = regime.regime_modifier(prices)

    # 1) Per-row scoring
    rows = []
    for u in universe:
        p = pdata.get(u["ticker"])
        if not p:
            continue
        rows.append(_score_row(u, p, rm, scoring.SIGNAL_META))

    rows.sort(key=lambda x: x["conviction_score"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i

    # 2) Per-layer cap + priority ranking with hysteresis + min-tenure
    rows = action_mod.apply_per_layer_cap(rows)
    prev_priority = _load_prior_priority(paths.signals)
    # Load priority_history (per-ticker entry date for min-tenure lock)
    try:
        priority_history = json.loads(paths.priority_history.read_text(encoding="utf-8")) \
                           if paths.priority_history.exists() else {}
    except Exception:
        priority_history = {}
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = action_mod.apply_priority_rank(rows, prev_priority,
                                          priority_history=priority_history,
                                          today_str=today_str)
    # Persist updated priority_history
    paths.priority_history.write_text(
        json.dumps(priority_history, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3) Cash-aware playbook
    pb_config = playbook.load_config(paths.portfolio)
    cash_avail, total_inv, account_total = _compute_cash_available(paths.holdings, pb_config)
    pb_config["_cash_available_usd"] = cash_avail
    pb_config["_total_invested_usd"] = total_inv
    pb_config["_account_total_usd"] = account_total
    playbook.annotate_rows(rows, pb_config)

    # 4) Assemble signals.json
    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine_version": ENGINE_VERSION,
        "spy_4w_return_pct": prices.get("spy_4w_return_pct"),
        "spy_above_sma50": prices.get("spy_above_sma50"),
        "spy_above_sma200": prices.get("spy_above_sma200"),
        "vix_close": prices.get("vix_close"),
        "vix_4w_change_pct": prices.get("vix_4w_change_pct"),
        "vix_percentile_1y": prices.get("vix_percentile_1y"),
        "vix_zone": regime.vix_zone(prices.get("vix_close")),
        "regime_modifier": rm,
        "regime_label": regime.regime_label(rm),
        "cash_available_usd": cash_avail,
        "total_invested_usd": total_inv,
        "account_total_usd": account_total,
        "is_mock": prices.get("is_mock", False),
        "extra_prices": prices.get("extra_prices", {}),
        "signal_meta": scoring.SIGNAL_META,
        "action_meta": action_mod.ACTION_META,
        "horizon_meta": action_mod.HORIZON_META,
        "framework_meta": framework.FRAMEWORK_META,
        "thresholds": {
            "STRONG_BUY": 82, "BUY": 70, "TRY_BUY": 55, "WATCH": 40,
            "strong_buy_floors": {"TS": 78},
            "max_strong_buy_per_layer": action_mod.MAX_STRONG_BUY_PER_LAYER,
            "priority_top_n": action_mod.PRIORITY_TOP_N,
            "priority_sort_key": "avg_monthly_6m_pct (DESC), tiebreak conviction",
        },
        "portfolio_config": pb_config,
        "rows": rows,
    }
    paths.signals.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # 5) Append to conviction history
    try:
        hist = json.loads(paths.conv_hist.read_text(encoding="utf-8")) if paths.conv_hist.exists() else {}
    except Exception:
        hist = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for r in rows:
        t = r["ticker"]
        series = hist.setdefault(t, [])
        entry = {"date": today, "conv": r["conviction_score"], "action": r["action"]}
        if series and series[-1].get("date") == today:
            series[-1] = entry
        else:
            series.append(entry)
        if len(series) > 90:
            hist[t] = series[-90:]
    paths.conv_hist.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")

    # 6) Dynamic stops (mutate my_holdings.json)
    if run_stops and paths.holdings.exists():
        holdings = json.loads(paths.holdings.read_text(encoding="utf-8"))
        signals_for_stops = out  # in-memory, no need to re-read
        prices_for_stops = prices
        table = stops.update_all_stops(holdings, prices_for_stops, signals_for_stops, paths.history_csv)
        paths.holdings.write_text(json.dumps(holdings, ensure_ascii=False, indent=2), encoding="utf-8")
        # Sync to seed for Render deploys
        seed = paths.root / "data_seed" / "my_holdings.json"
        if seed.exists():
            seed.write_text(json.dumps(holdings, ensure_ascii=False, indent=2), encoding="utf-8")
        changed = [r for r in table if r["changed"]]
        if verbose and changed:
            print(f"stops: updated {len(changed)} of {len(table)} anti-mart holdings")

    # 7) Alert detection
    if run_alerts:
        alerts.detect_and_persist(paths.signals, paths.holdings, paths.pending)

    # 8) Volume Profile for PRIORITY top 8 (informational, no scoring impact)
    if run_volume_profile:
        try:
            priority_tickers = [r["ticker"] for r in rows if r.get("is_priority")]
            if priority_tickers:
                profiles = volume_profile.compute_profiles(priority_tickers)
                volume_profile.save_profiles(profiles, paths.volume_profile)
                if verbose:
                    print(f"volume_profile: computed {len(profiles)}/{len(priority_tickers)} priority tickers")
        except Exception as exc:
            print(f"volume_profile error (non-fatal): {exc}")

    # 8) Console summary
    if verbose:
        actions = Counter(r["action"] for r in rows)
        priority = [r for r in rows if r.get("is_priority")]
        print(f"{ENGINE_VERSION} wrote {len(rows)} rows → {paths.signals.name}")
        print(f"Regime modifier: {rm} ({out['regime_label']}) · VIX {prices.get('vix_close')} · cash ${cash_avail:.0f}")
        print(f"Actions: {dict(actions)}")
        print(f"\nPriority Top-{action_mod.PRIORITY_TOP_N}:")
        print(f"{'#':>3} {'Tk':<6} {'Layer':<22} {'Conv':>5} {'TS':>4} {'EQ':>4} {'6M':>6} {'Action':<11}")
        for r in priority:
            mom = r.get("avg_monthly_6m_pct")
            mom_s = f"{mom:.1f}%" if mom is not None else "-"
            print(f"{r['priority_rank']:>3} {r['ticker']:<6} {(r['layer'] or '-')[:22]:<22} "
                  f"{r['conviction_score']:>5} {r['trend_strength']:>4} "
                  f"{r['entry_quality']:>4} {mom_s:>6} {r['action']:<11}")
    return out
