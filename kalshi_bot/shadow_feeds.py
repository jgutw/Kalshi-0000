"""Public venue tasks for Shadow. Same feed modules Production uses."""
from __future__ import annotations

import logging
import time

from .config import ASSETS
from .data.binance_feed import BINANCE_WS, run_binance_feed
from .data.coinbase_feed import run_coinbase_microstructure
from .data.gemini_feed import SYMBOL_TO_PAIR as GEMINI_PAIRS
from .data.gemini_feed import run_gemini
from .data.kraken_feed import SYMBOL_TO_PAIR as KRAKEN_PAIRS
from .data.kraken_feed import run_kraken
from .data.okx_feed import OKX_WS, run_okx_feed

log = logging.getLogger("kalshi_bot.shadow_feeds")

# Heartbeat field names. Counts are callback invocations, not signal inputs.
CALLBACK_KINDS = (
    "coinbase_trades",
    "coinbase_books",
    "coinbase_mids",
    "binance_trades",
    "binance_books",
    "binance_mids",
    "okx_trades",
    "okx_books",
    "okx_mids",
    "kraken_mids",
    "gemini_mids",
)


class FeedTelemetry:
    """In-memory Shadow callback counts. Not read by make_decision."""

    def __init__(self) -> None:
        self.counts: dict[tuple[str, str], int] = {}

    def note(self, asset: str, kind: str) -> None:
        key = (asset, kind)
        self.counts[key] = self.counts.get(key, 0) + 1

    def count(self, asset: str, kind: str) -> int:
        return self.counts.get((asset, kind), 0)


def _note(telemetry: FeedTelemetry | None, asset: str, kind: str) -> None:
    if telemetry is not None:
        telemetry.note(asset, kind)


def warmup_snapshot(engine, telemetry: FeedTelemetry | None, now: float | None = None) -> dict:
    """Read-only warmup and callback state for one operational heartbeat."""
    now = time.time() if now is None else now
    signal = engine.signal
    trades = signal.trades
    trade_age = None
    if trades:
        stamp = trades[-1].get("t")
        if stamp is not None:
            trade_age = now - float(stamp)
    spot = engine.synthetic_spot
    sources = int(spot.source_count)
    fields = {
        "prices": len(signal.prices),
        "trades": len(trades),
        "is_ready": bool(signal.is_ready()),
        "trade_age_secs": trade_age,
        "spot_mid": spot.spot_mid,
        "spot_sources": sources,
        "spot_staleness_secs": float(spot.staleness) if sources else None,
    }
    asset = engine.spec.symbol
    for kind in CALLBACK_KINDS:
        fields[kind] = 0 if telemetry is None else telemetry.count(asset, kind)
    return fields


def _apply_coinbase_book(signal, engine, bids, asks) -> None:
    """Store a Coinbase book the way Binance stores its book, then refresh spot.

    Shadow does not call update_book_coinbase. That method is not implemented,
    and the exception would recycle the Coinbase trade socket.
    """
    signal.book_bids_cb = {float(price): float(qty) for price, qty in bids if float(qty) > 0}
    signal.book_asks_cb = {float(price): float(qty) for price, qty in asks if float(qty) > 0}
    if bids and asks:
        try:
            best_bid = max(float(level[0]) for level in bids if len(level) >= 1 and float(level[0]) > 0)
            best_ask = min(float(level[0]) for level in asks if len(level) >= 1 and float(level[0]) > 0)
            if best_bid > 0 and best_ask > 0:
                engine.synthetic_spot.update("coinbase", (best_bid + best_ask) / 2)
        except (ValueError, IndexError):
            pass


def public_feed_coros(engines: dict, telemetry: FeedTelemetry | None = None):
    """(name, coroutine) per production venue feed. Unmapped Kraken/Gemini assets are skipped."""
    coros = []
    specs = {spec.symbol: spec for spec in ASSETS}
    for symbol, engine in engines.items():
        spec = specs[symbol]
        product_id = f"{spec.symbol}-USD"
        signal = engine.signal

        def on_trade(price, size, side, signal=signal, asset=symbol, telemetry=telemetry):
            _note(telemetry, asset, "coinbase_trades")
            signal.update_trade_coinbase(price, size, side)

        def on_book(bids, asks, engine=engine, signal=signal, asset=symbol, telemetry=telemetry):
            _note(telemetry, asset, "coinbase_books")
            _apply_coinbase_book(signal, engine, bids, asks)

        def on_mid(mid, engine=engine, asset=symbol, telemetry=telemetry):
            _note(telemetry, asset, "coinbase_mids")
            engine.synthetic_spot.update("coinbase", mid)

        coros.append((f"{spec.symbol}:coinbase", run_coinbase_microstructure(
            spec.symbol, product_id, on_trade=on_trade, on_book=on_book, on_mid=on_mid,
        )))

        def on_binance_trade(price, qty, maker, signal=signal, asset=symbol, telemetry=telemetry):
            _note(telemetry, asset, "binance_trades")
            signal.update_trade_binance(price, qty, maker)

        def on_binance_book(bids, asks, signal=signal, asset=symbol, telemetry=telemetry):
            _note(telemetry, asset, "binance_books")
            signal.update_book_binance(bids, asks)

        def on_binance_spot(source, price, engine=engine, asset=symbol, telemetry=telemetry):
            _note(telemetry, asset, "binance_mids")
            engine.synthetic_spot.update(source, price)

        coros.append((f"{spec.symbol}:binance", run_binance_feed(
            spec.symbol, spec.binance_symbol, BINANCE_WS,
            on_trade=on_binance_trade,
            on_book=on_binance_book,
            on_spot=on_binance_spot,
        )))

        def on_okx_trade(price, qty, side, signal=signal, asset=symbol, telemetry=telemetry):
            _note(telemetry, asset, "okx_trades")
            signal.update_trade_okx(price, qty, side)

        def on_okx_book(bids, asks, signal=signal, asset=symbol, telemetry=telemetry):
            _note(telemetry, asset, "okx_books")
            signal.update_book_okx(bids, asks)

        def on_okx_spot(source, price, engine=engine, asset=symbol, telemetry=telemetry):
            _note(telemetry, asset, "okx_mids")
            engine.synthetic_spot.update(source, price)

        coros.append((f"{spec.symbol}:okx", run_okx_feed(
            spec.symbol, spec.okx_inst_id, OKX_WS,
            on_trade=on_okx_trade,
            on_book=on_okx_book,
            on_spot=on_okx_spot,
        )))
        kraken_pair = KRAKEN_PAIRS.get(spec.symbol)
        if not kraken_pair:
            log.warning("[%s] No Kraken pair mapped — skipping Kraken feed", spec.symbol)
        else:
            def on_kraken_mid(mid, engine=engine, asset=symbol, telemetry=telemetry):
                _note(telemetry, asset, "kraken_mids")
                engine.synthetic_spot.update("kraken", mid)

            coros.append((f"{spec.symbol}:kraken", run_kraken(
                spec.symbol, kraken_pair, on_kraken_mid,
            )))
        gemini_pair = GEMINI_PAIRS.get(spec.symbol)
        if not gemini_pair:
            log.info("[%s] No Gemini pair — skipping Gemini feed", spec.symbol)
        else:
            def on_gemini_mid(mid, engine=engine, asset=symbol, telemetry=telemetry):
                _note(telemetry, asset, "gemini_mids")
                engine.synthetic_spot.update("gemini", mid)

            coros.append((f"{spec.symbol}:gemini", run_gemini(
                spec.symbol, gemini_pair, on_gemini_mid,
            )))
    return coros
