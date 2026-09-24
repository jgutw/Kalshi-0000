"""Shared operator controls for Telegram.

Telegram must call these functions. They call the existing paper, Shadow, and
safe-live launchers. They do not implement a second trading engine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from kalshi_bot.config import cfg
from kalshi_bot.runtime_control import (
    PROFILE_PRESETS,
    enqueue_bot_command,
    entries_paused,
    load_live_config,
    set_entries_paused,
    trading_bot_running,
)
from kalshi_bot.safe_live import preflight_account, start_live_safe
from kalshi_bot.telegram.rounds import start_paper_round
from kalshi_bot.vault import enqueue_set_config, enqueue_take_cash, load_vault_config, VaultConfig

log = logging.getLogger("kalshi_bot.operator_control")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
AUDIT_PATH = LOGS_DIR / "telegram_operator_audit.jsonl"
SIM_PATH = LOGS_DIR / "kalshi_sim.json"
SHADOW_ROOT = PROJECT_ROOT / "shadow_data"
CONFIRM_TTL_SECS = 60.0
FORBIDDEN_SHADOW_IDS = {"shadow_era1b_001"}

# Explicit whitelist. Ranges match the running bot's command consumer.
SETTING_RULES: dict[str, dict[str, Any]] = {
    "set_max_pos": {"field": "MAX_POS_PCT", "lo": 0.01, "hi": 0.50, "kind": "pct"},
    "set_min_trade": {"field": "MIN_TRADE_USD", "lo": 0.5, "hi": 1000.0, "kind": "usd"},
    "set_kelly": {"field": "KELLY_FRACTION", "lo": 0.05, "hi": 1.0, "kind": "frac"},
    "set_portfolio_cap": {"field": "PORTFOLIO_GROSS_CAP", "lo": 0.05, "hi": 1.0, "kind": "pct"},
    "set_consec_losses": {"field": "MAX_CONSEC_LOSSES", "lo": 2, "hi": 8, "kind": "int"},
    "set_daily_loss": {"field": "MAX_DAILY_LOSS_PCT", "lo": 0.05, "hi": 0.50, "kind": "pct"},
    "set_max_drawdown": {"field": "MAX_DRAWDOWN_PCT", "lo": 0.05, "hi": 0.60, "kind": "pct"},
}

LIVE_ALIASES = {"/go", "/safe_live", "/resume_live"}
PAPER_ALIASES = {"/start_round", "/new", "/new_round"}

SendFn = Callable[[str], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chat_hash(chat_id: str) -> str:
    return hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:12]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def audit(chat_id: str, command: str, operation: str, outcome: str, reason: str, *,
          challenge: str = "", state: str = "") -> None:
    """Append one operator record. Never include tokens, keys, or raw chat ids."""
    record = {
        "ts": _now_iso(),
        "chat": _chat_hash(chat_id or ""),
        "command": command,
        "operation": operation,
        "challenge": _token_hash(challenge) if challenge else "",
        "outcome": outcome,
        "reason": reason,
        "state": state,
    }
    blob = json.dumps(record)
    lowered = blob.lower()
    if "bot_token" in lowered or "private_key" in lowered or "api_key" in lowered:
        log.warning("audit dropped a record that looked like a secret")
        return
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_PATH, "a", encoding="utf-8") as handle:
            handle.write(blob + "\n")
    except OSError as exc:
        log.warning("operator audit write failed: %s", exc)


class ConfirmError(Exception):
    pass


@dataclass
class _Pending:
    operation: str
    chat_hash: str
    payload: dict[str, Any]
    expires: float
    used: bool = False


@dataclass
class Confirmations:
    """In-memory, one-use challenges. Lost on process restart."""

    ttl: float = CONFIRM_TTL_SECS
    _items: dict[str, _Pending] = field(default_factory=dict)

    def clear(self) -> None:
        self._items.clear()

    def issue(self, operation: str, chat_id: str, payload: dict[str, Any], *, now: Optional[float] = None) -> str:
        token = secrets.token_hex(3)
        stamp = time.monotonic() if now is None else now
        self._items[_token_hash(token)] = _Pending(
            operation=operation,
            chat_hash=_chat_hash(chat_id),
            payload=dict(payload),
            expires=stamp + self.ttl,
        )
        return token

    def take(self, operation: str, chat_id: str, token: str, *, now: Optional[float] = None) -> dict[str, Any]:
        key = _token_hash(token or "")
        pending = self._items.get(key)
        stamp = time.monotonic() if now is None else now
        if pending is None:
            raise ConfirmError("unknown challenge")
        if pending.used:
            raise ConfirmError("challenge already used")
        if stamp > pending.expires:
            pending.used = True
            raise ConfirmError("challenge expired")
        if pending.chat_hash != _chat_hash(chat_id):
            raise ConfirmError("challenge is for a different chat")
        if pending.operation != operation:
            raise ConfirmError("challenge is for a different operation")
        pending.used = True
        return dict(pending.payload)


CONFIRM = Confirmations()


def shadow_process_running() -> bool:
    from kalshi_bot.process_ownership import reconcile
    owned = reconcile()
    return owned.state == "running" and owned.mode == "shadow"


def detect_modes() -> dict[str, Any]:
    from kalshi_bot.process_ownership import reconcile
    owned = reconcile()
    profile = ""
    heart = LOGS_DIR / "bot_heartbeat.json"
    if heart.exists():
        try:
            data = json.loads(heart.read_text(encoding="utf-8"))
            profile = str(data.get("profile") or "")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            profile = ""
    running = owned.state == "running"
    ambiguous = owned.state in ("unknown", "alien", "reused")
    return {
        "heartbeat": trading_bot_running(),
        "paper": running and owned.mode == "paper",
        "live": running and owned.mode == "live",
        "shadow": running and owned.mode == "shadow",
        "ambiguous": ambiguous,
        "profile": profile,
        "paused": entries_paused(),
        "ownership": owned.state,
        "mode": owned.mode,
    }


def mode_banner() -> str:
    modes = detect_modes()
    if modes["live"]:
        return "LIVE — REAL CAPITAL\n"
    if modes["shadow"] and not modes["paper"] and not modes["live"]:
        return "SHADOW — NO CAPITAL\n"
    if modes["paper"]:
        return "PAPER — NO REAL CAPITAL\n"
    return ""


def _sim_safety() -> dict[str, Any]:
    if not SIM_PATH.exists():
        return {"live_halt_reason": "", "daily_dd": None, "consec_losses": 0,
                "balance": None, "vault_balance": None}
    try:
        data = json.loads(SIM_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"live_halt_reason": "", "daily_dd": None, "consec_losses": 0,
                "balance": None, "vault_balance": None}
    peak = float(data.get("peak_equity") or 0)
    equity = float(data.get("total_equity") or data.get("balance") or 0)
    daily_dd = None
    if peak > 0 and equity:
        daily_dd = max(0.0, (peak - equity) / peak)
    return {
        "live_halt_reason": str(data.get("live_halt_reason") or ""),
        "daily_dd": daily_dd,
        "consec_losses": int(data.get("consec_losses") or 0),
        "balance": data.get("balance"),
        "vault_balance": data.get("vault_balance"),
    }


def independent_halts() -> list[str]:
    """Halts from SimState.is_halted for the owned mode. Pause is not a halt."""
    from kalshi_bot.process_ownership import reconcile
    from kalshi_bot.sim_state import SimState
    owned = reconcile()
    live = owned.state == "running" and owned.mode == "live"
    try:
        sim = SimState.load()
    except Exception as exc:
        return [f"halt state unreadable: {exc}"]
    previous = cfg.DRY_RUN
    cfg.DRY_RUN = not live
    try:
        halted, reason = sim.is_halted()
    finally:
        cfg.DRY_RUN = previous
    return [reason] if halted else []


def require_finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("value must be a finite number")
    return float(value)


def _environment_name() -> str:
    if os.getenv("KALSHI_DEMO", "false").lower() == "true":
        return "DEMO"
    if "demo-api" in os.getenv("KALSHI_BASE_URL", ""):
        return "DEMO"
    return "PRODUCTION"


def _conflict(modes: dict[str, Any]) -> str:
    active = [name for name in ("paper", "live", "shadow") if modes.get(name)]
    if modes.get("ambiguous"):
        return "trader heartbeat is up but paper/live mode is unknown"
    if len(active) > 1:
        return "conflicting modes: " + ", ".join(active)
    if active:
        return f"{active[0]} is already running"
    return ""


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(PROJECT_ROOT), text=True, timeout=8,
        )
        return out.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _spawn(cmd: list[str]) -> int:
    logs = LOGS_DIR
    logs.mkdir(parents=True, exist_ok=True)
    out_f = open(logs / "bot_stdout.log", "a", encoding="utf-8")
    out_f.write(f"\n\n===== operator {' '.join(cmd)} =====\n")
    out_f.flush()
    kwargs: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdout": out_f,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    proc = subprocess.Popen(cmd, **kwargs)
    return int(proc.pid)


def _sizing_line() -> str:
    from kalshi_bot.bankroll import trading_bankroll_cap
    cap = trading_bankroll_cap()
    if cap is None:
        return "full tradeable cash minus vault"
    return f"trading bankroll ${cap:.2f}"


def live_preflight_report() -> tuple[bool, str, dict[str, Any]]:
    """Read-only live preflight. Does not start a trader."""
    modes = detect_modes()
    try:
        account = preflight_account()
    except Exception as exc:
        return False, f"Kalshi preflight failed: {exc}", {"modes": modes}
    env = _environment_name()
    available = float(account.get("available") or 0)
    opens = int(account.get("open_count") or 0)
    api_ok = bool(account.get("api_ok"))
    problems: list[str] = []
    if not api_ok:
        problems.append("Kalshi API unreachable")
    if env != "PRODUCTION":
        problems.append(f"environment is {env}")
    if available < float(cfg.LIVE_MIN_AVAILABLE_USD):
        problems.append(f"available ${available:.2f} is below ${cfg.LIVE_MIN_AVAILABLE_USD:.2f}")
    conflict = _conflict(modes)
    if conflict:
        problems.append(conflict)
    safety = _sim_safety()
    lines = [
        "LIVE TRADING REQUEST",
        "REAL CAPITAL",
        f"Kalshi API: {'OK' if api_ok else 'UNREACHABLE'}",
        f"Environment: {env}",
        f"Available balance: ${available:.2f}",
        f"Open positions: {opens}",
        "Market data: feeds start with the trader; none observed beforehand",
        f"Competing trader: {conflict or 'NONE'}",
        f"Pause state: {'paused' if modes['paused'] else 'not paused'}",
        f"Risk halt: {safety['live_halt_reason'] or 'none'}",
        "Active profile: max_risk_micro",
        "Sizing: " + _sizing_line(),
    ]
    detail = {
        "account": account,
        "modes": modes,
        "environment": env,
        "problems": problems,
        "profile": "max_risk_micro",
    }
    if problems:
        return False, "\n".join(lines + ["", "Refused: " + "; ".join(problems)]), detail
    return True, "\n".join(lines), detail


def request_live(chat_id: str, send: SendFn) -> None:
    ok, text, detail = live_preflight_report()
    if not ok:
        audit(chat_id, "/start_live", "start_live", "refused", "; ".join(detail.get("problems") or ["preflight"]))
        send(text)
        return
    token = CONFIRM.issue("start_live", chat_id, {"profile": "max_risk_micro"})
    audit(chat_id, "/start_live", "start_live", "challenge", "awaiting confirm", challenge=token)
    send(text + f"\n\nConfirm with /confirm_live {token}")


def confirm_live(chat_id: str, token: str, send: SendFn) -> None:
    try:
        payload = CONFIRM.take("start_live", chat_id, token)
    except ConfirmError as exc:
        audit(chat_id, "/confirm_live", "start_live", "refused", str(exc), challenge=token)
        send(f"Refused. {exc}")
        return
    ok, text, detail = live_preflight_report()
    if not ok:
        audit(chat_id, "/confirm_live", "start_live", "refused", "stale preflight", challenge=token)
        send(text)
        return
    try:
        result = start_live_safe(str(payload.get("profile") or "max_risk_micro"))
    except Exception as exc:
        audit(chat_id, "/confirm_live", "start_live", "refused", str(exc), challenge=token)
        send(f"LIVE — REAL CAPITAL\nLaunch refused: {exc}")
        return
    pid = result.get("pid")
    if result.get("exit_code") is not None or not result.get("spawned"):
        audit(chat_id, "/confirm_live", "start_live", "refused", "spawn failed", challenge=token)
        send(f"LIVE — REAL CAPITAL\nSpawn failed. pid {pid} exit {result.get('exit_code')}")
        return
    if not result.get("ready"):
        audit(chat_id, "/confirm_live", "start_live", "uncertain", "not ready", challenge=token)
        send(
            "LIVE — REAL CAPITAL\n"
            f"Process spawned but not ready. pid {pid} ownership {result.get('ownership')}. "
            "Do not start another trader."
        )
        return
    audit(chat_id, "/confirm_live", "start_live", "accepted", "ready", challenge=token, state="LIVE")
    send(f"LIVE — REAL CAPITAL\nTrader ready. Session {result.get('session_tag')} pid {pid}")


def request_resume(chat_id: str, send: SendFn) -> None:
    modes = detect_modes()
    halts = independent_halts()
    safety = _sim_safety()
    lines = [
        "RESUME REQUEST",
        "This clears entries_paused only.",
        f"Mode: {'LIVE' if modes['live'] else 'PAPER' if modes['paper'] else 'SHADOW' if modes['shadow'] else 'idle'}",
        f"Heartbeat: {'RUNNING' if modes['heartbeat'] else 'NONE'}",
        "Market data: not sampled from Telegram",
        f"Manual pause: {'paused' if modes['paused'] else 'not paused'}",
        f"Live halt: {safety['live_halt_reason'] or 'none'}",
        f"Independent halts: {'; '.join(halts) if halts else 'none'}",
    ]
    if halts:
        audit(chat_id, "/resume", "resume", "refused", "; ".join(halts))
        send("\n".join(lines) + "\n\nRefused. An independent safety breaker still prohibits trading.")
        return
    token = CONFIRM.issue("resume", chat_id, {})
    audit(chat_id, "/resume", "resume", "challenge", "awaiting confirm", challenge=token)
    send("\n".join(lines) + f"\n\nConfirm with /confirm_resume {token}")


def confirm_resume(chat_id: str, token: str, send: SendFn) -> None:
    try:
        CONFIRM.take("resume", chat_id, token)
    except ConfirmError as exc:
        audit(chat_id, "/confirm_resume", "resume", "refused", str(exc), challenge=token)
        send(f"Refused. {exc}")
        return
    halts = independent_halts()
    if halts:
        audit(chat_id, "/confirm_resume", "resume", "refused", "; ".join(halts), challenge=token)
        send("Refused. An independent safety breaker still prohibits trading: " + "; ".join(halts))
        return
    set_entries_paused(False, source="telegram_confirm_resume")
    audit(chat_id, "/confirm_resume", "resume", "accepted", "entries_paused cleared", challenge=token)
    send("Manual pause cleared. Independent halts were not changed. Open positions were not closed.")


def request_paper(chat_id: str, args: list[str], send: SendFn) -> None:
    if not args:
        send("PAPER — NO REAL CAPITAL\nUsage: /papertrade <capital> [profile]")
        return
    try:
        capital = require_finite(float(args[0].replace("$", "").replace(",", "")))
    except ValueError:
        send("PAPER — NO REAL CAPITAL\nCapital must be a finite number.")
        return
    if not 50 <= capital <= 100_000:
        send("PAPER — NO REAL CAPITAL\nCapital must be between $50 and $100000.")
        return
    profile = args[1].strip().lower() if len(args) > 1 else "max_risk_paper"
    if profile not in PROFILE_PRESETS:
        send(f"Unknown profile. Allowed: {', '.join(PROFILE_PRESETS)}")
        return
    conflict = _conflict(detect_modes())
    if conflict:
        audit(chat_id, "/papertrade", "paper", "refused", conflict)
        send(f"PAPER — NO REAL CAPITAL\nRefused. {conflict}")
        return
    token = CONFIRM.issue("paper", chat_id, {"capital": capital, "profile": profile})
    audit(chat_id, "/papertrade", "paper", "challenge", "awaiting confirm", challenge=token)
    send(
        "PAPER — NO REAL CAPITAL\n"
        f"Start paper ${capital:,.2f} profile {profile}\n"
        f"Confirm with /confirm_paper {token}"
    )


def confirm_paper(chat_id: str, token: str, send: SendFn) -> None:
    try:
        payload = CONFIRM.take("paper", chat_id, token)
    except ConfirmError as exc:
        audit(chat_id, "/confirm_paper", "paper", "refused", str(exc), challenge=token)
        send(f"Refused. {exc}")
        return
    conflict = _conflict(detect_modes())
    if conflict:
        audit(chat_id, "/confirm_paper", "paper", "refused", conflict, challenge=token)
        send(f"PAPER — NO REAL CAPITAL\nRefused. {conflict}")
        return
    try:
        result = start_paper_round(require_finite(float(payload["capital"])), str(payload["profile"]))
    except Exception as exc:
        audit(chat_id, "/confirm_paper", "paper", "refused", str(exc), challenge=token)
        send(f"PAPER — NO REAL CAPITAL\nCould not start: {exc}")
        return
    audit(chat_id, "/confirm_paper", "paper", "accepted", "start_paper_round", challenge=token, state="PAPER")
    send(
        "PAPER — NO REAL CAPITAL\n"
        f"Session {result.get('session_tag')} profile {result.get('profile')} "
        f"capital ${float(result.get('capital') or 0):,.2f}"
    )


def request_shadow(chat_id: str, args: list[str], send: SendFn) -> None:
    if len(args) < 2:
        send("SHADOW — NO CAPITAL\nUsage: /shadow <new-session-id> <starting-balance>")
        return
    session_id = args[0].strip()
    if session_id in FORBIDDEN_SHADOW_IDS or ".." in session_id or "/" in session_id or "\\" in session_id:
        audit(chat_id, "/shadow", "shadow", "refused", "session id rejected")
        send("SHADOW — NO CAPITAL\nRefused. That session id cannot be used.")
        return
    try:
        balance = require_finite(float(args[1]))
    except ValueError:
        send("SHADOW — NO CAPITAL\nStarting balance must be a finite number.")
        return
    if balance <= 0:
        send("SHADOW — NO CAPITAL\nStarting balance must be positive.")
        return
    target = SHADOW_ROOT / session_id
    if target.exists():
        audit(chat_id, "/shadow", "shadow", "refused", "session exists")
        send("SHADOW — NO CAPITAL\nRefused. That session already exists. Pick a new id.")
        return
    conflict = _conflict(detect_modes())
    if conflict:
        audit(chat_id, "/shadow", "shadow", "refused", conflict)
        send(f"SHADOW — NO CAPITAL\nRefused. {conflict}")
        return
    sha = _git_sha()
    token = CONFIRM.issue("shadow", chat_id, {
        "session_id": session_id, "balance": balance, "sha": sha,
    })
    audit(chat_id, "/shadow", "shadow", "challenge", session_id, challenge=token)
    send(
        "SHADOW — NO CAPITAL\n"
        f"Session {session_id}\n"
        f"Code SHA {sha}\n"
        f"Starting balance ${balance:,.2f}\n"
        "Sizing basis realized_gross_equity (new session only)\n"
        f"Confirm with /confirm_shadow {token}"
    )


def confirm_shadow(chat_id: str, token: str, send: SendFn) -> None:
    try:
        payload = CONFIRM.take("shadow", chat_id, token)
    except ConfirmError as exc:
        audit(chat_id, "/confirm_shadow", "shadow", "refused", str(exc), challenge=token)
        send(f"Refused. {exc}")
        return
    session_id = str(payload["session_id"])
    try:
        payload["balance"] = require_finite(float(payload["balance"]))
    except (TypeError, ValueError):
        audit(chat_id, "/confirm_shadow", "shadow", "refused", "non-finite balance", challenge=token)
        send("SHADOW — NO CAPITAL\nRefused. Starting balance is not finite.")
        return
    if payload["balance"] <= 0:
        send("SHADOW — NO CAPITAL\nRefused. Starting balance must be positive.")
        return
    if session_id in FORBIDDEN_SHADOW_IDS or (SHADOW_ROOT / session_id).exists():
        audit(chat_id, "/confirm_shadow", "shadow", "refused", "session exists", challenge=token)
        send("SHADOW — NO CAPITAL\nRefused. Session already exists.")
        return
    conflict = _conflict(detect_modes())
    if conflict:
        audit(chat_id, "/confirm_shadow", "shadow", "refused", conflict, challenge=token)
        send(f"SHADOW — NO CAPITAL\nRefused. {conflict}")
        return
    cmd = [
        sys.executable, str(PROJECT_ROOT / "run_shadow.py"),
        "--session-id", session_id,
        "--session-tag", session_id,
        "--code-sha", str(payload["sha"]),
        "--new",
        "--starting-balance", str(payload["balance"]),
        "--root", "shadow_data",
    ]
    from kalshi_bot.process_ownership import record_ownership, refuse_if_conflict, start_lock
    with start_lock():
        conflict = refuse_if_conflict("shadow")
        if conflict:
            audit(chat_id, "/confirm_shadow", "shadow", "refused", conflict, challenge=token)
            send(f"SHADOW — NO CAPITAL\nRefused. {conflict}")
            return
        pid = _spawn(cmd)
        try:
            record_ownership("shadow", pid, session_id)
        except Exception as exc:
            audit(chat_id, "/confirm_shadow", "shadow", "uncertain", str(exc), challenge=token)
            send(f"SHADOW — NO CAPITAL\nSpawned pid {pid} but ownership was not verified: {exc}")
            return
    audit(chat_id, "/confirm_shadow", "shadow", "accepted", session_id, challenge=token, state="SHADOW")
    send(
        "SHADOW — NO CAPITAL\n"
        f"Session {session_id}\n"
        f"Code SHA {payload['sha']}\n"
        f"Starting balance ${float(payload['balance']):,.2f}\n"
        "Sizing basis realized_gross_equity\n"
        f"Process {pid}"
    )


def _parse_pct(raw: str) -> float:
    value = float(raw.strip().rstrip("%"))
    if value > 1.0:
        value /= 100.0
    return value


def _current_setting(field_name: str) -> Any:
    live = load_live_config()
    if field_name in live and live[field_name] is not None:
        return live[field_name]
    return getattr(cfg, field_name)


def request_setting(chat_id: str, action: str, args: list[str], send: SendFn) -> None:
    rule = SETTING_RULES[action]
    if not args:
        current = _current_setting(rule["field"])
        send(f"{action} current {current}. Usage: /{action} <value>. Applies when the trader polls commands.")
        return
    try:
        if rule["kind"] == "int":
            proposed = int(require_finite(float(args[0])))
        elif rule["kind"] == "usd":
            proposed = require_finite(float(args[0]))
        else:
            proposed = require_finite(_parse_pct(args[0]))
    except ValueError:
        send("Could not parse a finite value.")
        return
    from kalshi_bot.runtime_control import setting_bounds
    modes = detect_modes()
    if modes.get("ambiguous") or modes.get("ownership") == "unknown":
        send("Refused. Trader mode is not known, so the setting was not queued.")
        return
    live = bool(modes.get("live"))
    lo, hi = setting_bounds(action, live=live)
    if not lo <= proposed <= hi:
        audit(chat_id, "/" + action, action, "refused", "out of range")
        send(f"Refused. {rule['field']} must be between {lo} and {hi}.")
        return
    current = _current_setting(rule["field"])
    token = CONFIRM.issue(action, chat_id, {"value": proposed, "field": rule["field"]})
    audit(chat_id, "/" + action, action, "challenge", "awaiting confirm", challenge=token)
    send(
        f"{rule['field']}\n"
        f"Current {current}\n"
        f"Proposed {proposed}\n"
        "Applies on the running trader's next command poll. Not a file edit.\n"
        f"Confirm with /confirm_set {token}"
    )


def confirm_setting(chat_id: str, token: str, send: SendFn) -> None:
    matched = None
    pending_op = ""
    for action in SETTING_RULES:
        try:
            payload = CONFIRM.take(action, chat_id, token)
            matched = payload
            pending_op = action
            break
        except ConfirmError as exc:
            if str(exc) != "challenge is for a different operation" and str(exc) != "unknown challenge":
                audit(chat_id, "/confirm_set", action, "refused", str(exc), challenge=token)
                send(f"Refused. {exc}")
                return
    if matched is None:
        audit(chat_id, "/confirm_set", "setting", "refused", "unknown challenge", challenge=token)
        send("Refused. unknown challenge")
        return
    rule = SETTING_RULES[pending_op]
    try:
        value = require_finite(float(matched["value"]))
    except (TypeError, ValueError):
        audit(chat_id, "/confirm_set", pending_op, "refused", "non-finite", challenge=token)
        send("Refused. Value is not finite.")
        return
    from kalshi_bot.runtime_control import setting_bounds
    modes = detect_modes()
    if modes.get("ambiguous"):
        audit(chat_id, "/confirm_set", pending_op, "refused", "mode unknown", challenge=token)
        send("Refused. Trader mode is not known.")
        return
    lo, hi = setting_bounds(pending_op, live=bool(modes.get("live")))
    if not lo <= value <= hi:
        audit(chat_id, "/confirm_set", pending_op, "refused", "out of range", challenge=token)
        send("Refused. Value is outside the trader's allowed range.")
        return
    enqueue_bot_command(pending_op, value=value, source="telegram_confirm")
    audit(chat_id, "/confirm_set", pending_op, "queued", str(value), challenge=token)
    send(f"Queued but not yet applied: {rule['field']}={value}. Independent halts were not cleared.")


def request_bankroll(chat_id: str, args: list[str], send: SendFn) -> None:
    from kalshi_bot.bankroll import trading_bankroll_cap
    if args and args[0].lower() == "clear":
        token = CONFIRM.issue("bankroll", chat_id, {"amount": None})
        audit(chat_id, "/bankroll", "bankroll", "challenge", "clear", challenge=token)
        send(f"Clear trading bankroll (now { _sizing_line() }).\nConfirm with /confirm_bankroll {token}")
        return
    try:
        amount = require_finite(float(args[0]))
    except (ValueError, IndexError):
        send("Usage: /bankroll <dollars> or /bankroll clear")
        return
    if amount <= 0:
        send("Refused. Trading bankroll must be positive.")
        return
    current = trading_bankroll_cap()
    token = CONFIRM.issue("bankroll", chat_id, {"amount": amount})
    audit(chat_id, "/bankroll", "bankroll", "challenge", f"{amount}", challenge=token)
    send(
        f"Trading bankroll current {('unset' if current is None else f'${current:,.2f}')}\n"
        f"Proposed ${amount:,.2f}\n"
        "This caps live sizing. It does not move Kalshi cash.\n"
        f"Confirm with /confirm_bankroll {token}"
    )


def confirm_bankroll(chat_id: str, token: str, send: SendFn) -> None:
    from kalshi_bot.bankroll import set_trading_bankroll
    try:
        payload = CONFIRM.take("bankroll", chat_id, token)
    except ConfirmError as exc:
        audit(chat_id, "/confirm_bankroll", "bankroll", "refused", str(exc), challenge=token)
        send(f"Refused. {exc}")
        return
    amount = payload.get("amount")
    if amount is not None:
        try:
            amount = require_finite(float(amount))
        except ValueError:
            send("Refused. Amount is not finite.")
            return
        if amount <= 0:
            send("Refused. Trading bankroll must be positive.")
            return
    set_trading_bankroll(amount)
    audit(chat_id, "/confirm_bankroll", "bankroll", "accepted", str(amount), challenge=token)
    send("Trading bankroll cleared. Live sizes from tradeable cash." if amount is None else f"Trading bankroll set to ${amount:,.2f}. Kelly math was not changed.")


def request_profile_change(chat_id: str, name: str, send: SendFn) -> None:
    from kalshi_bot.bankroll import canonical_profile
    key = canonical_profile(name)
    if key not in PROFILE_PRESETS:
        send(f"Unknown profile. Allowed: {', '.join(PROFILE_PRESETS)}")
        return
    current = load_live_config().get("CONFIG_PROFILE") or cfg.CONFIG_PROFILE
    token = CONFIRM.issue("profile", chat_id, {"name": key})
    audit(chat_id, "/profile", "profile", "challenge", key, challenge=token)
    send(
        f"Profile current {current}\n"
        f"Proposed {key}\n"
        "Applies on the running trader's next command poll.\n"
        f"Confirm with /confirm_profile {token}"
    )


def confirm_profile(chat_id: str, token: str, send: SendFn) -> None:
    try:
        payload = CONFIRM.take("profile", chat_id, token)
    except ConfirmError as exc:
        audit(chat_id, "/confirm_profile", "profile", "refused", str(exc), challenge=token)
        send(f"Refused. {exc}")
        return
    name = str(payload["name"])
    if name not in PROFILE_PRESETS:
        audit(chat_id, "/confirm_profile", "profile", "refused", "unknown profile", challenge=token)
        send("Refused. Profile is not allowed.")
        return
    enqueue_bot_command("profile", name=name, source="telegram_confirm")
    audit(chat_id, "/confirm_profile", "profile", "accepted", name, challenge=token)
    send(f"Queued profile {name}.")


def request_take_cash(chat_id: str, args: list[str], send: SendFn) -> None:
    if not args:
        send("Usage: /take_cash <usd>")
        return
    try:
        amount = require_finite(float(args[0]))
    except ValueError:
        send("Amount must be a finite number.")
        return
    if amount <= 0:
        send("Amount must be > 0")
        return
    safety = _sim_safety()
    balance = safety["balance"]
    vault = float(safety["vault_balance"] or 0)
    projected = None if balance is None else float(balance) - amount
    if projected is not None and projected < 0:
        audit(chat_id, "/take_cash", "take_cash", "refused", "exceeds balance")
        send(f"Refused. ${amount:,.2f} exceeds trading balance ${float(balance):,.2f}.")
        return
    token = CONFIRM.issue("take_cash", chat_id, {"amount": amount})
    audit(chat_id, "/take_cash", "take_cash", "challenge", f"{amount}", challenge=token)
    bal_txt = "unknown" if balance is None else f"${float(balance):,.2f}"
    proj_txt = "unknown" if projected is None else f"${projected:,.2f}"
    send(
        f"Trading balance {bal_txt}\n"
        f"Vault ${vault:,.2f}\n"
        f"Move ${amount:,.2f} trading -> vault\n"
        f"Projected trading balance {proj_txt}\n"
        f"Projected vault ${vault + amount:,.2f}\n"
        f"Confirm with /confirm_take_cash {token}"
    )


def confirm_take_cash(chat_id: str, token: str, send: SendFn) -> None:
    try:
        payload = CONFIRM.take("take_cash", chat_id, token)
    except ConfirmError as exc:
        audit(chat_id, "/confirm_take_cash", "take_cash", "refused", str(exc), challenge=token)
        send(f"Refused. {exc}")
        return
    try:
        amount = require_finite(float(payload["amount"]))
    except ValueError:
        audit(chat_id, "/confirm_take_cash", "take_cash", "refused", "non-finite", challenge=token)
        send("Refused. Amount is not finite.")
        return
    safety = _sim_safety()
    balance = safety["balance"]
    if balance is not None and amount > float(balance):
        audit(chat_id, "/confirm_take_cash", "take_cash", "refused", "exceeds balance", challenge=token)
        send("Refused. Balance changed and no longer covers that amount.")
        return
    enqueue_take_cash(amount, reason="telegram_confirm")
    audit(chat_id, "/confirm_take_cash", "take_cash", "accepted", f"{amount}", challenge=token)
    send(f"Queued take_cash ${amount:,.2f}. It moves simulator cash into the vault. It does not withdraw from Kalshi.")


def request_vault_auto(chat_id: str, args: list[str], send: SendFn) -> None:
    current = load_vault_config()
    if not args or args[0].lower() == "status":
        send(
            f"Vault auto {'ON' if current.auto_enabled else 'OFF'}\n"
            f"Trigger ${current.profit_trigger:,.2f} skim ${current.skim_amount:,.2f}"
        )
        return
    choice = args[0].lower()
    if choice not in ("on", "off"):
        send("Usage: /vault_auto status|on|off")
        return
    token = CONFIRM.issue("vault_auto", chat_id, {"enabled": choice == "on"})
    audit(chat_id, "/vault_auto", "vault_auto", "challenge", choice, challenge=token)
    send(
        f"Vault auto current {'ON' if current.auto_enabled else 'OFF'}\n"
        f"Proposed {choice.upper()}\n"
        f"Confirm with /confirm_vault {token}"
    )


def request_vault_set(chat_id: str, args: list[str], send: SendFn) -> None:
    if len(args) < 2:
        send("Usage: /vault_set <profit_trigger> <skim_amount>")
        return
    try:
        trigger = require_finite(float(args[0]))
        skim = require_finite(float(args[1]))
    except ValueError:
        send("Vault values must be finite numbers.")
        return
    if trigger < 1 or skim < 0:
        send("Refused. Trigger must be >= 1 and skim must be >= 0.")
        return
    current = load_vault_config()
    token = CONFIRM.issue("vault_set", chat_id, {"trigger": trigger, "skim": skim})
    audit(chat_id, "/vault_set", "vault_set", "challenge", "awaiting confirm", challenge=token)
    send(
        f"Trigger ${current.profit_trigger:,.2f} -> ${trigger:,.2f}\n"
        f"Skim ${current.skim_amount:,.2f} -> ${skim:,.2f}\n"
        f"Confirm with /confirm_vault {token}"
    )


def confirm_vault(chat_id: str, token: str, send: SendFn) -> None:
    payload = None
    operation = ""
    for operation in ("vault_auto", "vault_set"):
        try:
            payload = CONFIRM.take(operation, chat_id, token)
            break
        except ConfirmError as exc:
            if str(exc) not in ("unknown challenge", "challenge is for a different operation"):
                audit(chat_id, "/confirm_vault", operation, "refused", str(exc), challenge=token)
                send(f"Refused. {exc}")
                return
            payload = None
    if payload is None:
        audit(chat_id, "/confirm_vault", "vault", "refused", "unknown challenge", challenge=token)
        send("Refused. unknown challenge")
        return
    current = load_vault_config()
    if operation == "vault_auto":
        updated = VaultConfig(bool(payload["enabled"]), current.profit_trigger, current.skim_amount)
    else:
        updated = VaultConfig(current.auto_enabled, float(payload["trigger"]), float(payload["skim"]))
    enqueue_set_config(updated.clamp())
    audit(chat_id, "/confirm_vault", operation, "accepted", operation, challenge=token)
    send(
        f"Vault auto {'ON' if updated.auto_enabled else 'OFF'}, "
        f"trigger ${updated.profit_trigger:,.2f}, skim ${updated.skim_amount:,.2f}"
    )
