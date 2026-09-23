"""Pure deterministic counterfactual execution; no feeds, clock, journal or transport."""
from decimal import Decimal, localcontext
import math


MODEL_ID = "shadow_execution_v1"
FULL = "SIMULATED_FULL_FILL"
NOT_MARKETABLE = "NO_FILL_NOT_MARKETABLE"
NO_LIQUIDITY = "NO_FILL_NO_DISPLAYED_LIQUIDITY"
INVALID_BOOK = "NO_FILL_INVALID_BOOK"


def observation(book):
    """Detach JSON-like input; retain nonfinite evidence without nonstandard JSON.

    Paths identify replaced float values, never missing quotes. Unsupported Python
    objects/non-string map keys are API errors, not silently discarded evidence.
    """
    nonfinite = []

    def visit(value, path):
        if type(value) is float and not math.isfinite(value):
            kind = "nan" if math.isnan(value) else ("positive_infinity" if value > 0 else "negative_infinity")
            nonfinite.append(dict(path=path, kind=kind))
            return None
        if value is None or type(value) in (str, bool, int, float):
            return value
        if type(value) is list:
            return [visit(v, path + [i]) for i, v in enumerate(value)]
        if type(value) is dict and all(type(k) is str for k in value):
            return {k: visit(value[k], path + [k]) for k in sorted(value)}
        raise ValueError("Book must contain JSON-like data only")

    clean = visit(book, [])
    return dict(format="shadow_book_observation_v1", book=clean, nonfinite=nonfinite)


def restore_observation(evidence):
    """Decode persisted evidence for replay; model round-trip checks canonical form."""
    from copy import deepcopy
    if type(evidence) is not dict or set(evidence) != {"format", "book", "nonfinite"} or evidence["format"] != "shadow_book_observation_v1":
        raise ValueError("Invalid book evidence")
    value = deepcopy(evidence["book"])
    if type(evidence["nonfinite"]) is not list:
        raise ValueError("Invalid nonfinite evidence")
    constants = {"nan": float("nan"), "positive_infinity": float("inf"), "negative_infinity": -float("inf")}
    for item in evidence["nonfinite"]:
        if type(item) is not dict or set(item) != {"path", "kind"} or item["kind"] not in constants:
            raise ValueError("Invalid nonfinite marker")
        path = item["path"]
        if type(path) is not list or any(type(k) not in (int, str) for k in path):
            raise ValueError("Invalid observation path")
        if not path:
            if value is not None:
                raise ValueError("Nonfinite marker requires null slot")
            value = constants[item["kind"]]
            continue
        parent = value
        for key in path[:-1]:
            parent = parent[key]
        if parent[path[-1]] is not None:
            raise ValueError("Nonfinite marker requires null slot")
        parent[path[-1]] = constants[item["kind"]]
    return value


def _number(value):
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        return None
    return Decimal(value) if type(value) is int else Decimal(str(value))


def _levels(book, side):
    rows = book.get(side)
    if rows is None:
        return [], None
    if type(rows) is not list:
        raise ValueError("Side must be a list or unavailable")
    parsed = []
    for row in rows:
        # A known price with missing size is unavailable liquidity, not zero.
        if type(row) is not list or len(row) not in (1, 2):
            raise ValueError("Malformed book level")
        price = _number(row[0])
        if price is None or not 0 < price < 1:
            raise ValueError("Invalid bid/complement price")
        parsed.append((price, row[1] if len(row) == 2 else None))
    return parsed, max((p for p, _ in parsed), default=None)


