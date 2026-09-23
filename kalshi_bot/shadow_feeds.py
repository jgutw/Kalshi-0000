"""Public venue tasks for Shadow. Same feed modules Production uses."""
from __future__ import annotations

import logging

from .config import ASSETS
from .data.binance_feed import BINANCE_WS, run_binance_feed
from .data.coinbase_feed import run_coinbase_microstructure
from .data.gemini_feed import SYMBOL_TO_PAIR as GEMINI_PAIRS
from .data.gemini_feed import run_gemini
from .data.kraken_feed import SYMBOL_TO_PAIR as KRAKEN_PAIRS
from .data.kraken_feed import run_kraken
from .data.okx_feed import OKX_WS, run_okx_feed

log = logging.getLogger("kalshi_bot.shadow_feeds")


def public_feed_coros(engines: dict):
    """(name, coroutine) per production venue feed. Unmapped Kraken/Gemini assets are skipped."""
    coros = []
    specs = {spec.symbol: spec for spec in ASSETS}
    for symbol, engine in engines.items():
        spec = specs[symbol]
        product_id = f"{spec.symbol}-USD"
        signal = engine.signal

        def on_trade(price, size, side, signal=signal):
            signal.update_trade_coinbase(price, size, side)

        def on_book(bids, asks, engine=engine, signal=signal):
            signal.update_book_coinbase(bids, asks)
            if bids and asks:
                try:
                    best_bid = max(float(level[0]) for level in bids if len(level) >= 1 and float(level[0]) > 0)
                    best_ask = min(float(level[0]) for level in asks if len(level) >= 1 and float(level[0]) > 0)
                    if best_bid > 0 and best_ask > 0:
                        engine.synthetic_spot.update("coinbase", (best_bid + best_ask) / 2)
                except (ValueError, IndexError):
                    pass

        def on_mid(mid, engine=engine):
            engine.synthetic_spot.update("coinbase", mid)

        coros.append((f"{spec.symbol}:coinbase", run_coinbase_microstructure(
            spec.symbol, product_id, on_trade=on_trade, on_book=on_book, on_mid=on_mid,
        )))
        coros.append((f"{spec.symbol}:binance", run_binance_feed(
            spec.symbol, spec.binance_symbol, BINANCE_WS,
            on_trade=engine.signal.update_trade_binance,
            on_book=engine.signal.update_book_binance,
            on_spot=engine.synthetic_spot.update,
        )))
        coros.append((f"{spec.symbol}:okx", run_okx_feed(
            spec.symbol, spec.okx_inst_id, OKX_WS,
            on_trade=engine.signal.update_trade_okx,
            on_book=engine.signal.update_book_okx,
            on_spot=engine.synthetic_spot.update,
        )))
        kraken_pair = KRAKEN_PAIRS.get(spec.symbol)
        if not kraken_pair:
            log.warning("[%s] No Kraken pair mapped — skipping Kraken feed", spec.symbol)
        else:
            coros.append((f"{spec.symbol}:kraken", run_kraken(
                spec.symbol, kraken_pair, lambda mid, engine=engine: engine.synthetic_spot.update("kraken", mid),
            )))
        gemini_pair = GEMINI_PAIRS.get(spec.symbol)
        if not gemini_pair:
            log.info("[%s] No Gemini pair — skipping Gemini feed", spec.symbol)
        else:
            coros.append((f"{spec.symbol}:gemini", run_gemini(
                spec.symbol, gemini_pair, lambda mid, engine=engine: engine.synthetic_spot.update("gemini", mid),
            )))
    return coros
