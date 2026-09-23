"""Offline Shadow boundary. No production client, credentials, or network transport.

Feed acquisition and simulator wiring are deliberately not part of Ticket 1.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import uuid


_SHADOW = ContextVar("shadow_boundary", default=False)
PROTECTED = {"logs", "sessions", "research_data", ".env", "p6c_d1_validation"}
ARTIFACTS = {"decisions", "intended_orders", "simulated_fills", "positions",
             "trades", "outcomes", "operational_events", "research_observations"}


def reject_live_start_in_shadow():
    if _SHADOW.get():
        raise RuntimeError("Live start/funding is forbidden inside Shadow")


@contextmanager
def shadow_boundary():
    """Guard legacy live-start calls; not an OS sandbox or a feed runner."""
    token = _SHADOW.set(True)
    try:
        yield
    finally:
        _SHADOW.reset(token)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("Expected a simple session identifier")
    if value.casefold() in PROTECTED or value.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
        *(f"LPT{i}" for i in range(10))}:
        raise ValueError("Reserved/protected identifier")
    return value


def _safe(path):
    path = Path(path).absolute()
    resolved = path.resolve()
    if any(p.casefold() in PROTECTED for p in (*path.parts, *resolved.parts)):
        raise ValueError("Protected runtime path")
    # Reject existing symlinks/junctions, including parents, not just escapes.
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("Shadow roots cannot traverse links/junctions")
    return resolved


class ShadowSession:
    """Explicit initialization only; import has no filesystem side effects."""
    def __init__(self, root, shadow_session_id, session_tag, code_sha, *,
                 live=False, fresh_round=False, config_identity=None):
        if live or fresh_round:
            raise ValueError("Shadow rejects --live and --fresh-round")
        self.session_id = _identifier(shadow_session_id)
        tag = _identifier(session_tag)
        if not isinstance(code_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", code_sha):
            raise ValueError("Explicit full code SHA required")
        self.root = _safe(root)
        if self.root.name != "shadow_data":
            raise ValueError("Dedicated root must be named shadow_data")
        self.directory = self.root / self.session_id
        config_hash = None
        if config_identity is not None:
            # Caller-supplied public configuration only; never load environment.
            encoded = json.dumps(config_identity, sort_keys=True, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8")
            config_hash = hashlib.sha256(encoded).hexdigest()
        self.metadata = dict(schema_version=1, mode="SHADOW", shadow_session_id=self.session_id,
                             session_tag=tag, startup_utc=datetime.now(timezone.utc).isoformat(),
                             code_sha=code_sha, config_sha256=config_hash,
                             config_hash_scope="caller-supplied public JSON; null means unavailable")
        self.root.mkdir(parents=True, exist_ok=True)
        self.directory.mkdir(exist_ok=False)
        self._write("session.json", self.metadata, "x")

    def _write(self, name, value, mode):
        path = _safe(self.directory / name)
        if path.parent != self.directory or _safe(self.root) != self.root:
            raise ValueError("Shadow artifact escaped session")
        payload = json.dumps(value, allow_nan=False, sort_keys=True) + "\n"
        with path.open(mode, encoding="utf-8") as stream:
            stream.write(payload)

    def append(self, artifact, event):
        if artifact not in ARTIFACTS:
            raise ValueError("Unknown Shadow artifact")
        self._write(artifact + ".jsonl", dict(event, mode="SHADOW",
                    shadow_session_id=self.session_id, schema_version=1), "a")


@dataclass(frozen=True)
class IntendedOrderResult:
    order_id: str
    raw: dict
    ok: bool = False
    fill_count: int = 0
    entry_price: float = 0.0
    fees: float = 0.0
    cost: float = 0.0

    def __bool__(self):
        return False  # An intent is never a simulated or real fill.


class ShadowClient:
    """Snapshot-backed market reads and local order intents, with no HTTP object.

    No inheritance/delegation from KalshiClient. Unknown write operations fail
    by absence, including _post, transfer, cancel, amend and batch operations.
    """
    __slots__ = ("session", "_markets", "_books")

    def __init__(self, session, *, markets=None, books=None):
        if type(session) is not ShadowSession:
            raise TypeError("ShadowSession required")
        # JSON boundary prevents injection of a client/transport as snapshot data.
        self._markets = json.loads(json.dumps(markets or {}, allow_nan=False))
        self._books = json.loads(json.dumps(books or {}, allow_nan=False))
        self.session = session

    def get_market(self, ticker):
        return deepcopy(self._markets.get(ticker))

    def get_orderbook(self, ticker, depth=100):
        return deepcopy(self._books.get(ticker, {}))

    def place_market_order(self, ticker, side, count, client_order_id="",
                           limit_price=None, max_slippage=0.10):
        if not isinstance(ticker, str) or not ticker or side not in {"yes", "no"}:
            raise ValueError("Invalid intended order")
        if type(count) is not int or count <= 0:
            raise ValueError("Positive integer contract count required")
        for value in (limit_price, max_slippage):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError("Finite intended price/slippage required")
        event = dict(event_type="intended_order", intended_order_id=str(uuid.uuid4()),
                     timestamp_utc=datetime.now(timezone.utc).isoformat(), ticker=ticker,
                     side=side, count=count, client_order_id=client_order_id,
                     limit_price=limit_price, max_slippage=max_slippage)
        self.session.append("intended_orders", event)
        return IntendedOrderResult(event["intended_order_id"], event)
