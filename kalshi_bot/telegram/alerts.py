"""Push alerts by watching log files (trades, vault skims, halt flips)."""

from __future__ import annotations

import json
import logging
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

SendFn = Callable[[str], None]


class AlertWatcher:
    def __init__(self) -> None:
        self._trade_pos = self._size(TRADES_PATH)
        self._skim_pos = self._size(SKIMS_PATH)
        self._notice_pos = self._size(NOTICES_PATH)
        port = load_portfolio()
        self._last_halt: Optional[bool] = port.halt_state
        self._last_halt_reason: str = port.halt_reason or ""

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size if path.exists() else 0
        except OSError:
            return 0

    def poll(self, send: SendFn) -> None:
        self._poll_jsonl(TRADES_PATH, "_trade_pos", self._on_trade, send)
        self._poll_jsonl(SKIMS_PATH, "_skim_pos", self._on_skim, send)
        self._poll_jsonl(NOTICES_PATH, "_notice_pos", self._on_notice, send)
        self._poll_halt(send)

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
