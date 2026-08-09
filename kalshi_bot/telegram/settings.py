"""Telegram bridge settings from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class TelegramSettings:
    token: str
    chat_id: str
    enabled: bool = True
    poll_timeout_secs: int = 25
    alert_poll_secs: float = 3.0

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id and self.enabled)


def load_settings() -> TelegramSettings:
    enabled_raw = os.getenv("TELEGRAM_ENABLED", "true").strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    return TelegramSettings(
        token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        enabled=enabled,
        poll_timeout_secs=int(os.getenv("TELEGRAM_POLL_TIMEOUT", "25")),
        alert_poll_secs=float(os.getenv("TELEGRAM_ALERT_POLL_SECS", "3")),
    )
