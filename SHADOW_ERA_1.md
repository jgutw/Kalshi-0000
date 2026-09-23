# Shadow Era 1: Safety, durable state and Execution Model v1

This is offline infrastructure, not a runnable Shadow bot. Do not pass this
client to the existing bot launcher: that launcher still owns shared runtime
writers. Feed wiring and a fully isolated runner require a later review.

`ShadowClient` is independent of KalshiClient. It has no credentials, HTTP
transport, arbitrary endpoint interface, or authenticated write implementation.
Market/book reads use detached JSON snapshots. A future public-data collector
must supply snapshots across this data-only boundary; never inject a live client.
Only the market/book and execution-call interfaces are supplied in Ticket 1;
discovery, polling, balances and portfolio interfaces are deferred.

`place_market_order` writes a durable intended-order event and returns a false/unfilled
result. It never claims a fill. Ticket 2 requires explicit keyword-only `asset`,
`window_id_ts`, `decision_id`, and `strategy`; the production caller does not
currently provide these. This is intentionally not a drop-in production runner.
Cancels, amendments, batches and transfers have no implementation. No DRY_RUN
flag is consulted. `shadow_boundary()` additionally rejects `start_live_safe`
before account, archival or funding work. This context-local guard is defense
in depth, not a Python/OS security sandbox; it does not automatically propagate
to independently created threads. Journal operations enter the boundary in the
calling worker and serialize with a per-instance RLock. A future runner must also
establish the boundary around each worker's full execution path and must never
construct/import the live execution capability.

Explicit ShadowSession construction creates `shadow_data/<shadow_session_id>`
exclusively and writes `session.json`. New-session creation never reuses an existing directory.
Artifact names are allowlisted; resolved paths and existing links/junctions are
checked on initialization and each write. Protected path components are rejected.
There is no archive, bot restart, or fresh-round operation. New-session construction
rejects live/fresh-round flags and the P6 tag. Explicit `ShadowSession.recover`
opens only an existing valid Ticket 2 session. Neither operation launches anything.
Concurrent malicious filesystem replacement is outside this in-process boundary;
the root must be operator-controlled. No real shadow_data directory is created
by this ticket's implementation/tests (tests use temporary directories).

Metadata schema 2 records SHADOW mode, session ID/tag, startup UTC and an explicit
40-character code SHA supplied by the caller. That SHA is a declaration, not proof
of a clean tree. Optional config hashing covers only caller-supplied public JSON
(sorted keys, compact encoding, strict finite JSON). It is not a hash of effective
runtime configuration; null explicitly means unavailable. Never supply secrets.

Ticket 1 schema-1 metadata and plain intent logs are not silently migrated or
treated as authoritative Ticket 2 state. No actual Shadow session has been launched.
At the Ticket 2 checkpoint, fill modeling was unimplemented. Ticket 3 adds only
the deterministic counterfactual model documented below. Fee/latency models,
portfolio accounting and prospective instrumentation remain unimplemented.
No trading math or PAPER/LIVE flags change.

Validation: `py -3 -m unittest kalshi_bot.test_shadow` uses synthetic paths and
inert live-start dependencies. No engines, feeds, protected artifacts or config
files are needed. Independent review is required before any launch work.

Ticket 1 validation: 10 tests run, 9 passed, 1 skipped because the Windows host
denied symlink creation. Mocked link rejection passed. The suite includes the
actual PAPER client method with inert configuration and the actual live-start
helper with inert dependencies. The full engine suite was not run because it
can access runtime configuration and external APIs. P7/P6 code is unchanged.

## Ticket 2: authority, identity and lifecycle

Only `shadow_data/<session_id>/lifecycle.jsonl` is authoritative for lifecycle
state. `session.json` is immutable identity metadata; `writer.lock` is an OS-lock
anchor, not a state file or a stale PID token. There are no mutable position/index
files. `journal.state` returns detached order, position and receipt projections.
Generic session append rejects intended_orders, simulated_fills, positions,
trades and outcomes so they cannot form an alternate lifecycle write path.

Each strict JSON event contains schema 2, SHADOW mode, session/code identity,
contiguous integer sequence, session:sequence event ID, UTC event timestamp,
kind, payload, preceding hash and a SHA-256 of the canonical event without its
own digest. Hashes detect accidental corruption/reordering; they are not signatures.
Session metadata provides the provenance linkage for all reconstructed positions.

Canonical entry key: compact JSON encoding of
`["shadow-entry-v1", shadow_session_id, uppercase_asset, integer_window_id_ts]`.
The nonnegative window number is the supplied repository window-start identity;
it is never inferred from current time or ticker. No asset alias mapping is done.
Idempotency is scoped to the same recovered session, not all future sessions.
Decision IDs are linkage fields; asset/window remains the uniqueness key.

