"""Public OKX books5 + trades feed.

The loop is the former KalshiMultiBot.run_okx body. It has no order path.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

import websockets

log = logging.getLogger("kalshi_bot")

OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"
OKX_RETRY_SECS = 5
OKX_PING_INTERVAL = 25


def okx_subscription(inst_id: str) -> dict:
    return {
        "op": "subscribe",
        "args": [
            {"channel": "books5", "instId": inst_id},
            {"channel": "trades", "instId": inst_id},
        ],
    }


def handle_okx_message(
    raw: str,
    on_trade: Callable[..., Any],
    on_book: Callable[..., Any],
    on_spot: Callable[..., Any],
    *,
    symbol: str,
) -> None:
    """Apply one OKX payload. JSON errors propagate to the reconnect loop."""
    data = json.loads(raw)
    if "event" in data:
        if data.get("event") == "error":
            log.error(f"[{symbol}] OKX error: {data}")
        return
    channel = data.get("arg", {}).get("channel", "")
    pl = data.get("data", [])
    if not pl:
        return
    if channel == "books5":
        snap = pl[0] if isinstance(pl[0], dict) else {}
        bids, asks = snap.get("bids", []), snap.get("asks", [])
        on_book(bids, asks)
        if bids and asks:
            try:
                best_bid = max(float(b[0]) for b in bids if len(b) >= 1 and float(b[0]) > 0)
                best_ask = min(float(a[0]) for a in asks if len(a) >= 1 and float(a[0]) > 0)
                if best_bid > 0 and best_ask > 0:
                    on_spot("okx", (best_bid + best_ask) / 2)
            except (ValueError, IndexError):
                pass
    elif channel == "trades":
        for t in pl:
            if isinstance(t, dict):
                px = float(t.get("px", 0))
                sz = float(t.get("sz", 0))
                side = t.get("side", "buy")
                if px > 0 and sz > 0:
                    on_trade(px, sz, side)


async def run_okx_feed(
    symbol: str,
    inst_id: str,
    url: str,
    on_trade: Callable[..., Any],
    on_book: Callable[..., Any],
    on_spot: Callable[..., Any],
    *,
    connect: Optional[Callable[..., Any]] = None,
    sleep: Optional[Callable[[float], Any]] = None,
) -> None:
    """Reconnect forever, five seconds after an error."""
    connect = websockets.connect if connect is None else connect
    sleep = asyncio.sleep if sleep is None else sleep
    log.info(f"[{symbol}] OKX stream: {inst_id}")
    while True:
        try:
            async with connect(url, ping_interval=OKX_PING_INTERVAL) as ws:
                await ws.send(json.dumps(okx_subscription(inst_id)))
                async for raw in ws:
                    handle_okx_message(raw, on_trade, on_book, on_spot, symbol=symbol)
        except Exception as e:
            log.error(f"[{symbol}] OKX error: {e} — retry in 5s")
            await sleep(OKX_RETRY_SECS)
