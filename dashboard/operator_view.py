"""Mode and source labels for the Control Room. No process starts and no orders."""

from __future__ import annotations

import json

FRIENDLY_PROFILES = {
    "max_risk_micro": ("micro", "aggressive"),
    "max_risk_paper": ("standard",),
    "engineered_risk": ("conservative",),
    "live_safe": ("tight",),
}


def production_mode(state: str, mode: str) -> str:
    """PAPER, LIVE, or UNKNOWN. Unknown never becomes Live."""
    if state == "running" and mode == "paper":
        return "PAPER"
    if state == "running" and mode == "live":
        return "LIVE"
    return "UNKNOWN"


def production_banner(mode: str) -> str:
    if mode == "PAPER":
        return "PAPER — NO REAL CAPITAL"
    if mode == "LIVE":
        return "LIVE — REAL CAPITAL CAN BE AFFECTED"
    return "MODE UNKNOWN — LOCAL DATA ONLY"


def shadow_running(state: str, mode: str) -> bool:
    return state == "running" and mode == "shadow"


def shadow_banner(running: bool) -> str:
    if running:
        return "SHADOW — NO CAPITAL\nRUNNING NOW"
    return "SHADOW — NO CAPITAL\nHISTORICAL — NOT RUNNING"


def trade_outcome(pnl: float) -> str:
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "scratch"


def loss_streak_ceiling(mode: str, configured: int | None) -> int | None:
    """Live ceiling is 3. Do not invent an 8-loss streak when the mode is live or unknown."""
    if mode == "LIVE":
        return 3
    if mode == "PAPER" and configured is not None:
        return configured
    return None


def observe_ownership() -> tuple[str, str, str]:
    """Read ownership without deleting the record. Returns state, mode, session."""
    from kalshi_bot.process_ownership import OWNERSHIP_PATH, MARKERS, inspect_process
    try:
        record = json.loads(OWNERSHIP_PATH.read_text(encoding="utf-8"))
    except Exception:
        return "dead", "", ""
    mode = str(record.get("mode") or "")
    session = str(record.get("session") or "")
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return "unknown", mode, session
    info = inspect_process(pid)
    if info.get("state") != "alive":
        return "dead", mode, session
    created = str(record.get("created") or "")
    actual = str(info.get("created") or "")
    marker = str(record.get("marker") or MARKERS.get(mode, ""))
    command = str(info.get("command") or "")
    if created and actual and created != actual:
        return "reused", mode, session
    if marker and marker not in command:
        return "unknown", mode, session
    if not created or not actual:
        return "unknown", mode, session
    return "running", mode, session


def friendly_profile(preset: str) -> str | None:
    names = FRIENDLY_PROFILES.get(preset)
    if names is None or len(names) != 1:
        return None
    return names[0]
