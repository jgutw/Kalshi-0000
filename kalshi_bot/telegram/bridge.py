"""
Telegram bridge process.

Run:  python run_telegram_bridge.py

Long-polls Telegram for commands (allowlisted chat) and pushes trade/halt/vault alerts.
Mutating actions are queued to logs/ for the trading bot (same pattern as vault).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Optional

from .alerts import AlertWatcher
from .api import TelegramAPI
from .handlers import handle_command
from .settings import TelegramSettings, load_settings

log = logging.getLogger("kalshi_bot.telegram.bridge")


class TelegramBridge:
    def __init__(self, settings: TelegramSettings):
        self.settings = settings
        self.api = TelegramAPI(settings.token)
        self._offset: Optional[int] = None
        self._stop = threading.Event()
        self._alerts = AlertWatcher()

    def send(self, text: str) -> None:
        self.api.send_message(self.settings.chat_id, text)

    def _allowed(self, chat_id) -> bool:
        return str(chat_id) == str(self.settings.chat_id)

    def poll_commands_once(self) -> None:
        updates = self.api.get_updates(self._offset, timeout=self.settings.poll_timeout_secs)
        for upd in updates:
            upd_id = int(upd.get("update_id", 0))
            self._offset = upd_id + 1
            msg = upd.get("message") or {}
            chat = (msg.get("chat") or {}).get("id")
            text = msg.get("text") or ""
            if chat is None:
                continue
            if not self._allowed(chat):
                log.warning("Ignored message from unauthorized chat_id=%s", chat)
                self.api.send_message(str(chat), "Unauthorized.")
                continue
            if not text:
                continue
            log.info("Telegram cmd chat accepted")
            handle_command(text, self.send, chat_id=str(chat))

    def alert_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._alerts.poll(self.send)
            except Exception:
                log.exception("alert poll failed")
            self._stop.wait(self.settings.alert_poll_secs)

    def run_forever(self) -> None:
        log.info("Telegram bridge starting (chat_id=%s)", self.settings.chat_id)
        self.send(
            "Kalshi Telegram bridge online.\n"
            "Send /help for commands. Alerts: trade close, halt, vault skim."
        )
        t = threading.Thread(target=self.alert_loop, name="telegram-alerts", daemon=True)
        t.start()
        try:
            while not self._stop.is_set():
                try:
                    self.poll_commands_once()
                except Exception:
                    log.exception("command poll failed")
                    time.sleep(3)
        finally:
            self._stop.set()


def run_bridge(settings: Optional[TelegramSettings] = None) -> None:
    settings = settings or load_settings()
    if not settings.configured:
        raise SystemExit(
            "Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env "
            "(and TELEGRAM_ENABLED=true)."
        )
    TelegramBridge(settings).run_forever()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        run_bridge()
    except KeyboardInterrupt:
        log.info("Telegram bridge stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
