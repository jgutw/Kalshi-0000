"""
runtime_control.py — Cross-process control plane for Telegram / dashboard.

State file:  logs/runtime_control.json   (pause flag, last applied knobs)
Command queue: logs/bot_control.jsonl    (Telegram → trading bot)
Open positions snapshot: logs/open_positions.json  (bot → Telegram)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .config import cfg

log = logging.getLogger("kalshi_bot.runtime_control")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
RUNTIME_PATH = LOGS_DIR / "runtime_control.json"
COMMANDS_PATH = LOGS_DIR / "bot_control.jsonl"
OPEN_POSITIONS_PATH = LOGS_DIR / "open_positions.json"
LIVE_CONFIG_PATH = LOGS_DIR / "live_config.json"
BOT_HEARTBEAT_PATH = LOGS_DIR / "bot_heartbeat.json"
BOT_HEARTBEAT_STALE_SECS = 20.0

# Presets that Telegram /profile can apply (paper only).
PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "max_risk_paper": {
        "CONFIG_PROFILE": "max_risk_paper",
        "KELLY_FRACTION": 0.50,
        "MAX_POS_PCT": 0.08,
        "PORTFOLIO_GROSS_CAP": 0.30,
        "MIN_TRADE_USD": 5.0,
        "MIN_ENTRY_PRICE": 0.02,
        "MAX_ENTRY_PRICE": 0.98,
    },
    "max_risk_micro": {
        "CONFIG_PROFILE": "max_risk_micro",
        "KELLY_FRACTION": 0.50,
        "MAX_POS_PCT": 0.10,
        "PORTFOLIO_GROSS_CAP": 0.35,
        "MIN_TRADE_USD": 2.0,
        "MIN_ENTRY_PRICE": 0.02,
        "MAX_ENTRY_PRICE": 0.98,
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_runtime() -> dict[str, Any]:
    return {
        "entries_paused": False,
        "stop_requested": False,
        "updated_at": _now(),
        "source": "init",
    }


def load_runtime() -> dict[str, Any]:
    if not RUNTIME_PATH.exists():
        return default_runtime()
    try:
        data = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_runtime()
        base = default_runtime()
        base.update(data)
        return base
    except (json.JSONDecodeError, OSError) as e:
        log.warning("runtime_control read failed: %s", e)
        return default_runtime()


def save_runtime(data: dict[str, Any]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["updated_at"] = _now()
    RUNTIME_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def entries_paused() -> bool:
    return bool(load_runtime().get("entries_paused"))


def stop_requested() -> bool:
    return bool(load_runtime().get("stop_requested"))


def set_entries_paused(paused: bool, source: str = "telegram") -> None:
    rt = load_runtime()
    rt["entries_paused"] = bool(paused)
    rt["source"] = source
    # Clearing pause should not leave a stale stop bit unless explicitly set.
    save_runtime(rt)


def request_stop(source: str = "telegram") -> None:
    rt = load_runtime()
    rt["stop_requested"] = True
    rt["entries_paused"] = True
    rt["source"] = source
    save_runtime(rt)


def clear_stop(source: str = "bot") -> None:
    rt = load_runtime()
    rt["stop_requested"] = False
    rt["source"] = source
    save_runtime(rt)


def clear_bot_command_queue() -> None:
    """Drop pending Telegram control commands (avoids stale /stop killing a new round)."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        COMMANDS_PATH.write_text("", encoding="utf-8")
    except OSError as e:
        log.warning("clear bot_control queue failed: %s", e)


def prepare_for_new_round(source: str = "start_round") -> None:
    """Reset pause/stop flags and drain command queue before spawning a bot."""
    clear_bot_command_queue()
    rt = default_runtime()
    rt["entries_paused"] = False
    rt["stop_requested"] = False
    rt["source"] = source
    save_runtime(rt)


def enqueue_bot_command(action: str, **payload: Any) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = {"ts": _now(), "action": action, **payload}
    with open(COMMANDS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(cmd) + "\n")


