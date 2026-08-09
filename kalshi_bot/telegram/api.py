"""Thin Telegram Bot API client (HTTP long-poll)."""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

log = logging.getLogger("kalshi_bot.telegram.api")


class TelegramAPI:
    def __init__(self, token: str, timeout: int = 30):
        self.token = token
        self.timeout = timeout
        self.base = f"https://api.telegram.org/bot{token}"

    def _post(self, method: str, payload: dict[str, Any], timeout: Optional[int] = None) -> dict[str, Any]:
        url = f"{self.base}/{method}"
        try:
            resp = requests.post(url, json=payload, timeout=timeout or self.timeout)
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("Telegram %s failed: %s", method, e)
            return {"ok": False, "description": str(e)}
        if not data.get("ok"):
            log.warning("Telegram %s error: %s", method, data.get("description"))
        return data

    def send_message(self, chat_id: str, text: str, disable_preview: bool = True) -> bool:
        # Telegram max message length ~4096
        chunks = _chunk(text, 4000)
        ok_all = True
        for chunk in chunks:
            data = self._post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": disable_preview,
                },
            )
            ok_all = ok_all and bool(data.get("ok"))
        return ok_all

    def get_updates(self, offset: Optional[int], timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        data = self._post("getUpdates", payload, timeout=timeout + 10)
        if not data.get("ok"):
            return []
        return list(data.get("result") or [])


def _chunk(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    parts = []
    while text:
        parts.append(text[:size])
        text = text[size:]
    return parts
