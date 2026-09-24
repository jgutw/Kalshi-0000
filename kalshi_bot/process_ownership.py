"""Authoritative trader process ownership.

A heartbeat is health. This module answers whether a PAPER, SHADOW, or LIVE
trader process exists. Startup and stop share it. It does not kill processes.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger("kalshi_bot.process_ownership")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
OWNERSHIP_PATH = LOGS_DIR / "trader_ownership.json"
LOCK_PATH = LOGS_DIR / "trader_start.lock"
SHADOW_STOP_PATH = LOGS_DIR / "shadow_stop.request"

MARKERS = {
    "paper": "run_kalshi_bot.py",
    "live": "run_kalshi_bot.py",
    "shadow": "run_shadow.py",
}


@dataclass
class Ownership:
    mode: str
    pid: int
    created: str
    session: str
    marker: str
    started_at: str
    state: str  # running | dead | reused | unknown | alien


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_record() -> Optional[dict[str, Any]]:
    if not OWNERSHIP_PATH.exists():
        return None
    try:
        data = json.loads(OWNERSHIP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"unreadable": True}
    return data if isinstance(data, dict) else {"unreadable": True}


def _write_record(record: dict[str, Any]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    OWNERSHIP_PATH.write_text(json.dumps(record, indent=2), encoding="utf-8")


def clear_ownership() -> None:
    try:
        OWNERSHIP_PATH.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not clear ownership: %s", exc)


def inspect_process(pid: int) -> dict[str, Any]:
    """OS identity for one PID. Tests patch this. Never terminates the process."""
    if pid <= 0:
        return {"state": "dead"}
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return {"state": "dead"}
        except Exception:
            return {"state": "unknown"}
        return {"state": "unknown"}
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\" | "
             "Select-Object ProcessId, CreationDate, CommandLine | ConvertTo-Json -Compress)"],
            text=True, timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return {"state": "unknown"}
    text = (out or "").strip()
    if not text or text == "null":
        return {"state": "dead"}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"state": "unknown"}
    if isinstance(data, list):
        data = data[0] if data else {}
    return {
        "state": "alive",
        "created": str(data.get("CreationDate") or ""),
        "command": str(data.get("CommandLine") or ""),
    }


def scan_traders() -> Optional[list[dict[str, Any]]]:
    """Matching trader processes, or None if the scan itself failed."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'run_kalshi_bot.py|run_shadow.py' } | "
             "Select-Object ProcessId, CreationDate, CommandLine | ConvertTo-Json -Compress"],
            text=True, timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out or "").strip()
    if not text or text == "null":
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    rows = data if isinstance(data, list) else [data]
    found = []
    for row in rows:
        command = str(row.get("CommandLine") or "")
        if "run_telegram_bridge.py" in command:
            continue
        if "run_shadow.py" in command:
            mode = "shadow"
        elif "run_kalshi_bot.py" in command and "--live" in command:
            mode = "live"
        elif "run_kalshi_bot.py" in command:
            mode = "paper"
        else:
            continue
        found.append({
            "mode": mode,
            "pid": int(row.get("ProcessId") or 0),
            "created": str(row.get("CreationDate") or ""),
            "command": command,
        })
    return found


def reconcile() -> Ownership:
    """Return the owned trader, or state dead/unknown. Dead stale records are cleared."""
    record = _read_record()
    if record and record.get("unreadable"):
        return Ownership("", 0, "", "", "", "", "unknown")
    if not record:
        found = scan_traders()
        if found is None:
            return Ownership("", 0, "", "", "", "", "unknown")
        if found:
            row = found[0]
            return Ownership(row["mode"], row["pid"], row["created"], "", MARKERS[row["mode"]], "", "alien")
        return Ownership("", 0, "", "", "", "", "dead")
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return Ownership("", 0, "", "", "", "", "unknown")
    mode = str(record.get("mode") or "")
    created = str(record.get("created") or "")
    marker = str(record.get("marker") or MARKERS.get(mode, ""))
    info = inspect_process(pid)
    if info.get("state") == "unknown":
        return Ownership(mode, pid, created, str(record.get("session") or ""), marker,
                         str(record.get("started_at") or ""), "unknown")
    if info.get("state") == "dead":
        clear_ownership()
        return Ownership(mode, pid, created, str(record.get("session") or ""), marker,
                         str(record.get("started_at") or ""), "dead")
    actual_created = str(info.get("created") or "")
    command = str(info.get("command") or "")
    if created and actual_created and actual_created != created:
        clear_ownership()
        return Ownership(mode, pid, actual_created, "", marker, "", "reused")
    if marker and marker not in command:
        return Ownership(mode, pid, actual_created or created, str(record.get("session") or ""),
                         marker, str(record.get("started_at") or ""), "unknown")
    if not actual_created:
        return Ownership(mode, pid, created, str(record.get("session") or ""), marker,
                         str(record.get("started_at") or ""), "unknown")
    return Ownership(mode, pid, actual_created, str(record.get("session") or ""), marker,
                     str(record.get("started_at") or ""), "running")


