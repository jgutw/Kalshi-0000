"""
coinbase_feed.py — Coinbase Advanced Trade WebSocket microstructure feed.

Primary feed for US users (Binance returns HTTP 451 for US IPs).
Connects to wss://advanced-trade-ws.coinbase.com, subscribes to:
  - market_trades: real-time trade executions (price, size, side)
  - level2: order book snapshots and updates (bids, asks)

Callbacks: on_trade(price, size, side), on_book(bids, asks), on_mid(mid)
Product IDs: {SYMBOL}-USD (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, NEAR, ZEC, …)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Dict, List, Optional

import websockets

log = logging.getLogger("kalshi_bot.coinbase")

COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"

# symbol → mid price (shared across all connections)
coinbase_mids: Dict[str, float] = {}

# product_id (e.g. BTC-USD) → symbol
PRODUCT_TO_SYMBOL = {
    "BTC-USD": "BTC",
    "ETH-USD": "ETH",
    "SOL-USD": "SOL",
    "XRP-USD": "XRP",
}


def _parse_trades(events: list, product_id: str) -> List[tuple]:
    """Extract (price, size, side) from market_trades events."""
    out = []
    for ev in events:
        if ev.get("type") != "update":
            continue
        for t in ev.get("trades", []):
            if t.get("product_id") != product_id:
                continue
            try:
                price = float(t.get("price", 0))
                size = float(t.get("size", 0))
                side = (t.get("side") or "BUY").upper()
                if price > 0 and size > 0 and side in ("BUY", "SELL"):
                    out.append((price, size, side))
            except (TypeError, ValueError):
                pass
    return out


def _apply_level2_updates(
    bids: Dict[str, float],
    asks: Dict[str, float],
    events: list,
    product_id: str,
) -> None:
    """
    Apply level2 snapshot/update events to in-place bids/asks dicts.
    Handles: products[].bids/asks, products[].price_levels, updates[].side.
    """
    for ev in events:
        ev_type = ev.get("type", "")
        if ev_type not in ("snapshot", "update"):
            continue
        # Snapshot: replace entire book
        if ev_type == "snapshot":
            bids.clear()
            asks.clear()
        # products[].bids/asks as [[price, size], ...]
        for prod in ev.get("products", []):
            if prod.get("product_id") != product_id:
                continue
            for b in prod.get("bids", []):
                if b and len(b) >= 2:
                    try:
                        px, sz = float(b[0]), float(b[1])
                        if sz > 0:
                            bids[str(px)] = sz
                        else:
                            bids.pop(str(px), None)
                    except (TypeError, ValueError, IndexError):
                        pass
            for a in prod.get("asks", []):
                if a and len(a) >= 2:
                    try:
                        px, sz = float(a[0]), float(a[1])
                        if sz > 0:
                            asks[str(px)] = sz
                        else:
                            asks.pop(str(px), None)
                    except (TypeError, ValueError, IndexError):
                        pass
            # price_levels [[price, size] or [price, size, side], ...]
            for pl in prod.get("price_levels", []):
                if not pl or len(pl) < 2:
                    continue
                try:
                    px, sz = float(pl[0]), float(pl[1])
                    side = (pl[2] if len(pl) > 2 else "bid").lower()
                    if "bid" in side or side == "buy":
                        if sz > 0:
                            bids[str(px)] = sz
                        else:
                            bids.pop(str(px), None)
                    else:
                        if sz > 0:
                            asks[str(px)] = sz
                        else:
                            asks.pop(str(px), None)
                except (TypeError, ValueError, IndexError):
                    pass
        # Alternative: updates[] with side and price_levels
        for upd in ev.get("updates", []):
            side = (upd.get("side") or "bid").lower()
            for pl in upd.get("price_levels", []):
                if not pl or len(pl) < 2:
                    continue
                try:
                    px, sz = float(pl[0]), float(pl[1])
                    if "bid" in side or side == "buy":
                        if sz > 0:
                            bids[str(px)] = sz
                        else:
                            bids.pop(str(px), None)
                    else:
                        if sz > 0:
                            asks[str(px)] = sz
                        else:
                            asks.pop(str(px), None)
                except (TypeError, ValueError, IndexError):
                    pass


async def run_coinbase_microstructure(
    symbol: str,
    product_id: str,
    on_trade: Optional[Callable[[float, float, str], None]] = None,
    on_book: Optional[Callable[[list, list], None]] = None,
    on_mid: Optional[Callable[[float], None]] = None,
) -> None:
    """
    Connect to Coinbase Advanced Trade WS, subscribe to market_trades and level2.
    On each message, parse and invoke callbacks. Reconnect on disconnect.
    """
    log.info(f"[{symbol}] Coinbase microstructure: {product_id} (market_trades + level2)")
    while True:
        try:
            log.info(f"[{symbol}] Coinbase attempting connection to {COINBASE_WS}")
            # level2 snapshots can exceed 1MB; increase max_size (default 1MB)
            async with websockets.connect(COINBASE_WS, ping_interval=20, max_size=10 * 1024 * 1024) as ws:
                log.info(f"[{symbol}] Coinbase connected")
                # Subscribe within 5s: market_trades and level2 (one channel per message)
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "product_ids": [product_id],
                    "channel": "market_trades",
                }))
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "product_ids": [product_id],
                    "channel": "level2",
                }))
                # Optional: heartbeats to keep connection alive
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "product_ids": [product_id],
                    "channel": "heartbeats",
                }))
                book_bids: Dict[str, float] = {}
                book_asks: Dict[str, float] = {}
                msg_count = 0

                async def _30s_check() -> None:
                    await asyncio.sleep(30)
                    log.info(f"[{symbol}] Coinbase 30s check: {msg_count} messages received")
                    if msg_count == 0:
                        log.warning(f"[{symbol}] No messages in 30s — forcing reconnect")
                        await ws.close()

                asyncio.create_task(_30s_check())
                async for raw in ws:
                    msg_count += 1
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    channel = data.get("channel", "")
                    events = data.get("events", [])
                    if not events:
                        continue
                    # market_trades
                    if channel == "market_trades":
                        for price, size, side in _parse_trades(events, product_id):
                            log.debug(f"[{symbol}] Raw Coinbase price={price} type={type(price).__name__}")
                            log.debug(f"[{symbol}] Coinbase trade parsed: price={price} qty={size} side={side}")
                            if on_trade:
                                on_trade(price, size, side)
                    # level2
                    elif channel == "level2":
                        _apply_level2_updates(book_bids, book_asks, events, product_id)
                        if book_bids or book_asks:
                            bid_list = [[float(p), s] for p, s in book_bids.items() if s > 0]
                            ask_list = [[float(p), s] for p, s in book_asks.items() if s > 0]
                            bid_list.sort(key=lambda x: -x[0])
                            ask_list.sort(key=lambda x: x[0])
                            if on_book and (bid_list or ask_list):
                                on_book(bid_list, ask_list)
                            if on_mid and bid_list and ask_list:
                                try:
                                    best_bid = bid_list[0][0]
                                    best_ask = ask_list[0][0]
                                    if best_bid > 0 and best_ask > 0:
                                        mid = (best_bid + best_ask) / 2
                                        coinbase_mids[symbol] = mid
                                        on_mid(mid)
                                except (ValueError, IndexError):
                                    pass
        except Exception as e:
            log.error(f"[{symbol}] Coinbase connection failed: {e}")
            await asyncio.sleep(5)


async def run_coinbase(symbol: str, product_id: str, on_mid=None) -> None:
    """
    Legacy ticker-only feed. Reconnect on disconnect.
    """
    log.info(f"[{symbol}] Coinbase stream: {product_id} (ticker)")
    while True:
        try:
            async with websockets.connect(COINBASE_WS, ping_interval=20) as ws:
                sub = {
                    "type": "subscribe",
                    "product_ids": [product_id],
                    "channel": "ticker",
                }
                await ws.send(json.dumps(sub))
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "ticker" and data.get("product_id") == product_id:
                        bid, ask = data.get("best_bid"), data.get("best_ask")
                        if bid is not None and ask is not None:
                            try:
                                b, a = float(bid), float(ask)
                                if b > 0 and a > 0:
                                    mid = (b + a) / 2
                                    coinbase_mids[symbol] = mid
                                    if on_mid:
                                        on_mid(mid)
                            except (TypeError, ValueError):
                                pass
                        continue
                    if data.get("channel") != "ticker":
                        continue
                    for ev in data.get("events", []):
                        if ev.get("type") != "update":
                            continue
                        for t in ev.get("tickers", []):
                            if t.get("product_id") != product_id:
                                continue
                            bid, ask = t.get("best_bid"), t.get("best_ask")
                            if bid is not None and ask is not None:
                                try:
                                    b, a = float(bid), float(ask)
                                    if b > 0 and a > 0:
                                        mid = (b + a) / 2
                                        coinbase_mids[symbol] = mid
                                        if on_mid:
                                            on_mid(mid)
                                except (TypeError, ValueError):
                                    pass
        except Exception as e:
            log.error(f"[{symbol}] Coinbase error: {e} — retry in 5s")
            await asyncio.sleep(5)
