"""
session_meta.py — Tag and document each paper-trading round.

Writes logs/session_meta.json on bot startup with config levels, session chain,
and the last trade from the prior archived round.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import ASSETS, cfg

log = logging.getLogger("kalshi_bot.session_meta")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
SESSIONS_DIR = PROJECT_ROOT / "sessions"
META_PATH = LOGS_DIR / "session_meta.json"

# Log artifacts copied when archiving a round.
ARCHIVE_LOG_FILES = (
    "kalshi_sim.json",
    "kalshi_trades.jsonl",
    "kalshi_decisions.jsonl",
    "kalshi_events.jsonl",
    "kalshi_features.jsonl",
    "session_meta.json",
    "near_misses.csv",
    "vault_config.json",
    "vault_skims.jsonl",
)


def _load_jsonl_trades(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _last_real_trade(trades_path: Path) -> Optional[dict]:
    real = [t for t in _load_jsonl_trades(trades_path) if t.get("side")]
    return real[-1] if real else None


def _latest_archive() -> Optional[Path]:
    if not SESSIONS_DIR.exists():
        return None
    candidates = [
        p for p in SESSIONS_DIR.iterdir()
        if p.is_dir() and (p / "kalshi_trades.jsonl").exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _config_levels() -> dict[str, Any]:
    """Snapshot of tunable thresholds for this round."""
    return {
        "profile": cfg.CONFIG_PROFILE,
        "KELLY_FRACTION": cfg.KELLY_FRACTION,
        "MIN_EDGE_PCT": cfg.MIN_EDGE_PCT,
        "MAX_POS_PCT": cfg.MAX_POS_PCT,
        "MIN_ENTRY_PRICE": getattr(cfg, "MIN_ENTRY_PRICE", 0.0),
        "MAX_ENTRY_PRICE": getattr(cfg, "MAX_ENTRY_PRICE", 1.0),
        "LAG_CONFIDENCE_MIN": cfg.LAG_CONFIDENCE_MIN,
        "CWM_MIN": cfg.CWM_MIN,
        "ALPHA_EDGE_ENABLED": cfg.ALPHA_EDGE_ENABLED,
        "ALPHA_EDGE_MIN": cfg.ALPHA_EDGE_MIN,
        "ALPHA_EDGE_BAND_LOW": cfg.ALPHA_EDGE_BAND_LOW,
        "ALPHA_EDGE_LAG_MIN": cfg.ALPHA_EDGE_LAG_MIN,
        "LAG_ABSENT_MIN": cfg.LAG_ABSENT_MIN,
        "P_BASE_CENTER_MIN": cfg.P_BASE_CENTER_MIN,
        "PTB_CAPTURE_SECS": cfg.PTB_CAPTURE_SECS,
        "SKIP_OPEN_SECS": cfg.SKIP_OPEN_SECS,
        "SKIP_CLOSE_SECS": cfg.SKIP_CLOSE_SECS,
        "VOL_HI": cfg.VOL_HI,
        "VOL_MID": cfg.VOL_MID,
        "SPOT_CONFIDENCE_MIN": cfg.SPOT_CONFIDENCE_MIN,
        "MIN_CONVICTION": cfg.MIN_CONVICTION,
        "SHARPE_MIN": cfg.SHARPE_MIN,
        "SHARPE_MIN_TRADES": cfg.SHARPE_MIN_TRADES,
        "MAX_CONSEC_LOSSES": cfg.MAX_CONSEC_LOSSES,
        "COOLDOWN_MINUTES": cfg.COOLDOWN_MINUTES,
        "PER_ASSET_CIRCUIT_BREAKER": cfg.PER_ASSET_CIRCUIT_BREAKER,
        "MAX_DAILY_LOSS_PCT": cfg.MAX_DAILY_LOSS_PCT,
        "MAX_DRAWDOWN_PCT": getattr(cfg, "MAX_DRAWDOWN_PCT", 0.25),
        "DRAWDOWN_HALT_ENABLED": getattr(cfg, "DRAWDOWN_HALT_ENABLED", True),
        "DRAWDOWN_USE_EQUITY": getattr(cfg, "DRAWDOWN_USE_EQUITY", True),
        "ACTIVITY_MANDATE_ENABLED": getattr(cfg, "ACTIVITY_MANDATE_ENABLED", True),
        "ACTIVITY_IDLE_SECS": getattr(cfg, "ACTIVITY_IDLE_SECS", 3600.0),
        "EARLY_EXIT_ENABLED": cfg.EARLY_EXIT_ENABLED,
        "P_BASE_MIN": cfg.P_BASE_MIN,
        "P_BASE_MAX": cfg.P_BASE_MAX,
        "DRY_RUN": cfg.DRY_RUN,
        "SIM_BALANCE": cfg.SIM_BALANCE,
        "assets_enabled": [a.symbol for a in ASSETS if a.enabled],
        "optimizations": {
            "XRP_disabled": not any(a.symbol == "XRP" and a.enabled for a in ASSETS),
            "note": (
                "disciplined_paper_v2: entry band [MIN,MAX], equity DD halt, "
                "vault reduces peak_balance on skim, 1h activity mandate — PAPER ONLY"
            ),
        },
    }


def _archive_summary(archive_dir: Path) -> dict[str, Any]:
    archive_dir = archive_dir.resolve()
    sim_path = archive_dir / "kalshi_sim.json"
    trades_path = archive_dir / "kalshi_trades.jsonl"
    sim = {}
    if sim_path.exists():
        try:
            sim = json.loads(sim_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    real = [t for t in _load_jsonl_trades(trades_path) if t.get("side")]
    start = float(sim.get("starting_balance", cfg.SIM_BALANCE))
    balance = float(sim.get("balance", start))
    return {
        "archive_dir": str(archive_dir.relative_to(PROJECT_ROOT)),
        "starting_balance": start,
        "ending_balance": balance,
        "pnl": round(balance - start, 2),
        "real_trades": len(real),
        "wins": sum(1 for t in real if float(t.get("pnl", 0)) > 0),
        "losses": sum(1 for t in real if float(t.get("pnl", 0)) <= 0),
    }


def write_session_meta(
    session_tag: str,
    previous_archive: Optional[Path] = None,
) -> dict[str, Any]:
    """
    Write logs/session_meta.json. Returns the meta dict.
    previous_archive: explicit prior session folder; else latest under sessions/.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    prev = previous_archive or _latest_archive()
    prev_summary = _archive_summary(prev) if prev else None
    last_trade = _last_real_trade(prev / "kalshi_trades.jsonl") if prev else None

    meta = {
        "session_tag": session_tag,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "starting_balance": cfg.SIM_BALANCE,
        "config_levels": _config_levels(),
        "previous_session": prev_summary,
        "anchored_to_last_paper_trade": last_trade,
    }

    META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info(
        "Session tag=%s | profile=%s | prev=%s | last_trade=%s %s pnl=%s",
        session_tag,
        cfg.CONFIG_PROFILE,
        prev_summary["archive_dir"] if prev_summary else "none",
        last_trade.get("asset") if last_trade else "?",
        last_trade.get("side") if last_trade else "?",
        last_trade.get("pnl") if last_trade else "?",
    )
    return meta


