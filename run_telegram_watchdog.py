#!/usr/bin/env python3
"""
Keep the Telegram bridge process alive forever.

Restarts on crash/exit. Safe to leave running at login (Task Scheduler / Startup).

Usage:
  python run_telegram_watchdog.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / "venv" / "Scripts" / "python.exe"
PY = str(VENV_PY if VENV_PY.exists() else sys.executable)
LOG = ROOT / "logs" / "telegram_watchdog.log"
BRIDGE = ROOT / "run_telegram_bridge.py"


def _log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def main() -> None:
    check_secs = 15.0
    _log("Telegram watchdog start")
    proc: subprocess.Popen | None = None
    restarts = 0
    while True:
        if proc is None or proc.poll() is not None:
            code = None if proc is None else proc.returncode
            if proc is not None:
                restarts += 1
                _log(f"Bridge exited code={code} — restart #{restarts}")
                time.sleep(2)
            _log("Starting Telegram bridge")
            proc = subprocess.Popen(
                [PY, str(BRIDGE)],
                cwd=str(ROOT),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        time.sleep(check_secs)


if __name__ == "__main__":
    main()
