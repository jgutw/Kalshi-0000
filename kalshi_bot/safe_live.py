"""
Safe live start / resume — Kalshi account is source of truth.

Used by Telegram (/resume_live) and CLI. Never sizes from a stale sim file.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .config import cfg
from .kalshi_client import KalshiClient
from .runtime_control import PROFILE_PRESETS, prepare_for_new_round, trading_bot_running
from .session_meta import (
    PROJECT_ROOT,
    archive_and_log_round,
    session_is_active,
)

log = logging.getLogger("kalshi_bot.safe_live")


def _rounds():
    """Lazy import to avoid telegram package circular imports."""
    from kalshi_bot.telegram import rounds as r

    return r


def preflight_account(client: Optional[KalshiClient] = None) -> dict[str, Any]:
    """Read Kalshi cash + open positions (no side effects)."""
    c = client or KalshiClient()
    detail = c.get_balance_detail()
    available = float(detail.get("available") or 0.0)
    portfolio = float(detail.get("portfolio_value") or 0.0)
    positions = []
    for m in c.get_market_positions():
        try:
            pos = float(m.get("position_fp") or 0.0)
        except (TypeError, ValueError):
            pos = 0.0
        if abs(pos) < 1e-9:
            continue
        positions.append(
            {
                "ticker": m.get("ticker"),
                "position": pos,
                "exposure": float(m.get("market_exposure_dollars") or 0.0),
            }
        )
    return {
        "available": available,
        "portfolio": portfolio,
        "total": available + portfolio,
        "open_count": len(positions),
        "opens": positions,
        "bot_running": trading_bot_running(),
        "session_active": session_is_active(),
        "api_ok": bool(detail.get("raw")),
    }


def cleanup_stale_session(source: str = "safe_live_cleanup") -> Optional[dict[str, Any]]:
    """
    If meta says active but the bot process is dead (power/Wi‑Fi death),
    archive and mark idle so a clean live start can proceed.
    """
    if trading_bot_running():
        return None
    if not session_is_active():
        return None
    log.warning("Stale active session with no bot process — archiving")
    return archive_and_log_round(source=source, mark_idle=True)


def start_live_safe(
    profile: str = "max_risk_micro",
    *,
    force_with_opens: bool = False,
    session_tag: Optional[str] = None,
) -> dict[str, Any]:
    """
    Start a LIVE round sized to Kalshi available cash.

    Refuses if:
      - bot already running
      - Kalshi API unreachable
      - available < LIVE_MIN_AVAILABLE_USD
      - open Kalshi positions exist (unless force_with_opens)
    """
    rounds = _rounds()
    profile = rounds.resolve_profile(profile)
    if profile not in PROFILE_PRESETS:
        raise ValueError(f"Unknown profile {profile}")

    if trading_bot_running():
        raise RuntimeError("Bot already running. Send /stop or /pause first.")

    stale = cleanup_stale_session()
    pf = preflight_account()
    if not pf.get("api_ok"):
        raise RuntimeError("Cannot reach Kalshi API — fix network, then retry.")

    available = float(pf["available"])
    opens = list(pf.get("opens") or [])
    min_avail = float(getattr(cfg, "LIVE_MIN_AVAILABLE_USD", 2.0))

    if available < min_avail:
        raise RuntimeError(
            f"Kalshi available ${available:.2f} < min ${min_avail:.2f}. "
            "Fund the account before live."
        )
    if opens and not force_with_opens:
        tickers = ", ".join(str(o.get("ticker")) for o in opens[:6])
        raise RuntimeError(
            f"Kalshi still has {len(opens)} open position(s): {tickers}. "
            "Wait for settlement, or send:\n"
            f"/resume_live {profile} force"
        )

    # Critical: clear leftover /stop queue bits
    prepare_for_new_round(source="safe_live_start")

    n = rounds.next_round_number()
    cap_i = int(round(available))
    tag = session_tag or f"live_{n}_{profile}_{cap_i}"
    # Floor sim-balance to cents available (LiveGuard will re-sync at bootstrap)
    sim_bal = round(available, 2)

    py = sys.executable
    script = str(PROJECT_ROOT / "run_kalshi_bot.py")
    cmd = [
        py,
        script,
        "--mode", "run",
        "--live",
        "--fresh-round",
        "--sim-balance", str(sim_bal),
        "--profile", profile,
        "--session-tag", tag,
    ]
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_path = logs_dir / "bot_stdout.log"
    out_f = open(out_path, "a", encoding="utf-8")
    out_f.write(f"\n\n===== SAFE LIVE start {tag} =====\n")
    out_f.flush()

    kwargs: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdout": out_f,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    log.warning("Spawning SAFE LIVE bot: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, **kwargs)

    ready = False
    for _ in range(30):
        time.sleep(0.4)
        if trading_bot_running() or session_is_active():
            ready = True
            break
        if proc.poll() is not None:
            break

    preset = PROFILE_PRESETS[profile]
    return {
        "ok": ready or proc.poll() is None,
        "pid": proc.pid,
        "session_tag": tag,
        "round": n,
        "capital": sim_bal,
        "profile": profile,
        "kelly": preset.get("KELLY_FRACTION"),
        "max_pos": preset.get("MAX_POS_PCT"),
        "min_trade": preset.get("MIN_TRADE_USD"),
        "ready": ready,
        "exit_code": proc.poll(),
        "log": str(Path(out_path).relative_to(PROJECT_ROOT)),
        "available": available,
        "portfolio": float(pf["portfolio"]),
        "open_count": len(opens),
        "stale_archived": bool(stale),
        "live": True,
        "force_with_opens": force_with_opens,
    }


def format_account(pf: dict[str, Any]) -> str:
    lines = [
        "Kalshi account (source of truth)",
        f"Available: ${float(pf.get('available') or 0):,.2f}",
        f"Open portfolio: ${float(pf.get('portfolio') or 0):,.2f}",
        f"Total: ${float(pf.get('total') or 0):,.2f}",
        f"Open positions: {int(pf.get('open_count') or 0)}",
        f"Bot process: {'RUNNING' if pf.get('bot_running') else 'down'}",
        f"Session meta: {'active' if pf.get('session_active') else 'idle'}",
        f"API: {'ok' if pf.get('api_ok') else 'UNREACHABLE'}",
    ]
    for o in (pf.get("opens") or [])[:8]:
        lines.append(
            f"  · {o.get('ticker')} pos={o.get('position')} "
            f"exp=${float(o.get('exposure') or 0):.2f}"
        )
    if not pf.get("bot_running") and pf.get("session_active"):
        lines.append(
            "\nStale session (PC/Wi‑Fi death). "
            "Use /resume_live to archive + restart safely."
        )
    elif not pf.get("bot_running"):
        lines.append("\nTo start live: /resume_live micro")
    else:
        lines.append("\nControl: /pause  /resume  /stop  /status")
    return "\n".join(lines)


def format_live_start_result(result: dict[str, Any]) -> str:
    lines = [
        "LIVE SAFE START" if result.get("ready") else "LIVE SAFE LAUNCHED",
        f"Tag: {result.get('session_tag')}",
        f"Sized from Kalshi available: ${float(result.get('capital') or 0):,.2f}",
        f"Profile: {result.get('profile')}",
        f"Kelly={result.get('kelly')} max_pos={float(result.get('max_pos') or 0):.0%} "
        f"min_trade=${float(result.get('min_trade') or 0):.0f}",
        f"PID: {result.get('pid')}",
    ]
    if result.get("stale_archived"):
        lines.append("Cleaned stale session from prior crash.")
    if result.get("force_with_opens"):
        lines.append("⚠ Started while Kalshi still had open positions.")
    if result.get("ready"):
        lines.append("Heartbeat OK — LiveGuard will sync Kalshi cash.")
    elif result.get("exit_code") is not None:
        lines.append(
            f"Bot exited early (code {result.get('exit_code')}). Check {result.get('log')}"
        )
    else:
        lines.append("Spawning — wait ~10s then /status or /account")
    lines.append("Remote control: /pause /resume /stop /set_kelly …")
    return "\n".join(lines)