`INTENDED` records an independent UUID order ID, the key, and a normalized request:
decision, strategy, asset, ticker, integer window, explicit yes/no side, positive
integer count, nullable requested price, slippage and optional client ID. Its four
explicit YES/NO quote fields are supplied by the caller, with null meaning unknown.
No complementary quote, timestamp, or book contemporaneity is invented.

After successful flush/fsync the key is consumed. Exact canonical request retries
return the existing order without an event; changing any request field (including
decision, strategy or quotes) under that key is a conflict. Canonical JSON numeric
representation is significant for exact retry comparison. Keys remain consumed
after no-fill and closure. A retry never produces a truthy fill result.

Minimal order lifecycle: INTENDED -> FILLED or NOT_FILLED. The explicitly named
`synthetic_execution` infrastructure API accepts only zero quantity with no price,
or the full requested quantity with an explicit finite price. It marks the source
`synthetic_test_input`. It performs no fill calculation. A fill event contains an
independent position UUID and reconstructs an OPEN position in the same replay
step. Position state retains order/key/decision/strategy/request linkage, execution
ID, filled quantity/price, opened UTC, status and optional close payload.

Positions transition OPEN -> CLOSED only through an explicit close event. An
optional YES/NO outcome must be labeled `synthetic_test_input`; none is calculated
from spot, and no outcome is called official exchange settlement. Execution/close
IDs support exact replay without extra events and reject conflicting reuse.
Unknown orders, partial fills, duplicate terminal transitions and unknown event
types fail. Future execution models can extend the versioned contract without
merging order, execution and position identities.

## Ownership, writes and recovery

One process holds a nonblocking OS lock on byte zero of writer.lock on Windows
(`msvcrt.locking`); Unix uses `flock`. Ownership spans replay through explicit
close/context-manager exit. Competing instances/processes fail before opening the
journal. The OS releases ownership when a process dies; do not delete lock files
to force takeover. In-process threads serialize request checking and persistence.

Validated events are appended as UTF-8 JSON plus newline, flushed and fsynced
before memory publication or acknowledgement. Pre-validation errors do not poison
the writer because no write began. Any exception during append/sync/publication
poisons the writer: neither state access nor additional operations are allowed.
Close and recover; never retry an uncertain write on the same instance.

Recovery validates metadata, acquires exclusive ownership, opens the existing
journal, and replays every physical record through the same lifecycle validator.
It checks envelope/version/provenance, sequence, hash chain, key consistency and
transitions. It then fsyncs the validated file before exposing recovered state.
Thus a complete event from an uncertain acknowledgement becomes a durable retry;
an incomplete event blocks recovery. No legacy paper/P6 source is consulted.

Malformed JSON/UTF-8, duplicate JSON keys, nonfinite values, blank records,
missing newline (even after valid JSON), bad hashes, invalid transitions, missing
journal and unsupported schemas fail closed. Recovery never skips, truncates,
repairs or quarantines bytes. Preserve evidence and require a separately approved
repair procedure. A failed new-session setup can leave an incomplete directory;
it is not automatically reused or deleted.

Crash boundaries:

- Before intention write: no durable consumption; retry after recovery if the
  journal is intact. A partial attempted write blocks recovery.
- After durable intention, before execution: recover INTENDED and its consumed
  key. No position exists and no execution is automatically retried/simulated.
- After an execution result exists only in memory: it was not acknowledged and
  is not durable. Its producer must retry with the same execution ID.
- After a durable execution event but before memory publication: replay restores
  both execution and position together; there is no second position-file write.
- Before/after closure acknowledgement: replay yields the last intact committed
  lifecycle state; exact close replay is idempotent.

Guarantees are bounded to an operator-controlled local filesystem honoring OS
locks and fsync. No database/power-loss guarantee is claimed for directory-entry
persistence, controller caches, network filesystems or filesystem corruption.
No directory fsync is claimed on Windows. A hash chain cannot detect deletion of
an entire valid suffix without an independent durable checkpoint. Malicious
concurrent path replacement/hardlink manipulation is outside this boundary.
Resume retains the session's declared code SHA; it does not attest the currently
executing binary or silently migrate state across code/schema revisions.

## Offline validation and Ticket 3 boundary

Run `py -3 -m unittest kalshi_bot.test_shadow kalshi_bot.test_shadow_journal -v`.
Tests use temporary synthetic sessions, separate synthetic Python processes,
threaded exact retries and injected write/fsync/publication failures. Existing
PAPER behavior and the actual safe-live guard are checked with inert dependencies.
No real engine, feeds, credentials or active P6 data are required.