def simulate(intended_order, book_snapshot, *, yes_mid_poll_age_secs=None, book_exchange_timestamp=None):
    """Exactly one caller-supplied snapshot. Its contemporaneity is not inferred.

    Model output has no wall-clock simulation timestamp; the journal event supplies
    local simulation/persistence time. Poll age never changes the disposition.
    """
    if type(intended_order) is not dict or type(intended_order.get("request")) is not dict:
        raise ValueError("Explicit intended order required")
    request = intended_order["request"]
    order_id = intended_order.get("intended_order_id")
    if type(order_id) is not str or not order_id or request.get("side") not in ("yes", "no"):
        raise ValueError("Invalid intended-order identity/side")
    quantity = request.get("count")
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("Positive whole requested quantity required")
    raw_limit = request.get("limit_price")
    limit = _number(raw_limit)
    if raw_limit is not None and (limit is None or not 0 <= limit <= 1):
        raise ValueError("Invalid requested limit")
    if yes_mid_poll_age_secs is not None:
        age = _number(yes_mid_poll_age_secs)
        if age is None or age < 0:
            raise ValueError("Supplied poll age must be finite and nonnegative")
    if book_exchange_timestamp is not None and (type(book_exchange_timestamp) is not str or not book_exchange_timestamp.strip()):
        raise ValueError("Supplied exchange timestamp must be a nonempty string")
    evidence = observation(book_snapshot)
    book = restore_observation(evidence)
    result = dict(schema_version=1, model_id=MODEL_ID, intended_order_id=order_id,
                  intended_order_timestamp_utc=intended_order.get("timestamp_utc"),
                  side=request["side"], requested_quantity=quantity, requested_limit=raw_limit,
                  observation=evidence, yes_bid=None, yes_ask=None, no_bid=None, no_ask=None,
                  side_ask=None, displayed_executable_top_quantity=None, filled_quantity=0,
                  simulated_price=None, yes_spread=None, half_spread=None,
                  simulated_price_minus_requested_limit=None, simulated_price_minus_side_ask=None,
                  fee_model="unavailable", fee_amount=None, yes_mid_poll_age_secs=yes_mid_poll_age_secs,
                  book_exchange_timestamp=book_exchange_timestamp,
                  assumptions="top_only_all_or_none_no_latency_no_passive_no_fee_v1")

    def finish(disposition, reason):
        result.update(disposition=disposition, reason=reason)
        return result

    # Decimal-from-string preserves supplied decimal equality and exact touch.
    with localcontext() as context:
        context.prec = 400
        try:
            if type(book) is not dict:
                raise ValueError("Snapshot must be an object")
            yes, best_yes = _levels(book, "yes")
            no, best_no = _levels(book, "no")
        except ValueError:
            return finish(INVALID_BOOK, "malformed_snapshot_or_bid_price")
        yes_ask = 1 - best_no if best_no is not None else None
        no_ask = 1 - best_yes if best_yes is not None else None
        if any(value is not None and not 0 < float(value) < 1 for value in (yes_ask, no_ask)):
            return finish(INVALID_BOOK, "complement_ask_not_representable_inside_unit_interval")
        for field, value in (("yes_bid", best_yes), ("no_bid", best_no), ("yes_ask", yes_ask), ("no_ask", no_ask)):
            result[field] = float(value) if value is not None else None
        if best_yes is not None and yes_ask is not None:
            spread = yes_ask - best_yes
            result["yes_spread"] = float(spread)
            result["half_spread"] = float(spread / 2)
            if spread < 0:
                return finish(INVALID_BOOK, "crossed_complement_book")
        side_ask = yes_ask if request["side"] == "yes" else no_ask
        result["side_ask"] = float(side_ask) if side_ask is not None else None
        if side_ask is None:
            return finish(NO_LIQUIDITY, "opposite_bid_unavailable")
        opposite, best = (no, best_no) if request["side"] == "yes" else (yes, best_yes)
        sizes = [_number(size) for price, size in opposite if price == best]
        usable = all(size is not None and size > 0 for size in sizes)
        aggregate = sum(sizes, Decimal(0)) if usable else None
        if aggregate is not None and aggregate > 0 and aggregate == aggregate.to_integral_value():
            result["displayed_executable_top_quantity"] = int(aggregate)
        if limit is None or limit < side_ask:
            return finish(NOT_MARKETABLE, "requested_limit_unavailable" if limit is None else "limit_below_side_ask")
        if result["displayed_executable_top_quantity"] is None or aggregate < quantity:
            return finish(NO_LIQUIDITY, "invalid_or_insufficient_best_level_size")
        result.update(filled_quantity=quantity, simulated_price=float(side_ask),
                      simulated_price_minus_requested_limit=float(side_ask - limit),
                      simulated_price_minus_side_ask=0.0)
        return finish(FULL, "marketable_with_sufficient_displayed_top_quantity")
