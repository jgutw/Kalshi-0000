"""
vault.py — Profit skim / "take cash".

Trading balance stays in SimState.balance (used for sizing).
Taken profits move to vault_balance (not used for new trades).

Auto rule (example): when trading profit >= $200, skim $100 into the vault.
Settings live in logs/vault_config.json (dashboard writes; bot reads).
Manual takes enqueue logs/vault_commands.jsonl for the bot to apply safely.

Live mode: Kalshi has no sub-accounts, so a vault is a soft reservation, not
custody. LiveGuard subtracts vault_balance from synced Kalshi cash so the bot
cannot size against it, but the dollars remain in your Kalshi account and are
still exposed to anything that bypasses the bot (manual trades, another client,
a bug). The only hard vault is a withdrawal to your bank.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .sim_state import SimState

log = logging.getLogger("kalshi_bot.vault")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
CONFIG_PATH = LOGS_DIR / "vault_config.json"
COMMANDS_PATH = LOGS_DIR / "vault_commands.jsonl"
SKIMS_PATH = LOGS_DIR / "vault_skims.jsonl"


@dataclass
class VaultConfig:
    auto_enabled: bool = False
    profit_trigger: float = 200.0   # skim when (balance - starting) >= this
    skim_amount: float = 100.0      # dollars moved to vault per trigger hit

    def clamp(self) -> "VaultConfig":
        self.profit_trigger = max(1.0, float(self.profit_trigger))
        self.skim_amount = max(0.0, float(self.skim_amount))
        return self


def load_vault_config() -> VaultConfig:
    if not CONFIG_PATH.exists():
        return VaultConfig()
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = VaultConfig(
            auto_enabled=bool(raw.get("auto_enabled", False)),
            profit_trigger=float(raw.get("profit_trigger", 200.0)),
            skim_amount=float(raw.get("skim_amount", 100.0)),
        )
        return cfg.clamp()
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
        log.warning("vault_config read failed: %s", e)
        return VaultConfig()


def save_vault_config(cfg: VaultConfig) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    cfg = cfg.clamp()
    CONFIG_PATH.write_text(
        json.dumps({**asdict(cfg), "updated_at": datetime.now(timezone.utc).isoformat()}, indent=2),
        encoding="utf-8",
    )


def enqueue_take_cash(amount: float, reason: str = "manual") -> None:
    """Dashboard → bot command (avoids racing SimState.save)."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": "take_cash",
        "amount": float(amount),
        "reason": reason,
    }
    with open(COMMANDS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(cmd) + "\n")


def enqueue_set_config(cfg: VaultConfig) -> None:
    save_vault_config(cfg)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": "set_config",
        "auto_enabled": cfg.auto_enabled,
        "profit_trigger": cfg.profit_trigger,
        "skim_amount": cfg.skim_amount,
    }
    with open(COMMANDS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(cmd) + "\n")


def _append_skim(event: dict) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(SKIMS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except OSError as e:
        log.warning("vault skim log failed: %s", e)


def load_recent_skims(n: int = 20) -> list[dict]:
    if not SKIMS_PATH.exists():
        return []
    try:
        lines = [l for l in SKIMS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))


def process_vault_commands(sim: "SimState") -> bool:
    """Apply queued dashboard commands. Returns True if sim was mutated."""
    if not COMMANDS_PATH.exists():
        return False
    try:
        raw = COMMANDS_PATH.read_text(encoding="utf-8")
    except OSError:
        return False
    if not raw.strip():
        return False

    changed = False
    kept: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = cmd.get("action")
        if action == "take_cash":
            amt = float(cmd.get("amount") or 0)
            reason = str(cmd.get("reason") or "manual")
            moved = sim.take_cash(amt, reason=reason)
            if moved > 0:
                changed = True
        elif action == "set_config":
            save_vault_config(
                VaultConfig(
                    auto_enabled=bool(cmd.get("auto_enabled", False)),
                    profit_trigger=float(cmd.get("profit_trigger", 200.0)),
                    skim_amount=float(cmd.get("skim_amount", 100.0)),
                )
            )
            # config-only; still clear the command
        else:
            kept.append(line)

    try:
        if kept:
            COMMANDS_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            COMMANDS_PATH.write_text("", encoding="utf-8")
    except OSError as e:
        log.warning("vault commands cleanup failed: %s", e)

    return changed
