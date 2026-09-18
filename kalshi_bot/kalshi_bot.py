"""
kalshi_bot.py — Multi-asset Kalshi 15-min trading bot.

Orchestrates:
  · Coinbase Advanced Trade WebSocket (primary — market_trades + level2, US-available)
  · OKX books5 + trades WebSocket (secondary)
  · Binance aggTrade + depth20 (fallback — blocked for US IPs, HTTP 451)
  · Kalshi orderbook REST poll every 1s per asset
  · One AssetEngine per enabled asset, all running concurrently

How prices flow:
  Coinbase/OKX/Binance → AssetSignalEngine (Hawkes, OBI, Bayesian fusion)
                        ↓
  Kalshi REST poll  → AssetEngine.on_price_update()
                        ↓
                     make_decision() → execute() / wait

Kalshi WebSocket notes:
  Kalshi does support a WebSocket API for orderbook deltas.
  The channel is `orderbook_delta` with a subscription message.
  However, the REST poll path is used here because:
    1. The Kalshi WS requires auth (bearer token from POST /login)
    2. REST poll at 1s is sufficient for 15-min markets
    3. Easier to implement reliably for paper trading
  To switch to WS: implement run_kalshi_ws() and subscribe to
  `{"id":1,"cmd":"subscribe","params":{"channels":["orderbook_delta"],"market_tickers":[...]}}`
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import websockets

from .config import cfg, api_cfg, ASSETS, AssetSpec, all_asset_symbols
from .data.kraken_feed import SYMBOL_TO_PAIR as KRAKEN_PAIRS
from .data.gemini_feed import SYMBOL_TO_PAIR as GEMINI_PAIRS
from .kalshi_client import KalshiClient
from .signal_engine import AssetSignalEngine
from .sim_state import SimState
from .live_guard import LiveGuard
from .asset_engine import AssetEngine
from .recorder import EventRecorder
from .data.coinbase_feed import run_coinbase_microstructure as run_coinbase_microstructure_feed
from .data.kraken_feed import run_kraken as run_kraken_feed
from .data.gemini_feed import run_gemini as run_gemini_feed
from .session_meta import (
    archive_and_log_round,
    prepare_fresh_round,
    tag_archived_session,
    write_session_meta,
)
from .runtime_control import (
    PROFILE_PRESETS,
    apply_profile,
    clear_bot_command_queue,
    clear_stop,
    prepare_for_new_round,
    process_bot_commands,
    stop_requested,
    write_bot_heartbeat,
    write_live_config,
    write_open_positions,
)

# Windows UTF-8 fix
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kalshi_bot")


# ═══════════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

class KalshiMultiBot:
    """
    Runs all enabled asset engines concurrently.
    One asyncio event loop, multiple coroutines:
      · run_binance(asset)  — per-asset Binance stream
      · run_okx(asset)      — per-asset OKX stream
      · run_price_feed(engine) — per-asset Kalshi REST poll
      · heartbeat()         — periodic status log
    """

    def __init__(self):
        self.kalshi   = KalshiClient()
        self.sim      = SimState.load()
        if self.sim.reconcile_from_trade_log():
            self.sim.save()  # persist reconciled state
        else:
            self.sim.save()  # ensure dashboard has valid file from startup
        self.recorder = EventRecorder()
        self.engines: Dict[str, AssetEngine] = {}
        self.live_guard: LiveGuard | None = None

        def on_window_close(
            window_id: str, asset: str,
            price_to_beat: float, exit_price: float,
            outcome: str, trade_count: int, window_pnl: float,
        ) -> None:
            log.info(
                f"[WINDOW] {window_id} | {asset} | "
                f"price_to_beat={price_to_beat:.4f} | "
                f"exit_price={exit_price:.4f} | "
                f"outcome={outcome} | "
                f"trades={trade_count} | pnl={window_pnl:+.2f}"
            )

        for spec in ASSETS:
            if spec.enabled:
                self.engines[spec.symbol] = AssetEngine(
                    spec, self.kalshi, self.sim,
                    recorder=self.recorder,
                    on_window_close=on_window_close,
                )

        for engine in self.engines.values():
            engine._all_engines = self.engines

        self._shutdown = False
        log.info(f"Enabled assets: {list(self.engines.keys())}")

    def _open_position_list(self):
        return [e._open_pos for e in self.engines.values() if e._open_pos is not None]

    def _snapshot_open_positions(self) -> None:
        rows = []
        for sym, engine in self.engines.items():
            pos = engine._open_pos
            if pos is None:
                continue
            rows.append(
                {
                    "asset": sym,
                    "ticker": pos.market_ticker,
                    "side": pos.side,
                    "entry": pos.entry_price,
                    "contracts": pos.contracts,
                    "amount_usdc": pos.amount_usdc,
                    "window_id": pos.window_id,
                    "price_to_beat": pos.price_to_beat,
                    "entered_at": pos.entered_at,
                }
            )
        write_open_positions(rows)

    # ─── Startup ──────────────────────────────────────────────────────────────

    def _verify_series(self) -> None:
        """
        At startup, verify each series exists on Kalshi.
        Disable any that return 404 (e.g. XRP 15m may not exist).
        """
        to_remove = []
        for sym, engine in list(self.engines.items()):
            spec = engine.spec
            exists = self.kalshi.verify_series(spec.series_ticker)
            if not exists:
                log.warning(f"[{sym}] Series {spec.series_ticker} not found on Kalshi — disabling")
                to_remove.append(sym)
            else:
                m = self.kalshi.find_active_market(spec.series_ticker)
                if m:
                    engine._market = m
                    engine._ticker = m.get("ticker", "")
                    log.info(f"[{sym}] Active market: {engine._ticker}")
                else:
                    log.warning(f"[{sym}] Series exists but no open market yet")
        for sym in to_remove:
            del self.engines[sym]

    def _load_recent_windows(self) -> list:
        """Load last 3 completed windows from kalshi_trades.jsonl + kalshi_decisions.jsonl."""
        from pathlib import Path
        from datetime import datetime, timezone
        root = Path(__file__).resolve().parent.parent
        trades_path = root / "logs" / "kalshi_trades.jsonl"
        decisions_path = root / "logs" / "kalshi_decisions.jsonl"
        window_data: dict = {}  # window_id -> {asset -> str}
        assets = all_asset_symbols()

        def _fmt_window_short(wid: str) -> str:
            if not wid or len(wid) < 16:
                return wid or "?"
            return wid[11:16]  # "HH:MM"

        def _ensure_window(wid: str) -> None:
            if wid and wid not in window_data:
                window_data[wid] = {a: "NO_TRADE" for a in assets}

        # Trades
        if trades_path.exists():
            with open(trades_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    wid = t.get("window_id", "")
                    if not wid:
                        # Fallback: infer from ts (e.g. "2026-03-17T10:30:00.008088")
                        ts = t.get("ts", "")
                        if ts and "T" in ts:
                            try:
                                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                min_floor = (dt.minute // 15) * 15
                                dt = dt.replace(minute=min_floor, second=0, microsecond=0)
                                wid = dt.strftime("%Y-%m-%d %H:%M")
                            except (ValueError, TypeError):
                                continue
                        else:
                            continue
                    _ensure_window(wid)
                    asset = t.get("asset", "BTC")
                    pnl = float(t.get("pnl", 0))
                    outcome = f"+${pnl:.2f} WIN" if pnl > 0 else f"-${abs(pnl):.2f} LOSS"
                    window_data[wid][asset] = outcome

        # Decisions: collect windows (for NO_TRADE rows)
        if decisions_path.exists():
            with open(decisions_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    wid = d.get("window_id", "")
                    wid_ts = d.get("window_id_ts")
                    if (not wid or isinstance(wid, int)) and isinstance(wid_ts, (int, float)) and wid_ts:
                        wid = datetime.fromtimestamp(int(wid_ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                    if wid and isinstance(wid, str):
                        _ensure_window(wid)

        # Sort by window_id desc, take last 3
        all_windows = sorted([w for w in window_data.keys()], reverse=True)
        result = []
        for wid in all_windows[:3]:
            row = window_data[wid]
            parts = [f"{a} {row.get(a, 'NO_TRADE')}" for a in assets]
            result.append((_fmt_window_short(wid), "  |  ".join(parts)))
        return result

    def print_status(self) -> None:
        s = self.sim
        pnl = s.balance - s.starting_balance
        print("\n" + "=" * 62)
        print("  KALSHI MULTI-ASSET 15-MIN BOT — STATUS")
        print("=" * 62)
        print(f"  Balance:    ${s.balance:>10,.2f}  (start ${s.starting_balance:.2f})")
        eq_pnl = s.total_equity - s.starting_balance
        print(f"  Vault:      ${s.vault_balance:>10,.2f}  equity ${s.total_equity:,.2f}")
        print(f"  P&L:        ${eq_pnl:>+10,.2f}  equity ({(s.total_equity/s.starting_balance - 1):+.1%})")
        print(f"  Trades:     {s.total_trades}  W={s.wins} L={s.losses}  WR={s.win_rate:.1%}")
        print(f"  Sharpe:     {s.current_sharpe:.2f}   VaR95={s.var_95:.2%}")
        dd_basis = "equity" if getattr(cfg, "DRAWDOWN_USE_EQUITY", True) else "trading"
        dd_note = (
            f"halt@{cfg.MAX_DRAWDOWN_PCT:.0%} {dd_basis}"
            if getattr(cfg, "DRAWDOWN_HALT_ENABLED", True)
            else "halt off"
        )
        print(f"  Peak DD:    {s.peak_drawdown:.1%}  ({dd_note})")
        idle_m = s.seconds_since_last_trade() / 60.0
        if getattr(cfg, "ACTIVITY_MANDATE_ENABLED", False):
            probe = "PROBE ON" if s.activity_idle() else "normal"
            print(f"  Activity:   idle {idle_m:.0f}m  ({probe})")
        print(f"  Mode:       {'PAPER' if cfg.DRY_RUN else '⚠ LIVE'}")
        halted, reason = s.is_halted()
        if halted:
            print(f"  HALTED:     {reason}")
        print()
        for sym, eng in self.engines.items():
            stats = s.asset_stats.get(sym)
            wr = stats.win_rate if stats else 0.0
            pnl_a = stats.total_pnl if stats else 0.0
            pos = eng._open_pos
            pos_str = f"{pos.side.upper()} ×{pos.contracts}" if pos else "none"
            print(f"  [{sym:4}]  WR={wr:.0%}  pnl={pnl_a:+.2f}  pos={pos_str}  ticker={eng._ticker or '?'}")
        # Recent windows
        try:
            recent = self._load_recent_windows()
            if recent:
                print()
                print("  RECENT WINDOWS")
                for win_label, line in recent:
                    print(f"    {win_label} {line}")
        except Exception:
            pass
        print("=" * 62 + "\n")

    # ─── Binance WebSocket (fallback — blocked for US IPs, HTTP 451) ─────────────

    async def run_binance(self, spec: AssetSpec) -> None:
        """
        Optional aggTrade + depth20 stream.
        US IPs get HTTP 451 — stop cleanly so Coinbase/OKX/Kraken are unaffected.
        """
        sym  = spec.binance_symbol.lower()
        url  = f"{api_cfg.BINANCE_WS}?streams={sym}@aggTrade/{sym}@depth20@100ms"
        engine = self.engines[spec.symbol]
        sig    = engine.signal
        log.info(f"[{spec.symbol}] Binance stream (optional): {sym}")
        geo_block_logged = False
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    async for raw in ws:
                        data   = json.loads(raw)
                        stream = data.get("stream", "")
                        pl     = data.get("data", data)
                        if "aggTrade" in stream:
                            sig.update_trade_binance(
                                float(pl["p"]), float(pl["q"]), pl["m"]
                            )
                        elif "depth20" in stream:
                            bids, asks = pl.get("bids", []), pl.get("asks", [])
                            sig.update_book_binance(bids, asks)
                            if bids and asks:
                                try:
                                    best_bid = max(float(b[0]) for b in bids if len(b) >= 1 and float(b[0]) > 0)
                                    best_ask = min(float(a[0]) for a in asks if len(a) >= 1 and float(a[0]) > 0)
                                    if best_bid > 0 and best_ask > 0:
                                        engine.synthetic_spot.update("binance", (best_bid + best_ask) / 2)
                                except (ValueError, IndexError):
                                    pass
            except Exception as e:
                err = str(e)
                if "451" in err or "Unavailable for legal reasons" in err:
                    if not geo_block_logged:
                        log.warning(
                            f"[{spec.symbol}] Binance geo-blocked (451) — disabling feed; "
                            "using Coinbase/OKX/Kraken only"
                        )
                        geo_block_logged = True
                    return
                log.error(f"[{spec.symbol}] Binance error: {e} — retry in 30s")
                await asyncio.sleep(30)

    # ─── OKX WebSocket ────────────────────────────────────────────────────────

    async def run_okx(self, spec: AssetSpec) -> None:
        """books5 + trades for one asset from OKX."""
        inst_id = spec.okx_inst_id
        url     = api_cfg.OKX_WS
        engine  = self.engines[spec.symbol]
        sig     = engine.signal
        log.info(f"[{spec.symbol}] OKX stream: {inst_id}")
        while True:
            try:
                async with websockets.connect(url, ping_interval=25) as ws:
                    sub = {
                        "op": "subscribe",
                        "args": [
                            {"channel": "books5", "instId": inst_id},
                            {"channel": "trades",  "instId": inst_id},
                        ],
                    }
                    await ws.send(json.dumps(sub))
                    async for raw in ws:
                        data    = json.loads(raw)
                        if "event" in data:
                            if data.get("event") == "error":
                                log.error(f"[{spec.symbol}] OKX error: {data}")
                            continue
                        channel = data.get("arg", {}).get("channel", "")
                        pl      = data.get("data", [])
                        if not pl:
                            continue
                        if channel == "books5":
                            snap = pl[0] if isinstance(pl[0], dict) else {}
                            bids, asks = snap.get("bids", []), snap.get("asks", [])
                            sig.update_book_okx(bids, asks)
                            # Update synthetic spot from OKX book
                            if bids and asks:
                                try:
                                    best_bid = max(float(b[0]) for b in bids if len(b) >= 1 and float(b[0]) > 0)
                                    best_ask = min(float(a[0]) for a in asks if len(a) >= 1 and float(a[0]) > 0)
                                    if best_bid > 0 and best_ask > 0:
                                        engine.synthetic_spot.update("okx", (best_bid + best_ask) / 2)
                                except (ValueError, IndexError):
                                    pass
                        elif channel == "trades":
                            for t in pl:
                                if isinstance(t, dict):
                                    px   = float(t.get("px",   0))
                                    sz   = float(t.get("sz",   0))
                                    side = t.get("side", "buy")
                                    if px > 0 and sz > 0:
                                        sig.update_trade_okx(px, sz, side)
            except Exception as e:
                log.error(f"[{spec.symbol}] OKX error: {e} — retry in 5s")
                await asyncio.sleep(5)

    # ─── Coinbase Advanced Trade WebSocket (primary — US-available) ─────────────

    async def run_coinbase(self, spec: AssetSpec) -> None:
        """Primary microstructure feed: market_trades + level2 from Coinbase Advanced Trade."""
        product_id = f"{spec.symbol}-USD"
        engine = self.engines[spec.symbol]
        sig = engine.signal

        def on_trade(price: float, size: float, side: str) -> None:
            sig.update_trade_coinbase(price, size, side)

        def on_book(bids: list, asks: list) -> None:
            sig.update_book_coinbase(bids, asks)
            if bids and asks:
                try:
                    best_bid = max(float(b[0]) for b in bids if len(b) >= 1 and float(b[0]) > 0)
                    best_ask = min(float(a[0]) for a in asks if len(a) >= 1 and float(a[0]) > 0)
                    if best_bid > 0 and best_ask > 0:
                        engine.synthetic_spot.update("coinbase", (best_bid + best_ask) / 2)
                except (ValueError, IndexError):
                    pass

        def on_mid(mid: float) -> None:
            engine.synthetic_spot.update("coinbase", mid)

        await run_coinbase_microstructure_feed(
            spec.symbol, product_id,
            on_trade=on_trade, on_book=on_book, on_mid=on_mid,
        )

    # ─── Kraken WebSocket ─────────────────────────────────────────────────────

    async def run_kraken(self, spec: AssetSpec) -> None:
        """Ticker feed for one asset from Kraken v2."""
        pair = KRAKEN_PAIRS.get(spec.symbol)
        if not pair:
            log.warning(f"[{spec.symbol}] No Kraken pair mapped — skipping Kraken feed")
            return
        engine = self.engines[spec.symbol]
        on_mid = lambda mid: engine.synthetic_spot.update("kraken", mid)
        await run_kraken_feed(spec.symbol, pair, on_mid)

    async def run_gemini(self, spec: AssetSpec) -> None:
        """BookTicker mid from Gemini (4th venue). Skips unlisted symbols (e.g. NEAR)."""
        pair = GEMINI_PAIRS.get(spec.symbol)
        if not pair:
            log.info(f"[{spec.symbol}] No Gemini pair — skipping Gemini feed")
            return
        engine = self.engines[spec.symbol]
        on_mid = lambda mid: engine.synthetic_spot.update("gemini", mid)
        await run_gemini_feed(spec.symbol, pair, on_mid)

    # ─── Kalshi price feed ────────────────────────────────────────────────────

    async def run_price_feed(self, engine: AssetEngine) -> None:
        """
        Poll Kalshi orderbook REST endpoint for YES mid-price every 1s.
        Calls engine.on_price_update() on each fresh price.

        Switching to Kalshi WebSocket: subscribe to `orderbook_delta` channel
        with the market ticker; parse bids/asks deltas into mid-price and call
        engine.on_price_update(mid) instead. See Kalshi API docs for the WS format.
        """
        spec = engine.spec
        log.info(f"[{spec.symbol}] Kalshi price feed: REST poll every 1s")
        _no_price_streak = 0
        _heartbeat = time.time()

        while True:
            try:
                # Window may have rolled; ensure we have a current ticker
                engine.on_window_advance()

                if engine._ticker:
                    yes_mid = self.kalshi.get_yes_mid(engine._ticker)
                    if yes_mid is not None:
                        _no_price_streak = 0
                        engine.on_price_update(yes_mid)
                    else:
                        _no_price_streak += 1
                        if _no_price_streak % 5 == 1:
                            log.info(f"[{spec.symbol}] WAIT: no_kalshi_price ({_no_price_streak} polls) — "
                                     f"market may be settling")
                            m = self.kalshi.find_active_market(spec.series_ticker)
                            if m:
                                engine._market = m
                                engine._ticker = m.get("ticker", "")
                                engine._close_time_utc = m.get("close_time")
                else:
                    _no_price_streak += 1
                    if _no_price_streak % 5 == 1:
                        m = self.kalshi.find_active_market(spec.series_ticker)
                        if m:
                            engine._market = m
                            engine._ticker = m.get("ticker", "")
                            engine._close_time_utc = m.get("close_time")

                # Heartbeat log every 30s
                now = time.time()
                if now - _heartbeat >= 30:
                    wid_str = datetime.fromtimestamp(
                        engine._get_window_id(), tz=timezone.utc
                    ).strftime("%H:%M")
                    t_left  = engine._time_remaining_secs()
                    log.info(
                        f"[{spec.symbol}] ♥ ticks={engine._total_ticks} "
                        f"window={wid_str} t_left={t_left:.0f}s "
                        f"ticker={engine._ticker or '?'}"
                    )
                    _heartbeat = now

            except Exception as e:
                log.warning(f"[{spec.symbol}] Price feed error: {e}")

            await asyncio.sleep(1)

    # ─── Heartbeat ────────────────────────────────────────────────────────────

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(120)
            self.print_status()
            self._snapshot_open_positions()
            write_live_config()
            self.sim.save()

    async def vault_poll(self) -> None:
        """Apply dashboard take-cash / vault-config commands every few seconds."""
        from .vault import process_vault_commands
        while True:
            await asyncio.sleep(5)
            try:
                if process_vault_commands(self.sim):
                    self.sim.save()
            except Exception as e:
                log.warning("vault_poll failed: %s", e)

    async def live_sync_poll(self) -> None:
        """Keep local bankroll glued to Kalshi available cash (live only)."""
        while not self._shutdown:
            await asyncio.sleep(float(getattr(cfg, "LIVE_BALANCE_SYNC_SECS", 10.0)))
            if cfg.DRY_RUN or self.live_guard is None:
                continue
            try:
                self.live_guard.maybe_sync(self._open_position_list(), force=True)
                self._snapshot_open_positions()
                self.sim.save()
            except Exception as e:
                log.warning("live_sync_poll failed: %s", e)

    async def control_poll(self) -> None:
        """Apply Telegram runtime commands (pause/resume/sizing/stop)."""
        while not self._shutdown:
            await asyncio.sleep(2)
            try:
                write_bot_heartbeat(
                    {
                        "balance": self.sim.balance,
                        "vault": self.sim.vault_balance,
                        "equity": self.sim.total_equity,
                    }
                )
                process_bot_commands(sim=self.sim)
                self._snapshot_open_positions()
                if stop_requested():
                    log.warning(
                        "Stop requested via runtime control (Telegram) — "
                        "archiving + Excel, then shutting down"
                    )
                    self._shutdown = True
                    try:
                        self._snapshot_open_positions()
                        write_live_config()
                        self.sim.save()
                        # Bridge already archives when bot is down; when bot is up,
                        # archive here and mark notice so Telegram gets one confirmation.
                        notice = archive_and_log_round(source="telegram_stop_bot")
                        log.info(
                            "Telegram /stop archived %s excel=%s",
                            notice.get("archive_dir"),
                            notice.get("excel_path"),
                        )
                    except Exception as e:
                        log.error("Telegram /stop archive failed: %s", e)
                    # Cancel sibling tasks so asyncio.run() can exit cleanly
                    current = asyncio.current_task()
                    for task in asyncio.all_tasks():
                        if task is not current and not task.done():
                            task.cancel()
                    return
            except Exception as e:
                log.warning("control_poll failed: %s", e)

    # ─── Venue warmup check ────────────────────────────────────────────────────

    async def _warmup_check(self) -> None:
        """After 60s, warn if any asset has fewer than 2 venues in synthetic_spot."""
        await asyncio.sleep(60)
        for asset, engine in self.engines.items():
            count = engine.synthetic_spot.source_count
            if count < 2:
                log.warning(f"[{asset}] Only {count} venue(s) after 60s warmup")

    # ─── Run ──────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.print_status()
        log.info("Starting Kalshi multi-asset bot...")
        # Ignore stale Telegram /stop (flag + queued commands) from a prior session
        prepare_for_new_round(source="bot_start")
        clear_stop(source="bot_start")
        clear_bot_command_queue()
        self._shutdown = False
        write_live_config()
        self._snapshot_open_positions()

        if not cfg.DRY_RUN:
            log.warning("⚠ LIVE MODE — enabling LiveGuard (Kalshi cash is source of truth)")
            self.live_guard = LiveGuard(self.kalshi, self.sim)
            ok, msg = self.live_guard.bootstrap()
            if not ok:
                log.error("Live bootstrap failed: %s — not starting engines", msg)
                self.sim.save()
                return
            log.info("LiveGuard: %s", msg)
            self.sim.save()

        self._verify_series()

        if not self.engines:
            log.error("No assets available. Check series tickers and Kalshi API.")
            self.sim.save()
            return

        tasks = []
        for sym, engine in self.engines.items():
            spec = engine.spec
            tasks.append(self.run_binance(spec))
            tasks.append(self.run_okx(spec))
            tasks.append(self.run_coinbase(spec))
            tasks.append(self.run_kraken(spec))
            tasks.append(self.run_gemini(spec))
            tasks.append(self.run_price_feed(engine))

        tasks.append(self.heartbeat())
        tasks.append(self.vault_poll())
        tasks.append(self.control_poll())
        if not cfg.DRY_RUN:
            tasks.append(self.live_sync_poll())
        tasks.append(self.recorder.run())
        tasks.append(self._warmup_check())

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("Bot tasks cancelled (shutdown).")
        finally:
            clear_stop(source="bot_exit")
            self._snapshot_open_positions()
            self.sim.save()

    # ─── Scan mode ────────────────────────────────────────────────────────────

    def scan(self) -> None:
        """One-shot check: verify series, find markets, print YES prices."""
        self.print_status()
        log.info("Scanning Kalshi for active 15-min markets...")
        for sym, engine in self.engines.items():
            spec = engine.spec
            exists = self.kalshi.verify_series(spec.series_ticker)
            if not exists:
                log.warning(f"[{sym}] Series {spec.series_ticker} NOT FOUND on Kalshi")
                continue
            m = self.kalshi.find_active_market(spec.series_ticker)
            if not m:
                log.warning(f"[{sym}] No active market right now for {spec.series_ticker}")
                continue
            ticker = m.get("ticker", "?")
            close  = m.get("close_time", "?")
            yes_mid = self.kalshi.get_yes_mid(ticker)
            yes_str = f"{yes_mid:.3f}" if yes_mid is not None else "N/A"
            log.info(f"[{sym}] ✓ ticker={ticker}  close={close}  YES_mid={yes_str}")
        self.sim.save()


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Kalshi Multi-Asset 15-Min Bot")
    parser.add_argument("--mode", choices=["scan", "run", "status"], default="scan")
    parser.add_argument("--live",      action="store_true", help="Enable live trading")
    parser.add_argument("--kelly",     type=float,          help="Override Kelly fraction")
    parser.add_argument("--max-pos",   type=float,          help="Override MAX_POS_PCT (e.g. 0.04 = 4%%)")
    parser.add_argument("--portfolio-cap", type=float,      help="Override PORTFOLIO_GROSS_CAP")
    parser.add_argument("--min-edge",  type=float,          help="Override min edge")
    parser.add_argument("--no-xrp",   action="store_true",  help="Disable XRP engine")
    parser.add_argument("--enable-xrp", action="store_true", help="Re-enable XRP (off by default)")
    parser.add_argument("--debug",    action="store_true",  help="Enable DEBUG logging (orderbook, etc.)")
    parser.add_argument(
        "--session-tag",
        default="round_21_max_risk_paper",
        help="Label for logs/session_meta.json (run mode only)",
    )
    parser.add_argument(
        "--fresh-round",
        action="store_true",
        help="Archive logs/ to sessions/, reset sim to SIM_BALANCE, then start",
    )
    parser.add_argument(
        "--sim-balance",
        type=float,
        default=None,
        help="Paper starting capital (e.g. 500). Applied before --fresh-round.",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help=f"Apply sizing preset: {', '.join(PROFILE_PRESETS)}",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        log.info("DEBUG logging enabled")
    if args.live:
        cfg.DRY_RUN = False
        log.warning("⚠  LIVE MODE — real money")
    if args.profile:
        ok, msg = apply_profile(args.profile)
        if not ok:
            raise SystemExit(msg)
        log.info("Profile applied: %s", msg)
    if args.kelly:
        cfg.KELLY_FRACTION = args.kelly
    if args.max_pos:
        cfg.MAX_POS_PCT = args.max_pos
    if args.portfolio_cap:
        cfg.PORTFOLIO_GROSS_CAP = args.portfolio_cap
    if args.min_edge:
        cfg.MIN_EDGE_PCT = args.min_edge
    if args.sim_balance is not None:
        if args.sim_balance <= 0:
            raise SystemExit("--sim-balance must be > 0")
        cfg.SIM_BALANCE = float(args.sim_balance)
        log.info("Paper starting capital set to $%.2f", cfg.SIM_BALANCE)
    if args.no_xrp:
        for spec in ASSETS:
            if spec.symbol == "XRP":
                spec.enabled = False
    if args.enable_xrp:
        for spec in ASSETS:
            if spec.symbol == "XRP":
                spec.enabled = True

    if args.fresh_round and args.mode == "run":
        archive_tag = args.session_tag.replace("round_", "archive_", 1)
        archived = prepare_fresh_round(archive_tag)
        log.info("Starting fresh round after archive → %s", archived)

    bot = KalshiMultiBot()

    if args.mode == "status":
        bot.print_status()
    elif args.mode == "scan":
        bot.scan()
    else:
        write_session_meta(session_tag=args.session_tag)
        try:
            asyncio.run(bot.run())
        except KeyboardInterrupt:
            log.info("Stopped.")
        except Exception:
            import traceback
            log.error("Bot crashed:\n%s", traceback.format_exc())
            raise
        finally:
            bot.sim.save()


if __name__ == "__main__":
    main()