Ticket 2 validation: 37 tests run, 36 passed, 1 skipped (Windows host denied
symlink creation). This comprises 27 journal tests and the 10 Ticket 1 safety
tests. Mocked link rejection passed. The combined command above includes the
isolated PAPER-client and safe-live entry regression checks. Full engine/P7/P6
suites were not run: no production engine or research surface was modified.
Ancillary non-lifecycle append records retain their Ticket 1 schema-1 envelope;
they are never inputs to journal recovery.

Ticket 2 provides synthetic result persistence only. The accepted Ticket 3 scope
below adds top-of-book execution; probability, partial fills, fees and latency
remain future work rather than assumptions hidden in this model.
Portfolio risk/P&L, official settlement, P7F and a real Shadow runner are deferred.
Shadow remains non-live and has not been launched. Changes await independent review.

## Ticket 3: Execution Model v1

`shadow_execution_v1` is a pure deterministic function of an intended order and
one caller-supplied book snapshot. It does not consult a clock, random generator,
network, journal, later trades, later decisions or settlement. The caller is
responsible for supplying the intended contemporaneous observation; the function
cannot establish contemporaneity from prices or invent a missing exchange timestamp.

The narrow integration API is:

```python
payload = session.simulate_execution(
    intended_order_id,
    {"yes": [[0.46, 10]], "no": [[0.52, 10]]},
    yes_mid_poll_age_secs=None,
    book_exchange_timestamp=None,
)
```

This is an API illustration, not a launch command. It locates an existing durable
INTENDED order, applies the model under the Shadow boundary/writer lock, appends
and fsyncs one SIMULATED_EXECUTION event, then returns its detached payload.
It fetches nothing. `ShadowClient.place_market_order` remains an unfilled
intention operation, including retries after simulated execution.

### Price and quantity rules

The parser's `yes` and `no` lists contain **bids**, as `[probability_price, size]`.
BUY YES uses `1 - best_no_bid`; BUY NO uses `1 - best_yes_bid`. A requested
side limit equal to or above that complementary ask is marketable. No SELL
operation is introduced. Decimal-from-string arithmetic with a local precision
of 400 avoids binary complement rounding at exact touch. JSON output uses finite
numbers; a complement that cannot be represented strictly inside (0,1) is invalid.

Only the best opposite bid price contributes executable size. Equal-price rows
are aggregated at that exact price only. Each contributing size must be a positive
finite number (booleans and numeric strings are invalid). Fractional component
sizes are acceptable only when their aggregate is a positive whole contract count.
Zero/negative/missing/nonfinite sizes at that level cannot be offset by other rows.
Invalid sizes at worse prices are irrelevant; liquidity there is never consumed.

If the limit crosses and the usable top quantity covers the entire request, the
result is SIMULATED_FULL_FILL at the complementary ask, for the entire requested
quantity. A worse requested limit is not the simulated price. There are no partial
fills, deeper-level walking, passive fills, queue estimates or stochastic fills.

For example, a YES limit of 0.52 against a best NO bid of 0.52 uses a simulated
price of 0.48 if top size suffices. A request for 20 with only 12 displayed at
that price produces no fill, even if deeper levels contain more contracts.

### Missingness, invalidity and disposition precedence

The rules run in this deterministic order:

1. Non-object snapshots, malformed side/level structures, or any invalid bid price
   in either supplied side produce NO_FILL_INVALID_BOOK. Prices must be finite
   numbers strictly inside (0,1). A row with a price but no size is allowed as
   a missing-size observation; an empty row or row with more than two members is
   malformed. An incoherent crossed complement book (negative YES spread) is also
   invalid; a locked zero-spread book is permitted.
2. An absent, null or empty opposite side has no executable ask and produces
   NO_FILL_NO_DISPLAYED_LIQUIDITY. No side is borrowed from displayed-market fields.
3. An absent requested limit or one below the ask produces
   NO_FILL_NOT_MARKETABLE. No implicit market-order price/slippage allowance is used.
4. For a marketable order, unusable or insufficient aggregate top quantity produces
   NO_FILL_NO_DISPLAYED_LIQUIDITY. There is no fallback to a worse price.
5. Otherwise the result is SIMULATED_FULL_FILL.

Every no-fill has filled_quantity=0, simulated_price=null and null execution-price
differences. Missing/invalid top quantity is null, never substituted with zero;
insufficient but valid whole quantity remains recorded. The immutable observation
preserves why the quantity was unusable. Non-JSON-like Python objects and invalid
optional metadata are API errors before persistence, not silently dropped input.

