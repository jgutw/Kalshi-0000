#!/usr/bin/env python3
"""
Overnight watchdog — keeps the paper bot process alive for a long session.

- Does NOT use --fresh-round (continues same logs / same round).
- Restarts the bot if the process exits (crash / kill).
- Consecutive-loss cooldowns and drawdown halts do NOT exit the process;
  the bot stays up and resumes when cool-downs / daily reset allow.
- Optional: if equity drawdown halt is stuck, log it (no auto new round by default).

Usage (from project root):
  python run_overnight_watchdog.py --hours 8
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / "venv" / "Scripts" / "python.exe"
PY = str(VENV_PY if VENV_PY.exists() else sys.executable)
LOG = ROOT / "logs" / "overnight_watchdog.log"
SIM = ROOT / "logs" / "kalshi_sim.json"
META = ROOT / "logs" / "session_meta.json"


def _log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _bot_cmd(session_tag: str, profile: str) -> list[str]:
    return [
        PY,
        str(ROOT / "run_kalshi_bot.py"),
        "--mode", "run",
        "--profile", profile,
        "--session-tag", session_tag,
        # no --fresh-round, no --sim-balance → continue existing sim / logs
    ]


def _read_sim() -> dict:
    try:
        if SIM.exists() and SIM.stat().st_size > 0:
            return json.loads(SIM.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--session-tag", default="round_23_phase1_windows_1000")
    ap.add_argument("--profile", default="max_risk_paper")
    ap.add_argument("--check-secs", type=float, default=30.0)
    args = ap.parse_args()

    deadline = time.time() + args.hours * 3600
    _log(
        f"Watchdog start | hours={args.hours} tag={args.session_tag} "
        f"profile={args.profile} until={datetime.fromtimestamp(deadline).isoformat()}"
    )

    proc: subprocess.Popen | None = None
    restarts = 0

    try:
        while time.time() < deadline:
            if proc is None or proc.poll() is not None:
                code = None if proc is None else proc.returncode
                if proc is not None:
                    restarts += 1
                    _log(f"Bot exited code={code} — restart #{restarts} (same round, no fresh)")
                else:
                    _log("Starting bot (same round)")
                proc = subprocess.Popen(
                    _bot_cmd(args.session_tag, args.profile),
                    cwd=str(ROOT),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _log(f"Bot pid={proc.pid}")

            sim = _read_sim()
            if sim:
                bal = sim.get("balance")
                trades = sim.get("total_trades")
                consec = sim.get("consec_losses")
                _log(
                    f"heartbeat pid={proc.pid if proc else '?'} "
                    f"bal={bal} trades={trades} consec_losses={consec}"
                )

            # Sleep in small slices so we stop near deadline
            wake = min(args.check_secs, max(1.0, deadline - time.time()))
            time.sleep(wake)
    finally:
        if proc is not None and proc.poll() is None:
            _log(f"Watchdog window ended — leaving bot pid={proc.pid} running")
        _log(f"Watchdog done | restarts={restarts}")


if __name__ == "__main__":
    main()
