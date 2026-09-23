# Shadow Era 1: Ticket 1 boundary

This is offline infrastructure, not a runnable Shadow bot. Do not pass this
client to the existing bot launcher: that launcher still owns shared runtime
writers. Feed wiring and a fully isolated runner require a later review.

`ShadowClient` is independent of KalshiClient. It has no credentials, HTTP
transport, arbitrary endpoint interface, or authenticated write implementation.
Market/book reads use detached JSON snapshots. A future public-data collector
must supply snapshots across this data-only boundary; never inject a live client.
Only the market/book and execution-call interfaces are supplied in Ticket 1;
discovery, polling, balances and portfolio interfaces are deferred.

`place_market_order` writes an intended-order event and returns a false/unfilled
result compatible with the execution caller's fill check. It never claims a fill.
Cancels, amendments, batches and transfers have no implementation. No DRY_RUN
flag is consulted. `shadow_boundary()` additionally rejects `start_live_safe`
before account, archival or funding work. This context-local guard is defense
in depth, not a Python/OS security sandbox; it does not automatically propagate
to independently created threads. A future runner must establish the boundary
in each worker and must never construct/import the live execution capability.

Explicit ShadowSession construction creates `shadow_data/<shadow_session_id>`
exclusively and writes `session.json`. Existing sessions are never reused.
Artifact names are allowlisted; resolved paths and existing links/junctions are
checked on initialization and each write. Protected path components are rejected.
There is no archive, restart, fresh-round, or recovery operation. The constructor
rejects live/fresh-round flags and the P6 tag. It does not launch anything.
Concurrent malicious filesystem replacement is outside this in-process boundary;
the root must be operator-controlled. No real shadow_data directory is created
by this ticket's implementation/tests (tests use temporary directories).

Metadata schema 1 records SHADOW mode, session ID/tag, startup UTC and an explicit
40-character code SHA supplied by the caller. That SHA is a declaration, not proof
of a clean tree. Optional config hashing covers only caller-supplied public JSON
(sorted keys, compact encoding, strict finite JSON). It is not a hash of effective
runtime configuration; null explicitly means unavailable. Never supply secrets.

The intent journal is a minimal append-only JSONL scaffold, not a crash-safe,
idempotent ledger. Ticket 2 must implement durable identities, recovery and
duplicate protection. Fills, fee/slippage/latency models, portfolios and prospective
instrumentation are not implemented. No trading math or PAPER/LIVE flags change.

Validation: `py -3 -m unittest kalshi_bot.test_shadow` uses synthetic paths and
inert live-start dependencies. No engines, feeds, protected artifacts or config
files are needed. Independent review is required before any launch work.

Ticket 1 validation: 10 tests run, 9 passed, 1 skipped because the Windows host
denied symlink creation. Mocked link rejection passed. The suite includes the
actual PAPER client method with inert configuration and the actual live-start
helper with inert dependencies. The full engine suite was not run because it
can access runtime configuration and external APIs. P7/P6 code is unchanged.
