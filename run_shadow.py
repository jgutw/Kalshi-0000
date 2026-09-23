"""Shadow Era 1 entry pipeline.

Live public data, production make_decision, durable simulated entry.
No KalshiClient, KalshiMultiBot, safe-live, or Telegram.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

from kalshi_bot.asset_engine import AssetEngine
from kalshi_bot.config import ASSETS
from kalshi_bot.shadow import ShadowSession
from kalshi_bot.shadow_entry import (
    ShadowEntry, load_starting_balance, private_sim, record_starting_balance,
)
from kalshi_bot.shadow_feeds import FeedTelemetry, public_feed_coros, warmup_snapshot
from kalshi_bot.shadow_market import ReadOnlyKalshi

log = logging.getLogger("kalshi_bot.shadow")


def read_base_url() -> str:
    """Same public REST selection as APIConfig.KALSHI_REST, without importing it."""
    override = os.environ.get("KALSHI_BASE_URL")
    if override:
        return override.rstrip("/")
    if os.environ.get("KALSHI_DEMO", "false").lower() == "true":
        return "https://demo-api.kalshi.co/trade-api/v2"
    return "https://api.elections.kalshi.com/trade-api/v2"


def requests_get_transport():
    import requests

    def transport(url, *, headers, params, timeout):
        response = requests.get(url, headers=headers, params=params, timeout=timeout)
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body

    return transport


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Shadow Era 1 entry pipeline. Does not transmit capital.")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-tag", required=True)
    parser.add_argument("--code-sha")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--new", action="store_true")
    mode.add_argument("--recover", action="store_true")
    parser.add_argument("--starting-balance", type=float)
    parser.add_argument("--root", default="shadow_data")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--fresh-round", action="store_true")
    args = parser.parse_args(argv)
    if args.live or args.fresh_round:
        parser.error("Shadow rejects --live and --fresh-round")
    if args.session_tag.casefold() == "p6c_d1_validation":
        parser.error("Shadow rejects the P6 experimental session tag")
    if args.new and args.starting_balance is None:
        parser.error("A new Shadow session requires --starting-balance")
    if args.new and not args.code_sha:
        parser.error("A new Shadow session requires --code-sha")
    return args


def open_session(args):
    if args.new:
        session = ShadowSession(
            args.root, args.session_id, args.session_tag, args.code_sha,
            config_identity={"runner": "shadow-entry-4b"},
        )
        record_starting_balance(session, args.starting_balance)
        balance = float(args.starting_balance)
    else:
        session = ShadowSession.recover(args.root, args.session_id)
        balance = load_starting_balance(session)
        if args.starting_balance is not None and float(args.starting_balance) != balance:
            session.close()
            raise SystemExit("Recovery starting balance does not match the persisted session value")
    return session, balance


def build_engines(market, sim):
    engines = {}
    for spec in ASSETS:
        if not spec.enabled:
            continue
        engines[spec.symbol] = AssetEngine(
            spec, market, sim, entries_paused_provider=lambda: False,
        )
    for engine in engines.values():
        engine._all_engines = engines
    disabled = []
    for symbol, engine in engines.items():
        if not market.verify_series(engine.spec.series_ticker):
            disabled.append(symbol)
    for symbol in disabled:
        log.warning("[%s] series not verified — engine disabled", symbol)
        engines.pop(symbol, None)
    return engines


async def _price_loop(pipeline: ShadowEntry, engine: AssetEngine, telemetry: FeedTelemetry) -> None:
    streak = 0
    heartbeat = 0.0
    while True:
        try:
            pipeline.refresh_window(engine)
            if engine._ticker:
                yes_mid = pipeline.market.get_yes_mid(engine._ticker)
                if yes_mid is not None:
                    streak = 0
                    pipeline.on_yes_mid(engine, yes_mid)
                else:
                    streak += 1
                    if streak % 5 == 1:
                        found = pipeline.market.find_active_market(engine.spec.series_ticker)
                        if found:
                            engine._market = found
                            engine._ticker = found.get("ticker", "")
                            engine._close_time_utc = found.get("close_time")
            else:
                streak += 1
                if streak % 5 == 1:
                    found = pipeline.market.find_active_market(engine.spec.series_ticker)
                    if found:
                        engine._market = found
                        engine._ticker = found.get("ticker", "")
                        engine._close_time_utc = found.get("close_time")
            now = time.time()
            if now - heartbeat >= 30:
                pipeline.operational(
                    "heartbeat", asset=engine.spec.symbol, ticks=engine._total_ticks,
                    ticker=engine._ticker or None, window_id_ts=engine._window_id,
                    **warmup_snapshot(engine, telemetry, now),
                )
                heartbeat = now
        except Exception as exc:
            pipeline.operational("price_feed_error", asset=engine.spec.symbol, error=str(exc))
        await asyncio.sleep(1)


async def _watch_feed(pipeline: ShadowEntry, name: str, coro) -> None:
    try:
        await coro
        pipeline.operational("feed_stopped", feed=name)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        pipeline.operational("feed_failure", feed=name, error=str(exc))


async def run(args) -> None:
    session, balance = open_session(args)
    pipeline = ShadowEntry(session, ReadOnlyKalshi(
        read_base_url(),
        os.environ.get("KALSHI_API_KEY", ""),
        os.environ.get("KALSHI_PRIVATE_KEY", ""),
        requests_get_transport(),
    ))
    try:
        pipeline.operational("startup", mode="recover" if args.recover else "new",
                             starting_balance=balance)
        engines = build_engines(pipeline.market, private_sim(balance))
        pipeline.restore_open_positions(engines)
        telemetry = FeedTelemetry()
        tasks = [asyncio.create_task(_price_loop(pipeline, engine, telemetry), name=f"price:{symbol}")
                 for symbol, engine in engines.items()]
        for name, coro in public_feed_coros(engines, telemetry):
            tasks.append(asyncio.create_task(_watch_feed(pipeline, name, coro), name=name))
        await asyncio.gather(*tasks)
    except Exception as exc:
        pipeline.operational("uncaught_loop_error", error=str(exc))
        raise
    finally:
        pipeline.operational("shutdown")
        session.close()


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log.info("Shadow interrupted")


if __name__ == "__main__":
    main()