def record_ownership(mode: str, pid: int, session: str) -> Ownership:
    info = inspect_process(pid)
    if info.get("state") != "alive" or not info.get("created"):
        raise RuntimeError(f"spawned pid {pid} could not be identified")
    marker = MARKERS[mode]
    if marker not in str(info.get("command") or ""):
        raise RuntimeError(f"pid {pid} is not the {mode} trader")
    record = {
        "mode": mode,
        "pid": pid,
        "created": str(info["created"]),
        "session": session,
        "marker": marker,
        "started_at": _now(),
    }
    _write_record(record)
    return Ownership(mode, pid, record["created"], session, marker, record["started_at"], "running")


@contextmanager
def start_lock() -> Iterator[None]:
    """Cross-process exclusive start. Fail closed if the lock is held or unreadable."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("another start is in progress") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("another start is in progress") from exc
        yield
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def refuse_if_conflict(requested: str) -> Optional[str]:
    """None when a start may proceed. A string when it must fail closed."""
    owned = reconcile()
    if owned.state in ("unknown", "alien", "reused"):
        return f"trader state is {owned.state}; start refused"
    if owned.state == "running":
        return f"{owned.mode} is already running (pid {owned.pid})"
    found = scan_traders()
    if found is None:
        return "could not scan processes; start refused"
    if found:
        return f"{found[0]['mode']} process is already running"
    if requested not in MARKERS:
        return f"unknown mode {requested}"
    return None


def request_graceful_stop() -> dict[str, Any]:
    """Signal the owned trader. Do not archive and do not kill."""
    owned = reconcile()
    if owned.state == "unknown":
        return {"ok": False, "outcome": "uncertain", "detail": "process state could not be determined",
                "mode": owned.mode, "pid": owned.pid}
    if owned.state == "alien":
        return {"ok": False, "outcome": "uncertain",
                "detail": f"unregistered {owned.mode} process pid {owned.pid}; not signaled",
                "mode": owned.mode, "pid": owned.pid}
    if owned.state in ("dead", "reused"):
        return {"ok": True, "outcome": "already_stopped", "detail": owned.state,
                "mode": owned.mode, "pid": owned.pid}
    if owned.mode == "shadow":
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        SHADOW_STOP_PATH.write_text(_now(), encoding="utf-8")
    elif owned.mode in ("paper", "live"):
        from kalshi_bot.runtime_control import enqueue_bot_command
        enqueue_bot_command("stop", source="telegram_ownership")
    else:
        return {"ok": False, "outcome": "uncertain", "detail": f"unsupported mode {owned.mode}",
                "mode": owned.mode, "pid": owned.pid}
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        info = inspect_process(owned.pid)
        if info.get("state") == "dead":
            clear_ownership()
            try:
                SHADOW_STOP_PATH.unlink(missing_ok=True)
            except OSError:
                pass
            return {"ok": True, "outcome": "stopped", "detail": "process exited",
                    "mode": owned.mode, "pid": owned.pid}
        if info.get("state") == "alive" and str(info.get("created") or "") not in ("", owned.created):
            return {"ok": False, "outcome": "uncertain", "detail": "pid identity changed during stop",
                    "mode": owned.mode, "pid": owned.pid}
        time.sleep(0.4)
    return {"ok": False, "outcome": "timeout",
            "detail": "graceful stop timed out; process was not killed",
            "mode": owned.mode, "pid": owned.pid}
