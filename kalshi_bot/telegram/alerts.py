"""Push alerts by watching log files (trades, vault skims, halt flips, bot heartbeat)."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from dashboard.data.loaders import load_portfolio

from .formatters import (
    format_archive_notice,
    format_halt_alert,
    format_skim_alert,
    format_trade_alert,
)

log = logging.getLogger("kalshi_bot.telegram.alerts")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
TRADES_PATH = LOGS_DIR / "kalshi_trades.jsonl"
SKIMS_PATH = LOGS_DIR / "vault_skims.jsonl"
NOTICES_PATH = LOGS_DIR / "telegram_notices.jsonl"
HEARTBEAT_PATH = LOGS_DIR / "bot_heartbeat.json"
SESSION_META_PATH = LOGS_DIR / "session_meta.json"
# Alert if an *active* round's heartbeat is older than this
HEARTBEAT_STALE_SECS = 90.0
HEARTBEAT_ALERT_COOLDOWN_SECS = 300.0

SendFn = Callable[[str], None]


class AlertWatcher:
    def __init__(self) -> None:
        self._trade_pos = self._size(TRADES_PATH)
        self._skim_pos = self._size(SKIMS_PATH)
        self._notice_pos = self._size(NOTICES_PATH)
        port = load_portfolio()
        self._last_halt: Optional[bool] = port.halt_state
        self._last_halt_reason: str = port.halt_reason or ""
        self._heartbeat_alerted = False
        self._last_heartbeat_alert_ts = 0.0

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size if path.exists() else 0
        except OSError:
            return 0

    def poll(self, send: SendFn) -> None:
        from kalshi_bot.operator_control import mode_banner
        banner = mode_banner()

        def tagged(text: str) -> None:
            if banner and not text.startswith(banner.strip()):
                text = banner + text
            send(text)

        self._poll_jsonl(TRADES_PATH, "_trade_pos", self._on_trade, tagged)
        self._poll_jsonl(SKIMS_PATH, "_skim_pos", self._on_skim, tagged)
        self._poll_jsonl(NOTICES_PATH, "_notice_pos", self._on_notice, tagged)
        self._poll_halt(tagged)
        self._poll_heartbeat(tagged)

    def _poll_jsonl(
        self,
        path: Path,
        attr: str,
        handler: Callable[[dict, SendFn], None],
        send: SendFn,
    ) -> None:
        try:
            if not path.exists():
                setattr(self, attr, 0)
                return
            size = path.stat().st_size
            pos = int(getattr(self, attr))
            if size < pos:
                # file truncated / new round
                pos = 0
            if size == pos:
                return
            with open(path, "r", encoding="utf-8") as f:
                f.seek(pos)
                chunk = f.read()
                setattr(self, attr, f.tell())
            for line in chunk.splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                handler(row, send)
        except OSError as e:
            log.warning("alert watch %s failed: %s", path.name, e)

    def _on_trade(self, row: dict, send: SendFn) -> None:
        if not row.get("side"):
            return
        send(format_trade_alert(row))

    def _on_skim(self, row: dict, send: SendFn) -> None:
        send(format_skim_alert(row))

    def _on_notice(self, row: dict, send: SendFn) -> None:
        if row.get("event") == "round_archived":
            send(format_archive_notice(row))

    def _poll_halt(self, send: SendFn) -> None:
        try:
            port = load_portfolio()
        except Exception:
            return
        halted = bool(port.halt_state)
        reason = port.halt_reason or ""
        if self._last_halt is None:
            self._last_halt = halted
            self._last_halt_reason = reason
            return
        if halted != self._last_halt or (halted and reason != self._last_halt_reason):
            send(format_halt_alert(reason, halted))
        self._last_halt = halted
        self._last_halt_reason = reason

    def _session_active(self) -> bool:
        try:
            if not SESSION_META_PATH.exists():
                return False
            meta = json.loads(SESSION_META_PATH.read_text(encoding="utf-8"))
            return bool(meta.get("active"))
        except (OSError, json.JSONDecodeError):
            return False

    def _heartbeat_age_secs(self) -> Optional[float]:
        try:
            if not HEARTBEAT_PATH.exists():
                return None
            data = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
            ts = data.get("ts") or data.get("updated_at")
            if not ts:
                return time.time() - HEARTBEAT_PATH.stat().st_mtime
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - t).total_seconds()
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            return None

    def _poll_heartbeat(self, send: SendFn) -> None:
        """Alert when an active round's bot heartbeat goes stale (Wi‑Fi / crash / power)."""
        if not self._session_active():
            self._heartbeat_alerted = False
            return
        age = self._heartbeat_age_secs()
        if age is None:
            stale = True
            age_s = -1.0
        else:
            stale = age > HEARTBEAT_STALE_SECS
            age_s = age
        now = time.time()
        if stale:
            if (
                not self._heartbeat_alerted
                or (now - self._last_heartbeat_alert_ts) >= HEARTBEAT_ALERT_COOLDOWN_SECS
            ):
                send(
                    "⚠️ BOT HEARTBEAT STALE\n"
                    f"Active session but no heartbeat for "
                    f"{'unknown' if age_s < 0 else f'{age_s:.0f}s'}.\n"
                    "Likely Wi‑Fi drop, crash, or PC sleep/shutdown.\n"
                    "Open positions (if any) still settle on Kalshi — "
                    "check the account, then restart the bot when ready."
                )
                self._heartbeat_alerted = True
                self._last_heartbeat_alert_ts = now
        else:
            if self._heartbeat_alerted:
                send("✅ Bot heartbeat recovered.")
            self._heartbeat_alerted = False
