"""Volume Profile (POC / VAH / VAL) from intraday bars.

Concept (郭佳豪 量价统一 思路):
  - 看前一天 5-min K 线的成交量分布
  - POC (Point of Control)   量最大的价位 = 次日磁场中心
  - VAH (Value Area High)    70% 量集中区间的上沿 = 次日阻力参考
  - VAL (Value Area Low)     70% 量集中区间的下沿 = 次日支撑参考
  - 首尾盘 (Opening Range / Closing Range) 通常堆积量大

集成定位 (v3.7 A 方案):
  - 仅做参考展示, 不进 conviction 评分
  - 让用户下单时知道 "在哪个价位接货 / 哪个价位减仓"
  - 反马丁友好: 突破 VAH = 强势, 跌破 VAL = 警惕
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path


VP_FILE_NAME = "volume_profile.json"
N_BUCKETS = 50
VALUE_AREA_PCT = 0.70
MIN_BARS_FOR_PROFILE = 30  # ~半个交易日的 5-min 数


def compute_profile(bars, n_buckets=N_BUCKETS, value_area_pct=VALUE_AREA_PCT):
    """计算单个交易日的 POC / VAH / VAL.

    Args:
        bars: list[dict] 每条 K 线含 {high, low, close, volume}
        n_buckets: 价位分桶数 (越大越细)
        value_area_pct: value area 占总量的比例 (TPO 标准 70%)

    Returns:
        {poc, vah, val, session_high, session_low, total_volume} 或 None
    """
    if not bars or len(bars) < MIN_BARS_FOR_PROFILE:
        return None

    min_p = min(b["low"] for b in bars)
    max_p = max(b["high"] for b in bars)
    if max_p <= min_p:
        return None

    bucket_size = (max_p - min_p) / n_buckets
    volumes = [0.0] * n_buckets

    # 把每条 K 线的量按其 high-low 范围均匀分给覆盖的桶
    for b in bars:
        lo_idx = max(0, min(n_buckets - 1, int((b["low"] - min_p) / bucket_size)))
        hi_idx = max(0, min(n_buckets - 1, int((b["high"] - min_p) / bucket_size)))
        n_touched = hi_idx - lo_idx + 1
        vol_per = (b.get("volume") or 0) / n_touched
        for i in range(lo_idx, hi_idx + 1):
            volumes[i] += vol_per

    total_vol = sum(volumes)
    if total_vol <= 0:
        return None

    # POC = 量最大的桶中心
    poc_idx = max(range(n_buckets), key=lambda i: volumes[i])
    poc_price = min_p + (poc_idx + 0.5) * bucket_size

    # Value Area: 从 POC 向两侧扩展, 直到累计到 70% 总量
    target_vol = total_vol * value_area_pct
    accumulated = volumes[poc_idx]
    lo_i = hi_i = poc_idx
    while accumulated < target_vol and (lo_i > 0 or hi_i < n_buckets - 1):
        next_up = volumes[hi_i + 1] if hi_i + 1 < n_buckets else -1
        next_dn = volumes[lo_i - 1] if lo_i - 1 >= 0 else -1
        # 取量大的方向扩 (经典 TPO 算法)
        if next_up >= next_dn and next_up >= 0:
            hi_i += 1
            accumulated += next_up
        elif next_dn >= 0:
            lo_i -= 1
            accumulated += next_dn
        else:
            break

    val_price = min_p + lo_i * bucket_size
    vah_price = min_p + (hi_i + 1) * bucket_size

    # 首尾盘价位 (前 3 根和后 3 根 K 线的均价)
    n_open = min(3, len(bars))
    n_close = min(3, len(bars))
    opening_avg = sum(b["close"] for b in bars[:n_open]) / n_open
    closing_avg = sum(b["close"] for b in bars[-n_close:]) / n_close

    return {
        "poc": round(poc_price, 2),
        "vah": round(vah_price, 2),
        "val": round(val_price, 2),
        "session_high": round(max_p, 2),
        "session_low": round(min_p, 2),
        "opening_avg": round(opening_avg, 2),
        "closing_avg": round(closing_avg, 2),
        "total_volume": int(total_vol),
        "n_bars": len(bars),
    }


def fetch_intraday_bars(tickers, lookback_days=2, interval="5m"):
    """yfinance 拿多 ticker 的 5-min K 线, 按 ticker → bars (含 date) 分组.

    Returns:
        dict[ticker, list[dict]] 每条 bar = {date, time, open, high, low, close, volume}
    """
    try:
        import yfinance as yf
    except ImportError:
        print("  ! yfinance not installed; skipping VP")
        return {}

    if not tickers:
        return {}

    period = f"{lookback_days}d"
    out = {}
    # 用 group_by="ticker" 批量抓
    df = yf.download(
        tickers, period=period, interval=interval,
        group_by="ticker", auto_adjust=True, progress=False, threads=True,
    )

    for t in tickers:
        try:
            if len(tickers) == 1:
                sub = df  # single-ticker returns flat columns
            else:
                sub = df[t]
            sub = sub.dropna()
            if sub.empty:
                continue
            bars = []
            for idx, row in sub.iterrows():
                bars.append({
                    "date": idx.date().isoformat(),
                    "time": idx.time().isoformat(),
                    "open":  float(row["Open"]),
                    "high":  float(row["High"]),
                    "low":   float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row["Volume"]),
                })
            out[t] = bars
        except (KeyError, AttributeError, ValueError) as e:
            print(f"  ! VP fetch {t}: {e}")
    return out


def latest_session_bars(bars):
    """从混合多日的 bars 里挑最近一个完整交易日 (>= MIN_BARS_FOR_PROFILE 根 K 线)."""
    if not bars:
        return None, None
    by_date = {}
    for b in bars:
        by_date.setdefault(b["date"], []).append(b)
    # 按日期降序, 找第一个满足 min_bars 的
    for d in sorted(by_date.keys(), reverse=True):
        if len(by_date[d]) >= MIN_BARS_FOR_PROFILE:
            return d, by_date[d]
    return None, None


def compute_profiles(tickers, lookback_days=2, interval="5m"):
    """主入口: 批量算 VP, 返回 {ticker: profile_dict}."""
    all_bars = fetch_intraday_bars(tickers, lookback_days, interval)
    out = {}
    for t in tickers:
        bars = all_bars.get(t)
        session_date, session_bars = latest_session_bars(bars)
        if not session_bars:
            continue
        prof = compute_profile(session_bars)
        if prof:
            prof["session_date"] = session_date
            prof["ticker"] = t
            out[t] = prof
    return out


def save_profiles(profiles, out_path):
    """写 data/volume_profile.json."""
    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": "5-min bars, 70% value area, distribute volume across high-low range",
        "tickers": profiles,
    }
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_profiles(in_path):
    p = Path(in_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
