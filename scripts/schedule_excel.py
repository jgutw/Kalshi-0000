#!/usr/bin/env python3
"""
schedule_excel.py — Run export_excel.py every 10 minutes using the schedule library.
Companion to the Kalshi bot: run in a separate terminal alongside the bot and dashboard.
Ctrl+C stops cleanly.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# scripts/ is on path so we can import sibling export_excel;
# project root stays cwd-relative for logs when run from repo root.
SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

import schedule


def run_export() -> None:
    """Execute the Excel export."""
    import export_excel
    export_excel.main()


def main() -> None:
    print("Scheduling Excel export every 10 minutes. Ctrl+C to stop.")
    schedule.every(10).minutes.do(run_export)
    # Run immediately on start
    run_export()
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)  # Check every 30 seconds
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
