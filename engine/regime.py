"""Market regime modifier.

regime_modifier = _base_regime(SPY trend) × _vix_dampener(fear gauge)

  Base (SPY-only):
    SPY < SMA200 + dist<-15% / 4w<-12%      → 0.40  深熊+加速
    SPY < SMA200 + 4w<-5%                   → 0.50  中熊
    SPY < SMA200 否则                       → 0.60  浅熊但稳
    SPY > SMA200 + SMA50 + 4w>=4%           → 1.10  强 risk-on
    SPY > SMA200 + SMA50 + 4w>=1%           → 1.00  risk-on
    SPY > SMA200 + SMA50 否则               → 0.90  弱多头
    -3 <= 4w < 0                            → 0.80
    4w < -5                                 → 0.65
    其他                                    → 0.75

  VIX dampener:
    < 25  → 1.00  正常
    25-30 → 0.85  紧张
    30-40 → 0.65  压力
    >= 40 → 0.40  恐慌

  Example:
    SPY 强 risk-on × VIX 16  = 1.10 × 1.00 = 1.10
    SPY 强 risk-on × VIX 28  = 1.10 × 0.85 = 0.935
    SPY 浅熊      × VIX 45  = 0.50 × 0.40 = 0.200
"""
from __future__ import annotations


def _base_regime(prices_root):
    spy_above_50 = bool(prices_root.get("spy_above_sma50"))
    spy_above_200 = bool(prices_root.get("spy_above_sma200", True))
    spy_4w = prices_root.get("spy_4w_return_pct") or 0
    spy_dist_200 = prices_root.get("spy_dist_sma200_pct") or 0

    if not spy_above_200:
        if spy_dist_200 < -15 or spy_4w < -12: return 0.40
        if spy_4w < -5:                        return 0.50
        return 0.60

    if spy_above_50 and spy_4w >= 4:    return 1.10
    if spy_above_50 and spy_4w >= 1:    return 1.00
    if spy_above_50:                     return 0.90
    if -3 <= spy_4w < 0:                 return 0.80
    if spy_4w < -5:                      return 0.65
    return 0.75


def _vix_dampener(prices_root):
    vix = prices_root.get("vix_close")
    if vix is None:
        return 1.0
    if vix >= 40: return 0.40
    if vix >= 30: return 0.65
    if vix >= 25: return 0.85
    return 1.0


def regime_modifier(prices_root):
    """Composite regime = base (SPY trend) × VIX dampener. Rounded to 3 decimals."""
    return round(_base_regime(prices_root) * _vix_dampener(prices_root), 3)


def regime_label(rm):
    """Bucket regime modifier to human-readable label."""
    if rm >= 1.05: return "强 risk-on"
    if rm >= 0.95: return "risk-on"
    if rm >= 0.85: return "弱多头"
    if rm >= 0.60: return "中性偏防御"
    return "risk-off"


def vix_zone(vix):
    """Bucket VIX value to zone label."""
    if vix is None: return None
    if vix < 18: return "平静"
    if vix < 25: return "正常"
    if vix < 30: return "紧张"
    if vix < 40: return "压力"
    return "恐慌"