def archive_current_logs(session_tag: str) -> Path:
    """
    Copy logs/* into sessions/session_YYYY-MM-DD_HHMM/ and tag the archive.
    Returns the archive directory (even if logs were empty).
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    archive_dir = SESSIONS_DIR / f"session_{stamp}"
    archive_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for name in ARCHIVE_LOG_FILES:
        src = LOGS_DIR / name
        if src.exists():
            shutil.copy2(src, archive_dir / name)
            copied += 1

    if copied:
        tag_archived_session(archive_dir, session_tag)
        log.info("Archived %d log file(s) → %s", copied, archive_dir)
    else:
        log.info("No log files to archive under %s", LOGS_DIR)

    return archive_dir


def reset_logs_for_fresh_round() -> None:
    """Clear trade/decision logs and write a fresh sim state at SIM_BALANCE."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    for name in (
        "kalshi_trades.jsonl",
        "kalshi_decisions.jsonl",
        "kalshi_events.jsonl",
        "kalshi_features.jsonl",
        "near_misses.csv",
        "session_meta.json",
    ):
        path = LOGS_DIR / name
        if path.exists():
            path.unlink()

    from .sim_state import SimState

    sim = SimState()
    sim.save()
    log.info("Fresh round: sim reset to $%.2f", cfg.SIM_BALANCE)


def prepare_fresh_round(archive_tag: str) -> Path:
    """Archive current logs, then reset for a new paper-trading round."""
    archive_dir = archive_current_logs(archive_tag)
    reset_logs_for_fresh_round()
    return archive_dir


def tag_archived_session(archive_dir: Path, session_tag: str) -> None:
    """Write session_meta.json into an archive folder (retroactive tagging)."""
    trades_path = archive_dir / "kalshi_trades.jsonl"
    last_trade = _last_real_trade(trades_path)
    sim = {}
    sim_path = archive_dir / "kalshi_sim.json"
    if sim_path.exists():
        try:
            sim = json.loads(sim_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    config_snapshot = _config_levels()
    src_meta = archive_dir / "session_meta.json"
    if src_meta.exists():
        try:
            prior = json.loads(src_meta.read_text(encoding="utf-8"))
            config_snapshot = prior.get("config_levels", config_snapshot)
        except (json.JSONDecodeError, OSError):
            pass

    meta = {
        "session_tag": session_tag,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "summary": _archive_summary(archive_dir),
        "last_paper_trade": last_trade,
        "config_levels_at_archive": config_snapshot,
    }
    (archive_dir / "session_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
