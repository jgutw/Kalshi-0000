"""Public Binance aggTrade + depth20 feed.

The loop is the former KalshiMultiBot.run_binance body. It has no order path.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

import websockets

log = logging.getLogger("kalshi_bot")

BINANCE_WS = "wss://stream.binance.com:9443/stream"
BINANCE_RETRY_SECS = 30
BINANCE_PING_INTERVAL = 20


def binance_stream_url(base_url: str, binance_symbol: str) -> str:
    """Combined aggTrade and depth20 stream URL. Symbol is lowercased."""
    sym = binance_symbol.lower()
    return f"{base_url}?streams={sym}@aggTrade/{sym}@depth20@100ms"


def handle_binance_message(
    raw: str,
    on_trade: Callable[..., Any],
    on_book: Callable[..., Any],
    on_spot: Callable[..., Any],
) -> None:
    """Apply one combined-stream payload. JSON errors propagate to the reconnect loop."""
    data = json.loads(raw)
    stream = data.get("stream", "")
    pl = data.get("data", data)
    if "aggTrade" in stream:
        on_trade(float(pl["p"]), float(pl["q"]), pl["m"])
    elif "depth20" in stream:
        bids, asks = pl.get("bids", []), pl.get("asks", [])
        on_book(bids, asks)
        if bids and asks:
            try:
                best_bid = max(float(b[0]) for b in bids if len(b) >= 1 and float(b[0]) > 0)
                best_ask = min(float(a[0]) for a in asks if len(a) >= 1 and float(a[0]) > 0)
                if best_bid > 0 and best_ask > 0:
                    on_spot("binance", (best_bid + best_ask) / 2)
            except (ValueError, IndexError):
                pass


async def run_binance_feed(
    symbol: str,
    binance_symbol: str,
    url: str,
    on_trade: Callable[..., Any],
    on_book: Callable[..., Any],
    on_spot: Callable[..., Any],
    *,
    connect: Optional[Callable[..., Any]] = None,
    sleep: Optional[Callable[[float], Any]] = None,
) -> None:
    """Reconnect forever. HTTP 451 disables this feed and returns."""
    stream_url = binance_stream_url(url, binance_symbol)
    connect = websockets.connect if connect is None else connect
    sleep = asyncio.sleep if sleep is None else sleep
    log.info(f"[{symbol}] Binance stream (optional): {binance_symbol.lower()}")
    geo_block_logged = False
    while True:
        try:
            async with connect(stream_url, ping_interval=BINANCE_PING_INTERVAL) as ws:
                async for raw in ws:
                    handle_binance_message(raw, on_trade, on_book, on_spot)
        except Exception as e:
            err = str(e)
            if "451" in err or "Unavailable for legal reasons" in err:
                if not geo_block_logged:
                    log.warning(
                        f"[{symbol}] Binance geo-blocked (451) — disabling feed; "
                        "using Coinbase/OKX/Kraken only"
                    )
                    geo_block_logged = True
                return
            log.error(f"[{symbol}] Binance error: {e} — retry in 30s")
            await sleep(BINANCE_RETRY_SECS)
