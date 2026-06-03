"""StockChaser engine package — anti-martingale strategy core.

Public API:
    from engine import run_pipeline, active_alerts, dismiss_alert
    from engine import Paths, ENGINE_VERSION

Modules:
    scoring      TS/EQ/FC/Conviction + signal detection
    regime       SPY trend × VIX dampener
    framework    F1/F2/F3 industry chain tags
    action       map_action / priority / position sizing
    stops        HWM trailing stop ladder
    alerts       exit trigger detection
    playbook     cash-aware operation manual
    pipeline     orchestration
"""
from .pipeline import run as run_pipeline, Paths, ENGINE_VERSION
from .alerts import (
    detect_and_persist,
    active_alerts,
    dismiss as dismiss_alert,
    RETIRED_TYPES,
    ANTI_MART_ONLY_TYPES,
    SEVERITY,
)
from .stops import update_all_stops, compute_stop
from .playbook import build_playbook, load_config as load_playbook_config

__all__ = [
    "run_pipeline", "Paths", "ENGINE_VERSION",
    "detect_and_persist", "active_alerts", "dismiss_alert",
    "RETIRED_TYPES", "ANTI_MART_ONLY_TYPES", "SEVERITY",
    "update_all_stops", "compute_stop",
    "build_playbook", "load_playbook_config",
]
