"""Pipeline entry point — score, tag, rank, update stops, detect alerts.

Run:
    python3 scripts/run_pipeline.py

Replaces (no longer needed individually):
    python3 scripts/engine_v35.py
    python3 scripts/dynamic_stops.py
    python3 scripts/alerts.py
"""
import sys
from pathlib import Path

# Make `engine` package importable when running from repo root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import run_pipeline


if __name__ == "__main__":
    run_pipeline()
