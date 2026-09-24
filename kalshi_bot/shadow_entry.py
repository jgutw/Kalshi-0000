"""Shadow entry orchestration. Calls make_decision; never _execute or on_price_update.

on_price_update logs under logs/ and calls _execute. on_window_advance settles
and saves SimState. Those methods stay untouched. This module reproduces the
safe prefix (staleness, lag, window identity, price-to-beat) and then the
durable intend-before-book sequence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
import time
import uuid

from .asset_engine import _floor_strike_from_market
from .config import cfg
from .shadow_journal import JournalError, strict_loads
from .shadow_report import _chain
from .shadow_market import BookIdentityError
from .shadow_settlement import SETTLEMENT_METHOD, window_close_ts
from .sim_state import OpenPosition, SimState

log = logging.getLogger("kalshi_bot.shadow_entry")


@dataclass(frozen=True)
class IntendedEntry:
    side: str
    price: float
    count: int


def entry_from_decision(decision: dict, yes_price: float, *, p_base_min: float,
                        min_entry: float, max_entry: float,
                        spot_confidence_min: float) -> tuple[IntendedEntry | None, str | None]:
    """Production _execute construction, including its pre-order aborts.

    The divisor is max(entry, 0.01), matching de24128. The 0.02 entry floor
    makes that cap equal to entry for every order production will submit.
    The live available-cash shrink is not applied: Shadow has no Kalshi balance.
    """
    spot_conf = decision.get("spot_confidence")
    if spot_conf is not None and spot_conf < spot_confidence_min:
        return None, "spot_confidence"
    p_real = decision.get("p_real")
    if p_real is not None and 0.48 <= p_real <= 0.52:
        return None, "p_real_band"
    action = decision.get("action")
    if action == "BUY_YES":
        side = "yes"
        entry_price = yes_price
    elif action == "BUY_NO":
        side = "no"
        entry_price = 1.0 - yes_price
    else:
        return None, "not_actionable"
    if entry_price < p_base_min:
        return None, "below_p_base_min"
    if entry_price < min_entry or entry_price > max_entry:
        return None, "outside_entry_band"
    contracts = max(1, int(decision["size_usd"] / max(entry_price, 0.01)))
    return IntendedEntry(side, entry_price, contracts), None


def _durable_strike(value):
    """Fill-time spot strike, or None when the engine has no positive finite strike."""
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def production_entry(decision: dict, yes_price: float) -> tuple[IntendedEntry | None, str | None]:
    return entry_from_decision(
        decision, yes_price,
        p_base_min=cfg.P_BASE_MIN,
        min_entry=getattr(cfg, "MIN_ENTRY_PRICE", 0.0),
        max_entry=getattr(cfg, "MAX_ENTRY_PRICE", 1.0),
        spot_confidence_min=cfg.SPOT_CONFIDENCE_MIN,
    )


def private_sim(starting_balance: float) -> SimState:
    amount = float(starting_balance)
    return SimState(
        balance=amount,
        starting_balance=amount,
        peak_balance=amount,
        daily_start=amount,
        peak_equity=amount,
    )


def record_starting_balance(session, amount: float) -> None:
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
        raise ValueError("Explicit positive Shadow starting balance required")
    session.append("operational_events", {
        "event": "starting_balance",
        "starting_balance": float(amount),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    })


def realized_gross_equity(session) -> tuple[float | None, str | None]:
    """E_t from the starting-balance record plus verified CLOSED gross P&L.

    A torn final line keeps the verified prefix. Corruption, a nonfinite close,
    or E_t <= 0 blocks new risk. There is no fallback to the starting balance.
    """
    try:
        start = load_starting_balance(session)
    except ValueError:
        return None, "sizing_blocked:starting_balance"
    path = session.directory / "lifecycle.jsonl"
    lines = []
    if path.is_file():
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    events, chain_ok, _torn, _note = _chain(lines)
    if not chain_ok:
        return None, "sizing_blocked:lifecycle_integrity"
    total = 0.0
    seen = set()
    for event in events:
        if event.get("kind") != "CLOSED":
            continue
        payload = event.get("payload")
        if type(payload) is not dict:
            return None, "sizing_blocked:invalid_close_pnl"
        close_id = payload.get("close_id")
        if close_id in seen:
            return None, "sizing_blocked:duplicate_close"
        if close_id is not None:
            seen.add(close_id)
        pnl = payload.get("gross_pnl")
        if isinstance(pnl, bool) or type(pnl) not in (int, float) or not math.isfinite(pnl):
            return None, "sizing_blocked:invalid_close_pnl"
        total += float(pnl)
    equity = start + total
    if not math.isfinite(equity):
        return None, "sizing_blocked:nonfinite_equity"
    if equity <= 0:
        return None, "sizing_blocked:nonpositive_equity"
    return equity, None


def load_starting_balance(session) -> float:
    path = session.directory / "operational_events.jsonl"
    if not path.is_file():
        raise ValueError("Recovered session has no starting-balance provenance")
    found = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        event = strict_loads(line)
        if event.get("event") != "starting_balance":
            continue
        if found is not None:
            raise ValueError("Multiple starting-balance records")
        found = event.get("starting_balance")
    if isinstance(found, bool) or not isinstance(found, (int, float)) or not math.isfinite(found) or found <= 0:
        raise ValueError("Recovered starting balance is missing or invalid")
    return float(found)


class ShadowEntry:
    def __init__(self, session, market):
        self.session = session
        self.market = market

    def operational(self, event: str, **fields) -> None:
        payload = {"event": event, "timestamp_utc": datetime.now(timezone.utc).isoformat()}
        payload.update(fields)
        self.session.append("operational_events", payload)
        log.warning("shadow %s %s", event, fields)

    def refresh_window(self, engine) -> str | None:
        """Local window-roll bookkeeping. Returns a series ticker when a lookup is required.

        The Kalshi GET stays with the caller so the event loop can run it off-thread.
        Does not resolve, save, or research-finalize.
        """
        wid = engine._get_window_id()
        if wid == engine._window_id:
            return None
        if engine._open_pos is not None:
            self.operational(
                "window_rolled_position_remains_open",
                asset=engine.spec.symbol,
                previous_window_id_ts=engine._window_id,
                window_id_ts=wid,
            )
        engine._window_id = wid
        engine._window_start = float(wid)
        engine._last_decision = None
        engine._window_fills = []
        return engine.spec.series_ticker

    def apply_active_market(self, engine, market) -> None:
        """Install a fetched market and price-to-beat. No network."""
        if market:
            engine._market = market
            engine._ticker = market.get("ticker", "")
            engine._close_time_utc = market.get("close_time")
        else:
            engine._market = None
            engine._ticker = ""
            engine._close_time_utc = None
        engine._price_to_beat = None
        engine._price_to_beat_source = ""
        floor = _floor_strike_from_market(engine._market)
        if floor is not None:
            engine._price_to_beat = floor
            engine._price_to_beat_source = "floor_strike"
        else:
            spot = engine.synthetic_spot.spot_mid
            if spot is not None and spot > 0:
                engine._price_to_beat = float(spot)
                engine._price_to_beat_source = "window_open_spot"
            elif engine.signal.prices:
                price = float(engine.signal.prices[-1])
                if price > 0:
                    engine._price_to_beat = price
                    engine._price_to_beat_source = "window_open_spot"

    def _apply_sizing_equity(self, engine) -> None:
        """Replace the frozen bankroll with realized gross equity. Never mutates sim.balance.

        Dynamic sizing is this code path. ``sizing_basis`` is reporting provenance
        only, so this build must not be resumed against an Era 1B directory.
        ``sizing_blocked`` dedup is process-local. Daily-loss and drawdown halts
        still watch frozen sim.balance and do not track realized gross P&L.
        """
        equity, reason = realized_gross_equity(self.session)
        engine._sizing_equity = equity
        engine._sizing_block = reason
        if reason and getattr(engine, "_sizing_block_noted", None) != reason:
            self.operational("sizing_blocked", asset=engine.spec.symbol, reason=reason)
            engine._sizing_block_noted = reason

    def evaluate(self, engine, yes_prob: float, now: float | None = None) -> tuple[dict, dict | None]:
        """Decision and durable INTENDED, without the execution-book GET."""
        now = time.time() if now is None else now
        if engine.signal.prices:
            engine.lag_tracker.update(float(engine.signal.prices[-1]), yes_prob)
        if engine._last_price_val is not None:
            unchanged = abs(yes_prob - engine._last_price_val) < 1e-6
            if unchanged and now - engine._last_price_ts > cfg.PRICE_MAX_AGE_SECS:
                decision = engine._wait("stale_price", yes_prob)
                return decision, self.stage_submission(engine, decision, yes_prob)
        engine._last_price_ts = now
        engine._last_price_val = yes_prob
        engine._kalshi_prob_history.append((now, yes_prob))
        if not engine._ticker:
            decision = engine._wait("no_market", yes_prob)
            return decision, self.stage_submission(engine, decision, yes_prob)
        engine._total_ticks += 1
        self._apply_sizing_equity(engine)
        decision = engine.make_decision(yes_prob)
        return decision, self.stage_submission(engine, decision, yes_prob)

    def on_yes_mid(self, engine, yes_prob: float, now: float | None = None) -> dict:
        """Decision cadence without _execute, early exit, or the production decision log."""
        decision, staged = self.evaluate(engine, yes_prob, now)
        if staged is not None:
            self.observe_book(engine, staged)
        return decision

    def _record(self, engine, decision: dict, yes_mid: float) -> None:
        if not decision.get("decision_id"):
            decision["decision_id"] = uuid.uuid4().hex
        window = engine._window_id if isinstance(engine._window_id, int) and engine._window_id >= 0 else None
        self.session.append("decisions", {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "code_sha": self.session.metadata["code_sha"],
            "asset": engine.spec.symbol,
            "ticker": engine._ticker or None,
            "window_id_ts": window,
            "action": decision.get("action"),
            "reason": decision.get("reason"),
            "strategy": decision.get("strategy"),
            "size_usd": decision.get("size_usd"),
            "p_base": decision.get("p_base"),
            "p_real": decision.get("p_real"),
            "p_market": decision.get("p_market"),
            "yes_price_raw": yes_mid,
            "decision_id": decision.get("decision_id"),
        })

    def _asset_is_open(self, asset: str) -> bool:
        state = self.session.journal.state
        asset = asset.upper()
        for position in state["positions"].values():
            if position.get("status") == "OPEN" and position["request"]["asset"] == asset:
                return True
        return False

    def restore_open_positions(self, engines: dict) -> None:
        state = self.session.journal.state
        for position in state["positions"].values():
            if position.get("status") != "OPEN":
                continue
            asset = position["request"]["asset"]
            engine = engines.get(asset)
            if engine is None:
                continue
            request = position["request"]
            engine._open_pos = OpenPosition(
                window_id=request["window_id_ts"],
                market_ticker=request["ticker"],
                asset=asset,
                side=request["side"],
                entry_price=float(position["entry_price"]),
                contracts=int(position["quantity"]),
                amount_usdc=float(position["entry_price"]) * int(position["quantity"]),
                entered_at=time.time(),
                price_to_beat=position.get("price_to_beat"),
                order_id=position["position_id"],
                decision_id=request["decision_id"],
            )

    def submit(self, engine, decision: dict, yes_mid: float, *, poll_age: float | None = None) -> dict:
        staged = self.stage_submission(engine, decision, yes_mid, poll_age=poll_age)
        if staged is not None:
            self.observe_book(engine, staged)
        return decision

    def stage_submission(self, engine, decision: dict, yes_mid: float, *, poll_age: float | None = None) -> dict | None:
        """Record the decision and journal INTENDED before any execution-book read."""
        self._record(engine, decision, yes_mid)
        action = decision.get("action")
        if action == "WAIT" or action not in ("BUY_YES", "BUY_NO"):
            return None
        if engine._open_pos is not None or self._asset_is_open(engine.spec.symbol):
            self.operational("open_position_blocks_entry", asset=engine.spec.symbol,
                             decision_id=decision.get("decision_id"))
            return None
        try:
            entry, reason = production_entry(decision, yes_mid)
        except (TypeError, ValueError, KeyError, ZeroDivisionError) as exc:
            self.operational("entry_not_constructed", asset=engine.spec.symbol,
                             decision_id=decision.get("decision_id"), error=str(exc))
            return None
        if entry is None:
            self.operational("entry_not_constructed", asset=engine.spec.symbol, reason=reason,
                             decision_id=decision.get("decision_id"))
            return None
        if not isinstance(engine._window_id, int) or engine._window_id < 0 or not engine._ticker:
            self.operational("entry_identity_unavailable", asset=engine.spec.symbol,
                             decision_id=decision.get("decision_id"))
            return None
        strategy = decision.get("strategy")
        if not isinstance(strategy, str) or not strategy.strip():
            self.operational("entry_strategy_unavailable", asset=engine.spec.symbol,
                             decision_id=decision.get("decision_id"))
            return None
        frozen_ticker = engine._ticker
        frozen_window = engine._window_id
        ids_before = {
            order["intended_order_id"]
            for order in self.session.journal.state["orders"].values()
        }
        try:
            order = self.session.journal.intend(
                asset=engine.spec.symbol,
                window_id_ts=frozen_window,
                decision_id=decision["decision_id"],
                strategy=strategy,
                ticker=frozen_ticker,
                side=entry.side,
                count=entry.count,
                limit_price=entry.price,
            )
        except (JournalError, ValueError) as exc:
            self.operational("journal_intend_failed", asset=engine.spec.symbol,
                             decision_id=decision.get("decision_id"), error=str(exc))
            return None
        if order["intended_order_id"] in ids_before or order.get("status") != "INTENDED" or order.get("execution"):
            self.operational(
                "intention_already_consumed",
                asset=engine.spec.symbol,
                intended_order_id=order["intended_order_id"],
                status=order.get("status"),
            )
            return None
        return {
            "order": order,
            "ticker": frozen_ticker,
            "window_id": frozen_window,
            "poll_age": poll_age,
        }

    def note_book_failure(self, staged: dict, exc: Exception) -> None:
        order_id = staged["order"]["intended_order_id"]
        ticker = staged["ticker"]
        if isinstance(exc, BookIdentityError):
            self.operational("execution_book_identity_mismatch", intended_order_id=order_id,
                             ticker=ticker, error=str(exc))
            return
        self.operational("execution_book_failed", intended_order_id=order_id,
                         ticker=ticker, error=str(exc))

    def observe_book(self, engine, staged: dict) -> None:
        try:
            book = self.market.fetch_execution_book(staged["ticker"])
        except Exception as exc:
            self.note_book_failure(staged, exc)
            return
        self.apply_execution_book(engine, staged, book)

    def apply_execution_book(self, engine, staged: dict, book) -> None:
        order = staged["order"]
        ticker = staged["ticker"]
        window_id = staged["window_id"]
        poll_age = staged["poll_age"]
        order_id = order["intended_order_id"]
        request = order["request"]
        if request["ticker"] != ticker or request["window_id_ts"] != window_id:
            self.operational("execution_book_identity_mismatch", intended_order_id=order_id,
                             ticker=ticker)
            return
        if request["asset"] != engine.spec.symbol.upper():
            self.operational("execution_book_identity_mismatch", intended_order_id=order_id,
                             asset=engine.spec.symbol)
            return
        try:
            payload = self.session.simulate_execution(
                order_id, book, yes_mid_poll_age_secs=poll_age,
                price_to_beat=_durable_strike(engine._price_to_beat),
            )
        except (JournalError, ValueError) as exc:
            self.operational("simulation_rejected", intended_order_id=order_id, error=str(exc))
            return
        result = payload["result"]
        if result.get("disposition") == "SIMULATED_FULL_FILL":
            engine._open_pos = OpenPosition(
                window_id=window_id,
                market_ticker=ticker,
                asset=engine.spec.symbol,
                side=request["side"],
                entry_price=float(result["simulated_price"]),
                contracts=int(result["filled_quantity"]),
                amount_usdc=float(result["simulated_price"]) * int(result["filled_quantity"]),
                entered_at=time.time(),
                price_to_beat=payload.get("price_to_beat"),
                order_id=str(payload.get("position_id") or ""),
                decision_id=request["decision_id"],
                decision=None,
            )

    def settle_if_due(self, engine, now: float | None = None) -> None:
        """Close one open position on the first valid spot at or after its window boundary.

        Uses the strike stored with the fill. Does not read the engine's current
        price_to_beat, which a window roll may already have replaced.
        """
        pos = engine._open_pos
        if pos is None or not pos.order_id:
            return
        now = time.time() if now is None else now
        position = self.session.journal.state["positions"].get(pos.order_id)
        if position is None:
            self._settlement_error(engine, pos, ValueError("Open position is not in the journal"))
            return
        if position["status"] == "CLOSED":
            engine._open_pos = None
            return
        if position["status"] != "OPEN":
            self._settlement_error(engine, pos, ValueError("Position is not open"))
            return
        if position.get("price_to_beat") is None:
            self._settlement_error(engine, pos, ValueError("Open position has no durable price_to_beat"))
            return
        try:
            close_ts = window_close_ts(position["request"]["window_id_ts"])
        except (TypeError, ValueError) as exc:
            self._settlement_error(engine, pos, exc)
            return
        if now < close_ts:
            return
        spot = getattr(engine.synthetic_spot, "spot_mid", None)
        if type(spot) not in (int, float) or not math.isfinite(spot) or spot <= 0:
            if not getattr(engine, "_settlement_wait_noted", False):
                self.operational(
                    "settlement_waiting",
                    asset=engine.spec.symbol,
                    position_id=pos.order_id,
                    window_id_ts=pos.window_id,
                )
                engine._settlement_wait_noted = True
            return
        try:
            closed = self.session.journal.settle_research(
                pos.order_id, observed_spot=spot, observation_unix=now,
            )
        except (JournalError, ValueError) as exc:
            self._settlement_error(engine, pos, exc)
            return
        engine._open_pos = None
        engine._settlement_wait_noted = False
        self.operational(
            "shadow_position_closed",
            asset=engine.spec.symbol,
            position_id=pos.order_id,
            window_id_ts=pos.window_id,
            yes_settled=closed["yes_settled"],
            side_payoff=closed["side_payoff"],
            gross_pnl=closed["gross_pnl"],
            settlement_method=SETTLEMENT_METHOD,
        )

    def _settlement_error(self, engine, pos, exc: Exception) -> None:
        if getattr(engine, "_settlement_error_noted", False):
            return
        self.operational(
            "settlement_error",
            asset=engine.spec.symbol,
            position_id=getattr(pos, "order_id", None),
            window_id_ts=getattr(pos, "window_id", None),
            error=str(exc),
        )
        engine._settlement_error_noted = True
