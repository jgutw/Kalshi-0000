"""Single-writer Shadow lifecycle journal with replay-validated counterfactual executions."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import uuid
from functools import wraps
from threading import RLock

from .shadow_settlement import RESEARCH_CLOSE_KEYS, research_close_payload


class JournalError(RuntimeError):
    pass


def _operation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        from .shadow import shadow_boundary
        with self._mutex, shadow_boundary():
            return method(self, *args, **kwargs)
    return guarded


def strict_loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Nonfinite JSON token")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Explicit nonempty identity required")
    return value


def _quantity(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Positive integer quantity required")
    return value


def _price(value, nullable=False):
    if value is None and nullable:
        return value
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Finite probability price in [0, 1] required")
    return value


def _spot(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("Positive finite spot required")
    return float(value)


def intent_request(*, asset, window_id_ts, decision_id, strategy, ticker, side, count,
                   limit_price=None, max_slippage=.10, client_order_id="", quotes=None):
    asset = _text(asset).upper()
    if not re.fullmatch(r"[A-Z0-9]+", asset):
        raise ValueError("Invalid asset")
    if type(window_id_ts) is not int or window_id_ts < 0:
        raise ValueError("Explicit nonnegative integer window_id_ts required")
    if side not in ("yes", "no"):
        raise ValueError("Explicit YES/NO side required")
    if not isinstance(client_order_id, str):
        raise ValueError("client_order_id must be a string")
    names = {"yes_bid", "yes_ask", "no_bid", "no_ask"}
    if quotes is None:
        quotes = {}
    if type(quotes) is not dict or not set(quotes) <= names:
        raise ValueError("Only explicit YES/NO quote names allowed")
    return dict(asset=asset, window_id_ts=window_id_ts, decision_id=_text(decision_id),
                strategy=_text(strategy), ticker=_text(ticker), side=side,
                count=_quantity(count), limit_price=_price(limit_price, True),
                max_slippage=_price(max_slippage), client_order_id=client_order_id,
                quotes={name: _price(quotes.get(name), True) for name in sorted(names)})


class ShadowJournal:
    """Kernel ownership lock held from replay to close; immutable journal is authority.

    Use through ShadowSession. Recovery never repairs or truncates bytes.
    """
    def __init__(self, session, *, create=False):
        from .shadow import _safe
        self.session = session
        self._mutex = RLock()
        self._closed = False
        self._poisoned = False
        self._stream = None
        self._lock = None
        self._locked = False
        self._state = dict(orders={}, positions={}, receipts={})
        self._seq = 0
        self._last_hash = "0" * 64
        self.path = _safe(session.directory / "lifecycle.jsonl")
        lock_path = _safe(session.directory / "writer.lock")
        try:
            self._lock = lock_path.open("a+b", buffering=0)
            if os.name == "nt":
                import msvcrt
                self._lock.seek(0)
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked = True
            self._stream = self.path.open("x+b" if create else "r+b", buffering=0)
            if create:
                self._sync()
            self._recover()
            # A complete record may have survived a lost/uncertain acknowledgement.
            # Establish a fresh durability barrier before exposing recovered retries.
            self._sync()
        except BaseException:
            self.close()
            raise

    def _check(self):
        from .shadow import _safe
        if self._closed or self._poisoned:
            raise JournalError("Writer closed or uncertain; close and recover before proceeding")
        if _safe(self.path).parent != self.session.directory:
            raise JournalError("Journal escaped session")

    def _key(self, request):
        return canonical(["shadow-entry-v1", self.session.session_id,
                          request["asset"], request["window_id_ts"]])

    @property
    @_operation
    def state(self):
        self._check()
        return deepcopy(self._state)

    def _apply(self, state, event):
        """Same validation for new events and replay; no normalization during replay."""
        p = event["payload"]
        kind = event["kind"]
        if kind == "INTENDED":
            if set(p) != {"request", "intended_order_id", "idempotency_key"}:
                raise ValueError("Invalid intended payload")
            request = intent_request(**p["request"])
            if canonical(request) != canonical(p["request"]) or p["idempotency_key"] != self._key(request):
                raise ValueError("Noncanonical intent identity")
            key = p["idempotency_key"]
            if key in state["orders"] or any(o["intended_order_id"] == p["intended_order_id"] for o in state["orders"].values()):
                raise ValueError("Duplicate durable intention")
            _text(p["intended_order_id"])
            state["orders"][key] = dict(p, status="INTENDED", timestamp_utc=event["timestamp_utc"],
                                       execution=None, position_id=None)
        elif kind == "EXECUTION":
            if set(p) != {"execution_id", "intended_order_id", "source", "quantity", "price", "position_id"}:
                raise ValueError("Invalid execution payload")
            _text(p["execution_id"])
            if p["source"] != "synthetic_test_input":
                raise ValueError("Ticket 2 only accepts explicitly synthetic execution inputs")
            receipt = "execution:" + p["execution_id"]
            if receipt in state["receipts"]:
                raise ValueError("Duplicate execution event")
            orders = [o for o in state["orders"].values() if o["intended_order_id"] == p["intended_order_id"]]
            if len(orders) != 1 or orders[0]["status"] != "INTENDED":
                raise ValueError("Execution requires one pending intention")
            order = orders[0]
            if type(p["quantity"]) is not int or p["quantity"] not in (0, order["request"]["count"]):
                raise ValueError("Ticket 2 supports explicit no-fill or full synthetic fill only")
            if p["quantity"] == 0:
                if p["price"] is not None or p["position_id"] is not None:
                    raise ValueError("No-fill cannot have a position/price")
                order["status"] = "NOT_FILLED"
            else:
                _price(p["price"])
                pos_id = _text(p["position_id"])
                if pos_id in state["positions"]:
                    raise ValueError("Duplicate position identity")
                state["positions"][pos_id] = dict(position_id=pos_id,
                    intended_order_id=order["intended_order_id"], idempotency_key=order["idempotency_key"],
                    request=deepcopy(order["request"]), execution_id=p["execution_id"],
                    quantity=p["quantity"], entry_price=p["price"], opened_utc=event["timestamp_utc"],
                    status="OPEN", close=None)
                order["status"] = "FILLED"
                order["position_id"] = pos_id
            order["execution"] = deepcopy(p)
            state["receipts"][receipt] = deepcopy(p)
        elif kind == "SIMULATED_EXECUTION":
            from .shadow_execution import simulate, restore_observation, FULL
            sim_keys = {"execution_id", "intended_order_id", "position_id", "result"}
            if type(p) is not dict or set(p) not in (sim_keys, sim_keys | {"price_to_beat"}):
                raise ValueError("Invalid simulated execution payload")
            orders = [o for o in state["orders"].values() if o["intended_order_id"] == p["intended_order_id"]]
            if len(orders) != 1 or orders[0]["status"] != "INTENDED":
                raise ValueError("Simulated execution requires one pending intention; duplicates forbidden")
            order = orders[0]
            result = p["result"]
            if type(result) is not dict:
                raise ValueError("Invalid simulated result")
            expected = simulate(order, restore_observation(result["observation"]),
                                yes_mid_poll_age_secs=result["yes_mid_poll_age_secs"],
                                book_exchange_timestamp=result["book_exchange_timestamp"])
            if canonical(result) != canonical(expected):
                raise ValueError("Simulated result does not match immutable observation/model")
            execution_id, position_id = self._simulation_ids(expected)
            if p["execution_id"] != execution_id or p["position_id"] != position_id:
                raise ValueError("Noncanonical simulated execution/position identity")
            receipt = "execution:" + execution_id
            if receipt in state["receipts"]:
                raise ValueError("Duplicate execution identity")
            if expected["disposition"] == FULL:
                if position_id in state["positions"]:
                    raise ValueError("Duplicate position identity")
                opened = dict(position_id=position_id,
                    intended_order_id=order["intended_order_id"], idempotency_key=order["idempotency_key"],
                    request=deepcopy(order["request"]), execution_id=execution_id,
                    quantity=expected["filled_quantity"], entry_price=expected["simulated_price"],
                    opened_utc=event["timestamp_utc"], status="OPEN", close=None,
                    source="shadow_execution_v1")
                if "price_to_beat" in p:
                    opened["price_to_beat"] = _spot(p["price_to_beat"])
                state["positions"][position_id] = opened
                order["status"] = "FILLED"
                order["position_id"] = position_id
            else:
                order["status"] = "NOT_FILLED"
            order["execution"] = deepcopy(p)
            state["receipts"][receipt] = deepcopy(p)
        elif kind == "CLOSED" and set(p) == RESEARCH_CLOSE_KEYS:
            receipt = "close:" + _text(p["close_id"])
            if receipt in state["receipts"]:
                raise ValueError("Duplicate close event")
            position = state["positions"].get(p["position_id"])
            if position is None or position["status"] != "OPEN":
                raise ValueError("Close requires an open position")
            expected = research_close_payload(
                position, observed_spot=p["observed_spot"], observation_unix=p["observation_unix"],
            )
            if canonical(expected) != canonical(p):
                raise ValueError("Research settlement does not match the open position")
            position["status"] = "CLOSED"
            position["close"] = dict(p, timestamp_utc=event["timestamp_utc"])
            state["receipts"][receipt] = deepcopy(p)
        elif kind == "CLOSED":
            if set(p) != {"close_id", "position_id", "reason", "outcome", "outcome_source"}:
                raise ValueError("Invalid close payload")
            receipt = "close:" + _text(p["close_id"])
            _text(p["reason"])
            if receipt in state["receipts"]:
                raise ValueError("Duplicate close event")
            position = state["positions"].get(p["position_id"])
            if position is None or position["status"] != "OPEN":
                raise ValueError("Close requires an open position")
            if p["outcome"] is None:
                if p["outcome_source"] is not None:
                    raise ValueError("Outcome source without outcome")
            elif p["outcome"] not in ("YES", "NO") or p["outcome_source"] != "synthetic_test_input":
                raise ValueError("Only explicitly synthetic outcomes in Ticket 2")
            position["status"] = "CLOSED"
            position["close"] = dict(p, timestamp_utc=event["timestamp_utc"])
            state["receipts"][receipt] = deepcopy(p)
        else:
            raise ValueError("Unknown lifecycle event")

    def _recover(self):
        self._stream.seek(0)
        for number, raw in enumerate(self._stream, 1):
            try:
                if not raw.endswith(b"\n") or raw == b"\n":
                    raise ValueError("Incomplete/blank physical record")
                event = strict_loads(raw.decode("utf-8"))
                fields = {"schema_version", "mode", "shadow_session_id", "code_sha", "seq", "event_id",
                          "timestamp_utc", "kind", "payload", "previous_hash", "sha256"}
                if type(event) is not dict or set(event) != fields:
                    raise ValueError("Invalid event envelope")
                if type(event["schema_version"]) is not int or event["schema_version"] != 2 or event["mode"] != "SHADOW":
                    raise ValueError("Unsupported journal schema/mode")
                if event["shadow_session_id"] != self.session.session_id or event["code_sha"] != self.session.metadata["code_sha"]:
                    raise ValueError("Session/code provenance mismatch")
                if type(event["seq"]) is not int or event["seq"] != self._seq + 1 or event["event_id"] != f"{self.session.session_id}:{event['seq']}":
                    raise ValueError("Invalid event sequence")
                timestamp = datetime.fromisoformat(event["timestamp_utc"])
                if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
                    raise ValueError("UTC event timestamp required")
                digest = event.pop("sha256")
                if event["previous_hash"] != self._last_hash or hashlib.sha256(canonical(event).encode()).hexdigest() != digest:
                    raise ValueError("Journal chain mismatch")
                self._apply(self._state, event)
                self._seq = event["seq"]
                self._last_hash = digest
            except Exception as exc:
                raise JournalError(f"Invalid journal record {number}; no repair performed: {exc}") from exc
        self._stream.seek(0, os.SEEK_END)

    def _write_bytes(self, data):
        written = self._stream.write(data)
        if written != len(data):
            raise OSError("Short journal write")

    def _sync(self):
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def _publish(self, state):
        self._state = state

    def _append(self, kind, payload):
        self._check()
        event = dict(schema_version=2, mode="SHADOW", shadow_session_id=self.session.session_id,
                     code_sha=self.session.metadata["code_sha"], seq=self._seq + 1,
                     event_id=f"{self.session.session_id}:{self._seq + 1}",
                     timestamp_utc=datetime.now(timezone.utc).isoformat(), kind=kind, payload=payload,
                     previous_hash=self._last_hash)
        next_state = deepcopy(self._state)
        self._apply(next_state, event)  # Validate before any bytes or key consumption.
        digest = hashlib.sha256(canonical(event).encode()).hexdigest()
        event["sha256"] = digest
        data = (canonical(event) + "\n").encode("utf-8")
        try:
            self._write_bytes(data)
            self._sync()
            self._publish(next_state)
            self._seq += 1
            self._last_hash = digest
        except BaseException:
            self._poisoned = True
            raise

    @_operation
    def intend(self, **values):
        self._check()
        request = intent_request(**values)
        key = self._key(request)
        existing = self._state["orders"].get(key)
        if existing:
            if canonical(existing["request"]) != canonical(request):
                raise JournalError("Conflicting reuse of consumed asset/window")
            return deepcopy(existing)
        self._append("INTENDED", dict(request=request, idempotency_key=key, intended_order_id=str(uuid.uuid4())))
        return deepcopy(self._state["orders"][key])

    @_operation
    def synthetic_execution(self, *, execution_id, intended_order_id, quantity, price=None):
        self._check()
        receipt = self._state["receipts"].get("execution:" + _text(execution_id))
        p = dict(execution_id=execution_id, intended_order_id=intended_order_id,
                 quantity=quantity, price=price, source="synthetic_test_input",
                 position_id=receipt["position_id"] if receipt else (str(uuid.uuid4()) if quantity else None))
        if receipt:
            if canonical(receipt) != canonical(p):
                raise JournalError("Conflicting execution retry")
            return deepcopy(receipt)
        self._append("EXECUTION", p)
        return deepcopy(p)

    @staticmethod
    def _simulation_ids(result):
        digest = hashlib.sha256(canonical(result).encode("utf-8")).hexdigest()
        execution_id = "shadow-simulation-v1:" + digest
        position_id = "shadow-position-v1:" + digest if result["filled_quantity"] else None
        return execution_id, position_id

    @_operation
    def simulate_execution(self, intended_order_id, book_snapshot, *,
                           yes_mid_poll_age_secs=None, book_exchange_timestamp=None,
                           price_to_beat=None):
        """One caller-supplied snapshot, no fetch. Duplicate execution always fails.

        price_to_beat is fill-time strike provenance. It is not part of the
        execution-model hash, and older events omit it.
        """
        from .shadow_execution import simulate
        self._check()
        _text(intended_order_id)
        orders = [o for o in self._state["orders"].values() if o["intended_order_id"] == intended_order_id]
        if len(orders) != 1 or orders[0]["status"] != "INTENDED":
            raise JournalError("Simulated execution requires one pending intention; duplicates forbidden")
        result = simulate(orders[0], book_snapshot, yes_mid_poll_age_secs=yes_mid_poll_age_secs,
                          book_exchange_timestamp=book_exchange_timestamp)
        execution_id, position_id = self._simulation_ids(result)
        payload = dict(execution_id=execution_id, intended_order_id=intended_order_id,
                       position_id=position_id, result=result)
        if price_to_beat is not None:
            payload["price_to_beat"] = _spot(price_to_beat)
        self._append("SIMULATED_EXECUTION", payload)
        return deepcopy(payload)

    @_operation
    def settle_research(self, position_id, *, observed_spot, observation_unix):
        """Append one research CLOSE, or return the identical close already stored."""
        self._check()
        position = self._state["positions"].get(position_id)
        if position is None:
            raise JournalError("Close requires an open position")
        payload = research_close_payload(
            position, observed_spot=observed_spot, observation_unix=observation_unix,
        )
        receipt = self._state["receipts"].get("close:" + payload["close_id"])
        if receipt:
            if canonical(receipt) != canonical(payload):
                raise JournalError("Conflicting close retry")
            return deepcopy(receipt)
        if position["status"] != "OPEN":
            raise JournalError("Close requires an open position")
        self._append("CLOSED", payload)
        return deepcopy(payload)

    @_operation
    def close_position(self, *, close_id, position_id, reason, outcome=None, outcome_source=None):
        self._check()
        p = dict(close_id=_text(close_id), position_id=position_id, reason=reason,
                 outcome=outcome, outcome_source=outcome_source)
        receipt = self._state["receipts"].get("close:" + close_id)
        if receipt:
            if canonical(receipt) != canonical(p):
                raise JournalError("Conflicting close retry")
            return deepcopy(receipt)
        self._append("CLOSED", p)
        return deepcopy(p)

    @_operation
    def close(self):
        self._closed = True
        try:
            if self._stream:
                self._stream.close()
        finally:
            if self._lock:
                try:
                    if self._locked:
                        if os.name == "nt":
                            import msvcrt
                            self._lock.seek(0)
                            msvcrt.locking(self._lock.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
                finally:
                    self._lock.close()
                    self._lock = None
                    self._locked = False
