"""
gemini_feed.py — Gemini public WebSocket bookTicker mid feed.

Connects to wss://ws.gemini.com, subscribes to {symbol}@bookTicker,
computes mid from best bid/ask. Mid-only (no microstructure) — 4th venue
for SyntheticSpotEstimator when Binance is geo-blocked.

NEAR is not listed on Gemini; unmapped symbols are skipped by the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Dict, Optional

import websockets

log = logging.getLogger("kalshi_bot.gemini")

GEMINI_WS = "wss://ws.gemini.com"

# Internal symbol → Gemini bookTicker symbol (lowercase, no separator)
SYMBOL_TO_PAIR: Dict[str, str] = {
    "BTC":  "btcusd",
    "ETH":  "ethusd",
    "SOL":  "solusd",
    "XRP":  "xrpusd",
    "DOGE": "dogeusd",
    "BNB":  "bnbusd",
    "HYPE": "hypeusd",
    # NEAR: not listed on Gemini
    "ZEC":  "zecusd",
}

# symbol → last mid
gemini_mids: Dict[str, float] = {}


async def run_gemini(
    symbol: str,
    pair: str,
    on_mid: Optional[Callable[[float], None]] = None,
) -> None:
    """
    Subscribe to Gemini bookTicker for pair. On each update, store mid and
    call on_mid(mid). Reconnect on disconnect.
    """
    channel = f"{pair}@bookTicker"
    log.info(f"[{symbol}] Gemini stream: {channel}")
    while True:
        try:
            async with websockets.connect(GEMINI_WS, ping_interval=20) as ws:
                await ws.send(json.dumps({
                    "method": "SUBSCRIBE",
                    "params": [channel],
                }))
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict) and data.get("error"):
                        log.error(f"[{symbol}] Gemini error: {data.get('error')}")
                        await asyncio.sleep(5)
                        break
                    # bookTicker payload: s, b, a, …
                    if data.get("s") and data.get("s").lower() != pair.lower():
                        continue
                    bid, ask = data.get("b"), data.get("a")
                    if bid is None or ask is None:
                        continue
                    try:
                        b, a = float(bid), float(ask)
                        if b > 0 and a > 0:
                            mid = (b + a) / 2
                            gemini_mids[symbol] = mid
                            if on_mid:
                                on_mid(mid)
                    except (TypeError, ValueError):
                        pass
        except Exception as e:
            log.error(f"[{symbol}] Gemini error: {e} — retry in 5s")
            await asyncio.sleep(5)