def write_open_positions(positions: list[dict[str, Any]]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"ts": _now(), "positions": positions}
    OPEN_POSITIONS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_live_config() -> None:
    """Snapshot of trading knobs from the running bot process (for Telegram views)."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": _now(),
        "CONFIG_PROFILE": cfg.CONFIG_PROFILE,
        "KELLY_FRACTION": cfg.KELLY_FRACTION,
        "MAX_POS_PCT": cfg.MAX_POS_PCT,
        "PORTFOLIO_GROSS_CAP": cfg.PORTFOLIO_GROSS_CAP,
        "MIN_TRADE_USD": cfg.MIN_TRADE_USD,
        "MIN_ENTRY_PRICE": cfg.MIN_ENTRY_PRICE,
        "MAX_ENTRY_PRICE": cfg.MAX_ENTRY_PRICE,
        "MIN_EDGE_PCT": cfg.MIN_EDGE_PCT,
        "DRY_RUN": cfg.DRY_RUN,
        "SIM_BALANCE": cfg.SIM_BALANCE,
        "MAX_DRAWDOWN_PCT": cfg.MAX_DRAWDOWN_PCT,
        "MAX_DAILY_LOSS_PCT": cfg.MAX_DAILY_LOSS_PCT,
        "MAX_CONSEC_LOSSES": cfg.MAX_CONSEC_LOSSES,
    }
    LIVE_CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_live_config() -> dict[str, Any]:
    if not LIVE_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(LIVE_CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def write_bot_heartbeat(extra: Optional[dict[str, Any]] = None) -> None:
    """Trading bot liveness signal for Telegram /stop behavior."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "ts": _now(),
        "unix": __import__("time").time(),
        "profile": cfg.CONFIG_PROFILE,
        "dry_run": cfg.DRY_RUN,
    }
    if extra:
        payload.update(extra)
    BOT_HEARTBEAT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def trading_bot_running(stale_secs: float = BOT_HEARTBEAT_STALE_SECS) -> bool:
    """True if the trading bot wrote a recent heartbeat."""
    if not BOT_HEARTBEAT_PATH.exists():
        return False
    try:
        data = json.loads(BOT_HEARTBEAT_PATH.read_text(encoding="utf-8"))
        unix = float(data.get("unix") or 0)
        if unix <= 0:
            return False
        age = __import__("time").time() - unix
        return age <= float(stale_secs)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return False


def load_open_positions() -> list[dict[str, Any]]:
    if not OPEN_POSITIONS_PATH.exists():
        return []
    try:
        data = json.loads(OPEN_POSITIONS_PATH.read_text(encoding="utf-8"))
        pos = data.get("positions") if isinstance(data, dict) else None
        return list(pos) if isinstance(pos, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def apply_profile(name: str) -> tuple[bool, str]:
    key = name.strip().lower()
    preset = PROFILE_PRESETS.get(key)
    if not preset:
        return False, f"Unknown profile '{name}'. Try: {', '.join(PROFILE_PRESETS)}"
    for field, value in preset.items():
        setattr(cfg, field, value)
    return True, f"Applied profile {key}"


def process_bot_commands(
    on_stop: Optional[Callable[[], None]] = None,
) -> list[str]:
    """
    Drain bot_control.jsonl and apply to cfg / runtime flags.
    Returns human-readable result lines for logging.
    """
    if not COMMANDS_PATH.exists():
        return []
    try:
        raw = COMMANDS_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    if not raw.strip():
        return []

    results: list[str] = []
    kept: list[str] = []

    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = str(cmd.get("action") or "")
        try:
            if action == "pause":
                set_entries_paused(True, source=str(cmd.get("source") or "telegram"))
                results.append("entries paused")
            elif action == "resume":
                set_entries_paused(False, source=str(cmd.get("source") or "telegram"))
                results.append("entries resumed")
            elif action == "stop":
                request_stop(source=str(cmd.get("source") or "telegram"))
                results.append("stop requested")
                if on_stop:
                    on_stop()
            elif action == "set_max_pos":
                val = float(cmd["value"])
                if not 0.01 <= val <= 0.50:
                    results.append(f"set_max_pos rejected: {val}")
                else:
                    cfg.MAX_POS_PCT = val
                    results.append(f"MAX_POS_PCT={val:.2%}")
            elif action == "set_min_trade":
                val = float(cmd["value"])
                if val < 0.5:
                    results.append(f"set_min_trade rejected: {val}")
                else:
                    cfg.MIN_TRADE_USD = val
                    results.append(f"MIN_TRADE_USD=${val:.2f}")
            elif action == "set_kelly":
                val = float(cmd["value"])
                if not 0.05 <= val <= 1.0:
                    results.append(f"set_kelly rejected: {val}")
                else:
                    cfg.KELLY_FRACTION = val
                    results.append(f"KELLY_FRACTION={val:.2f}")
            elif action == "set_portfolio_cap":
                val = float(cmd["value"])
                if not 0.05 <= val <= 1.0:
                    results.append(f"set_portfolio_cap rejected: {val}")
                else:
                    cfg.PORTFOLIO_GROSS_CAP = val
                    results.append(f"PORTFOLIO_GROSS_CAP={val:.2%}")
            elif action == "profile":
                ok, msg = apply_profile(str(cmd.get("name") or ""))
                results.append(msg if ok else f"profile failed: {msg}")
            else:
                kept.append(line)
                results.append(f"unknown action kept: {action}")
        except (KeyError, TypeError, ValueError) as e:
            results.append(f"bad command {action}: {e}")

    try:
        if kept:
            COMMANDS_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            COMMANDS_PATH.write_text("", encoding="utf-8")
    except OSError as e:
        log.warning("bot_control cleanup failed: %s", e)

    if results:
        write_live_config()
    for line in results:
        log.info("runtime_control: %s", line)
    return results
