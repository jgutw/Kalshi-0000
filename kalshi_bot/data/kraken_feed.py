"""
kraken_feed.py — Kraken WebSocket v2 ticker feed.

Connects to wss://ws.kraken.com/v2, subscribes to ticker for BTC/USD, ETH/USD,
SOL/USD, XRP/USD. Extracts best bid/ask, computes mid, stores in
kraken_mids[symbol]. Reconnects on disconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict

import websockets

log = logging.getLogger("kalshi_bot.kraken")

KRAKEN_WS = "wss://ws.kraken.com/v2"

# symbol → mid price (shared across all connections)
kraken_mids: Dict[str, float] = {}

# symbol → Kraken WS v2 pair (DOGE is XDG on Kraken)
SYMBOL_TO_PAIR = {
    "BTC":  "BTC/USD",
    "ETH":  "ETH/USD",
    "SOL":  "SOL/USD",
    "XRP":  "XRP/USD",
    "DOGE": "XDG/USD",
    "BNB":  "BNB/USD",
    "HYPE": "HYPE/USD",
    "NEAR": "NEAR/USD",
    "ZEC":  "ZEC/USD",
}


async def run_kraken(symbol: str, pair: str, on_mid=None) -> None:
    """
    Connect to Kraken WS v2, subscribe to ticker for pair.
    On each snapshot/update, extract bid/ask, compute mid, store in
    kraken_mids[symbol]. Reconnect on disconnect.
    """
    log.info(f"[{symbol}] Kraken stream: {pair}")
    while True:
        try:
            async with websockets.connect(KRAKEN_WS, ping_interval=20) as ws:
                sub = {
                    "method": "subscribe",
                    "params": {
                        "channel": "ticker",
                        "symbol": [pair],
                    },
                }
                await ws.send(json.dumps(sub))
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if data.get("channel") != "ticker":
                        continue
                    payload = data.get("data", [])
                    if not payload:
                        continue
                    ticker = payload[0] if isinstance(payload[0], dict) else None
                    if not ticker or ticker.get("symbol") != pair:
                        continue
                    bid, ask = ticker.get("bid"), ticker.get("ask")
                    if bid is not None and ask is not None:
                        try:
                            b, a = float(bid), float(ask)
                            if b > 0 and a > 0:
                                mid = (b + a) / 2
                                kraken_mids[symbol] = mid
                                if on_mid:
                                    on_mid(mid)
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            log.error(f"[{symbol}] Kraken error: {e} — retry in 5s")
            await asyncio.sleep(5)