The full supplied JSON-like observation is detached and retained, with map keys
sorted and original level order preserved. Only yes/no levels drive the model.
Extra keys such as YES mid or displayed-market bid/ask fields are retained as
evidence but ignored. Malformed bid prices are not silently filtered to expose a
different best level.

### Measurements and timing

Derived YES/NO bids and complementary asks come from that same observation.
YES spread is `yes_ask - yes_bid` only when both exist; otherwise it and half-spread
are null. No missing spread becomes zero. Midpoint is never an execution price.

For full fills, `simulated_price_minus_side_ask` is zero and
`simulated_price_minus_requested_limit` is nonpositive. A negative difference means
price improvement relative to the maximum requested limit, not demonstrated
real-world positive slippage. The existing request's max_slippage is preserved in
the INTENDED event but does not widen the submitted limit in v1.

`fee_model="unavailable"` and `fee_amount=null`. No fee or net P&L is inferred.
`yes_mid_poll_age_secs` is optional nonnegative finite observational metadata,
never a fill criterion or a staleness cutoff. `book_exchange_timestamp` is null
unless explicitly supplied; a supplied nonempty string is retained verbatim,
not parsed into exchange/network/acknowledgement latency or used for selection.

The pure output retains the intended-order timestamp but generates no wall-clock
timestamp. The SIMULATED_EXECUTION envelope's UTC timestamp is the local
simulation/persistence timestamp (and position opened time). It is not an exchange
fill or network latency measurement. Simulation results depend only on supplied
inputs; local envelope timestamps do not participate in model determinism.

### Strict evidence and replay

Book evidence uses `shadow_book_observation_v1`: a detached `book` plus a
`nonfinite` list of path/kind markers. Source NaN/+Infinity/-Infinity values become
null slots with explicit signed-kind markers, preserving their distinction from
genuine null/missing values. Nonfinite prices yield invalid-book; nonfinite best
sizes yield no-liquidity for a marketable request. No nonstandard JSON numeric
tokens are written. All other journal strict-JSON rules remain intact.

SIMULATED_EXECUTION is distinct from Ticket 2 EXECUTION/source=synthetic_test_input.
The latter remains unchanged. Its new payload contains intended-order ID,
deterministic model-result-derived execution/position IDs, and the complete model
result/evidence. Decision, asset/window, strategy and request identity are joined
from the authoritative intended order; session/code provenance remains in the
envelope. Position IDs are separate from order IDs, not copied from them.

Replay restores the evidence, recomputes `shadow_execution_v1` from the original
intention and observation, and compares the complete canonical result. It also
checks deterministic execution/position IDs and the existing event hash chain.
Rehashed but inconsistent results fail recovery. These are integrity checks,
not signatures proving the snapshot was actually observed at an exchange.

A full simulated result reconstructs exactly one OPEN position; no-fill results
reconstruct explicit NOT_FILLED state and no position. The asset/window key stays
consumed in either case. Duplicate simulation always fails, including identical
input after restart. Ticket 2's exact retries for synthetic execution remain
unchanged; synthetic and modeled execution cannot both execute the same intention.
After lost acknowledgement, recover and inspect the existing execution rather
than resimulating. Later trade-through or book changes cannot revise a no-fill.

The journal envelope/session schema remains 2: existing fields and Ticket 2
events are unchanged. The added event kind carries result schema 1, model ID and
observation format. Ticket 2-only readers fail closed on the unknown event kind;
Ticket 3 reads existing Ticket 2 streams without rewriting or migrating them.
Future semantic changes need a new model ID and preserved version-specific replay.

### Interpretation and validation

A SIMULATED_FULL_FILL means that v1 observed sufficient displayed top-of-book
liquidity under its deterministic assumptions. It **does not prove that a real
Kalshi order would have filled**. Liquidity withdrawal, competing orders, queue
priority, hidden liquidity and execution delays are not modeled. There is no
reservation/depletion model across different intentions. No later observation is
consulted to improve an earlier result.

Future tickets may add latency, passive/queue behavior, partial fills, deeper
execution, or an independently validated fee model. Ticket 3 implements none of
those, no portfolio accounting/settlement, and no actual Shadow runner.

Validation command:

```text
py -3 -m unittest kalshi_bot.test_shadow kalshi_bot.test_shadow_journal kalshi_bot.test_shadow_execution -v
```

Initial combined validation: 85 tests run, 84 passed, 0 failures/errors, 1 skipped
(the existing Windows symlink-creation restriction). The 48 new execution tests
cover deterministic pricing, missingness, quantity boundaries, immutable evidence,
replay/tampering, no future trade-through, no live transport, and loss of execution
acknowledgement. Ticket 1/2 test files remain unchanged. All sessions/data were
synthetic temporary fixtures. No actual Shadow session or feed was launched.
