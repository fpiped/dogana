# The dogana protocol

Version 1. This page defines the wire values, transitions and publication obligations.

## Trust model

A delivery contains rows and a `Manifest`, the sender's declaration of what was read. A `Contract`
states the source guarantees; a `Gate` checks the declaration and the delivered rows. It cannot
verify the source against them. A truncated full export with a matching count, an overstated window
or an omitted late commit can pass. A receiver-derived row count checks parsing, not completeness.
The shrink guard limits one delivery's row loss, not cumulative loss or source dishonesty.

The invariant is conditional on truthful extraction, the declared source guarantees and correct
store checks: a delivery never raises an evidence date above what those premises prove. The core
checks region partitioning, date order, continuity and unchanged evidence outside coverage. It does
not authenticate senders, verify business values or certify a query's answer.

`produced_at` is the source snapshot or read boundary. The extractor guarantees all changes through
that boundary, including its endpoint. Download completion, arrival time and a scheduler's logical
date are not that boundary. Required receiver time `now` rejects `produced_at > now`. A late commit
with an earlier `updated_at` is invisible to the gate. Overlapping keyed reads mitigate bounded lag;
no lookback proves that extraction was complete.

## Contract and shapes

`Contract` declares `table`, `key`, named `shapes`, optional `window_column`, optional `cursor_column`,
`schema`, `max_shrink_fraction`, `freshness` and `start_at`. A schema is an ordered tuple of
`Column(name, type)` with logical types `int`, `float`, `str`, `bool`, `date`, `datetime`. Column order
matters: `schema_hash` is SHA-256 of compact ASCII JSON containing ordered `[name, type]` pairs. Publication requires a schema; accounting alone does not.
`freshness` defaults to seven days; choose cadence plus arrival slack. `max_shrink_fraction` defaults
to 0.5, lies between zero and one, and is disabled only by explicit `None`.

A window column exists exactly when a window shape exists. Regions divide this one column's ordered
domain, not the key space. Its value must be immutable for a key; a full delivery can reset it.
Keys are unique and non-null. Key and window values are strings, integers, dates or UTC datetimes;
schema columns and manifest bounds must agree on type. Normalize aware datetimes to UTC before key
equality or interval arithmetic, including DST folds. Strings use code-point order, without Unicode
normalization or locale collation. Equal-endpoint windows denote one partition. String adjacency uses successor `s + "\x00"` and minimum `""`. Date and datetime domains respect
their representable minima and maxima; empty exterior intervals are discarded. A set of delivered
plant codes does not close the rest of the string domain; whole-table evidence requires a full read.

An empty `key` means rows have no identity. Complete shapes replace their coverage wholesale.
Keyless changed-only shapes must be whole-domain timestamp appends with no deletion signal and a
`start_at` bounding retained history. This declares that the source only appends and never deletes
or revises retained events. Named-key coverage, tombstones and generation checkpoints require keys.
An optional `cursor_column` names a schema datetime column for timestamp row checks; without it,
the gate cannot check individual rows against the declared extraction interval.

A `Shape` has four semantic properties:

| Property | Values | Meaning |
|---|---|---|
| coverage | `all`, `window`, `keys` | Whole domain, closed window, or a table-wide stream naming changed keys. |
| completeness | `complete`, `changed_only` | All current rows in coverage, or all changes since a checkpoint. |
| deletions | `absence`, `tombstone`, `none` | Missing rows delete, named keys delete, or no deletion signal. |
| checkpoint | `generation`, `timestamp`, `none` | Opaque cursor, source time, or no checkpoint. |

Complete shapes require `absence` and checkpoint `none`. Changed-only shapes require `timestamp`
or `generation` and deletions `none` or `tombstone`. Key coverage is changed-only. Generations are
table-wide, so window generation feeds are forbidden. These rules admit twelve semantic shapes.

Shapes also carry nonnegative relative `cost` (default one), optional positive `window_size` and
optional `lookback` durations. `window_size` is a planning hint, not a push-window width constraint.
Rolling durations require date or datetime windows. DATE widths are whole days including both ends:
thirty days ending October 19 start September 20. Datetime windows include both ends of the duration.
`lookback` is allowed only on timestamp shapes in keyed contracts; it extends a planned read back
from the weakest covered completeness boundary. Keyless appends cannot overlap.

## Manifest

`Manifest(table, shape, produced_at, window, since, generation, rows, schema_hash, keys, tombstones,
content_hash)` describes one read. `window` is required exactly for window coverage; `since` exactly
for changed-only shapes. Timestamp checkpoints are aware datetimes; generation checkpoints and
issued generations are nonempty strings. A generation feed must issue its next generation.
A complete whole-domain delivery may issue a generation; a complete window carries none.

Changed-only delivery collapses changes to one final live row or terminal tombstone per key at its
boundary. `rows` counts live rows, excluding tombstones. `keys` and `tombstones` are tuples of
composite key tuples of the contract's width and types. Key coverage enumerates every live key;
other keyed shapes may omit the enumeration. Live and tombstoned keys cannot intersect. Tombstones
require a tombstone shape. A zero-length timestamp interval contains no rows.

Timestamp deliveries with `cursor_column` use `(since, produced_at]`, including for keyed feeds.
Without that column, keyed extraction may include `since`; overlapping keyed reads are admissible.
Keyless appends always use `(since, produced_at]`, without overlap. `content_hash` is an optional
digest of the bytes or canonical rows, supplied by sender or receiver. It participates in identity.
A corrected payload needs a changed manifest; a changed digest creates a new delivery subject to
ordinary time checks, so a correction at an already accepted boundary is refused, not replayed.
Without a digest, manifest identity does not identify the content.

## Ledger and transitions

`Ledger` contains only its contract, disjoint regions covering the domain, table generation and
`publication_id`. Initially one unbounded region is unknown. `Region` has `low`, `high`, open-endpoint
flags, `complete_as_of` and `valid_as_of`. `None` endpoints are unbounded. Evidence dates are absent
together, or `valid_as_of <= complete_as_of`. No accepted-identity history is embedded in the ledger.

For a region with dates `(c, v)`, every source row live there at `c` is present. Every retained row
was live at some source instant in `[v, c]`; deletions after `v` may remain. Equal dates mean the
region equals the source at that instant. Unknown is different from a known empty region.

Coverage boundaries split regions exactly, preserving excluded endpoints; adjacent regions with
equal evidence merge. Empty gaps between successive integers, dates or microsecond datetimes are
removed. Untouched regions retain their evidence, including unknown gaps.

- Complete delivery at `t` sets covered dates to `(t, t)`. Empty complete coverage proves absence,
  subject to shrink policy. Whole-domain completion sets or clears the table generation.
- Changed-only delivery needs a known base throughout coverage. Key streams cover all regions,
  asserting that no unreported key changed; a partial base cannot bootstrap them.
- The first keyless append is the exception: `since = start_at` establishes retained history only.
  Later appends require `since` equal to every covered `complete_as_of`, not their minimum. A complete
  keyless delivery restarts the boundary in its coverage. After a newer complete window, a full
  complete delivery must realign boundaries before global append can resume.
- A keyed timestamp feed requires `since <= produced_at` and `since <= complete_as_of` everywhere
  covered. A gap is refused. A generation feed requires `since` equal to the table generation and
  advances that single cursor. A broken chain needs a complete whole-domain reset.
- Changed-only delivery raises completeness to `t`. Timestamp tombstones also raise validity where
  `since <= valid_as_of`; otherwise missed deletions remain unproved. Generation tombstones raise
  validity where the previous validity equalled completeness. Keyed feeds without deletion signals
  leave validity unchanged. Keyless appends raise both dates because their contract declares that
  retained events never change or disappear, despite the shape's `deletions="none"`.
- A new delivery with `t <= complete_as_of` in any affected known region is refused as a whole.
  A late full export cannot overwrite a newer window. Disjoint older windows remain admissible.

`Ledger.apply(manifest)` performs accounting only and raises `RejectedDelivery` for an impossible
transition. It neither certifies rows nor detects replays. Repeating a manifest through this pure
operation is subject to the same strict time rule; store delivery provides idempotent replay.

## Plans, checks and receipts

`ApplyPlan` contains ordered `Action` values. `replace_all` replaces the table. `replace_window`
removes rows in its closed window, removes earlier copies of candidate keys and inserts the candidate.
`upsert` matches the contract key; `tombstone` deletes exactly its keys; `append` inserts without
matching. Keyless replacement does not match keys. The core describes operations but never reads rows.

`Gate.requirements(contract, manifest)` names required row checks. The store returns exactly one
`Check(name, passed, detail)` for each; missing, duplicate or unexpected outcomes reject publication.

| Check | Predicate |
|---|---|
| row_count | Candidate live row count equals `manifest.rows`. |
| schema_hash | Candidate ordered schema and manifest hash equal the contract schema hash; omitted for schemaless accounting. |
| rows_in_window | Every candidate window value is non-null and inside the declared closed window. |
| window_stability | Every candidate window-column value is non-null. |
| key_integrity | Every candidate key is non-null and unique. |
| named_keys | Required live-key enumeration equals candidate keys; window tombstones cannot delete published keys outside coverage. |
| shrink_tolerance | Complete delivery removes no more than the allowed fraction of previously published rows in coverage. |
| rows_in_interval | With `cursor_column` and a timestamp shape, every cursor value is non-null and in `(since, produced_at]`. |

Moving a key into a window is safe: replacement removes its old copy. Moving out can leave a hole
that no candidate check sees, hence source immutability remains necessary. Checks blocked by an
earlier failure return `passed=False, detail="not evaluated"`, not an unevaluated predicate's result.

`Gate.evaluate(ledger, manifest, outcomes, now=now, source=None)` requires receiver `now`. Core checks
validate structure, source time, base coverage, timestamp/generation continuity and monotonic time.
`Gate.preflight(ledger, manifest, now=now)` returns those core checks without row outcomes;
`Gate.reject(ledger, manifest, checks, now=now, source=None)` records a declared failure.
The `Receipt` contains manifest, checks, acceptance, `received_at`, `replayed`, optional `source`,
`base_publication_id`, transition, resulting ledger, claims at receipt time, and publication identity.
Only acceptance has a transition; rejection retains the previous ledger and publication. Receipts
are indexed by the receiver contract's table even if the rejected manifest names another table.
Their identity also covers check outcomes. Ledger evidence in each receipt grows with region count,
not delivery count; manifest key lists and other payload metadata still contribute to receipt size.

`DoganaError` is the error root; `InvalidContract`, `RejectedDelivery`, `PlanningError`,
`SerializationError` and `StoreError` distinguish configuration, delivery, planning, wire and storage
failures. `SerializationError` subclasses `InvalidContract`; `InconsistentPublication` subclasses
`StoreError`. Only declared protocol violations become rejection receipts. Operational failures and
programming errors propagate after cleanup. Failure after commit cannot revoke acceptance.

## Claims and planning

`Ledger.claims(now, limit=None, window=None)` requires the reader's current time. It clips regions
to an optional window and returns `Claims` with each region's dates and state. `now` evaluates
freshness, not historical time travel. Equality at the freshness limit is fresh.

| State | Condition |
|---|---|
| unknown | Both dates absent. |
| complete | Dates equal and completeness within the freshness limit. |
| patched | Completeness newer than validity and within the freshness limit. |
| expired | Completeness older than the freshness limit, even if validity is also older. |

State priority is unknown, expired, patched, complete. Aggregate dates are separate minima, absent
if any region is unknown; equal aggregate dates do not imply that every region is exact. Validity
remains visible even when recent patches hide old deletion knowledge.

`Claims.combine` takes those minima across tables. These are lower bounds on input evidence dates,
not completeness or validity statements about one common snapshot or a joined result. Regions at
different dates may form a table exact at no instant. Common-time evidence is needed to propagate
row guarantees through monotone queries such as filters, joins and unions. Negation (`NOT EXISTS`,
anti-joins, difference) swaps completeness and validity requirements for the negated input. With
exact items and complete-but-patched movements at the same boundary, "items with no movement" is
valid but possibly incomplete: a retained deleted movement hides an item. Exact aggregates need
both guarantees; `COUNT` and `SUM` as exact values are not ordinary monotone row queries.

`Requirement` names a table, minimum completeness, optional minimum validity and optional window.
`plan_delivery` returns no request if claims suffice; otherwise it chooses the cheapest admitted
shape that establishes the requirement, breaking ties by shape name. It checks candidate transitions
against the ledger. Unknown bases or unrepairable deletion gaps exclude feeds; complete windows can
prove bounded requirements, but whole-domain requirements need whole-domain evidence. No suitable
shape raises `PlanningError`. The boundary may exceed the requirement minimum to be later than all
affected evidence; the extractor must actually obtain that source boundary.

`next_extraction(ledger, shape, until)` derives a rolling window from `window_size`, extending its
start back when needed to bridge the last complete evidence and avoid scheduling-jitter gaps.
Timestamp `since` is the weakest covered completeness boundary minus optional keyed `lookback`.
Append requires one common boundary; generation requests use the held cursor. The request's `until`
becomes the manifest boundary only when extraction proves it. Push and pull use the same gate.

## Store protocol and publication

`Store` and `Extractor` are structural `typing.Protocol` interfaces. An extractor implements
`extract(contract, request)`, returning `(manifest, source)`. The source representation belongs to
the store. `dogana.deliver(store, contract, source, manifest, now)` owns the shared sequence:

1. Load the ledger and compare `publication_stamp(table)` with its head. Consult
   `accepted_receipt(manifest.identity)` before preflight; a replay returns the stored receipt with
   `replayed=True`, preserving original receipt identity and `received_at`, without changing rows.
2. Preflight the declaration and transition. A rejected declaration is persisted by `save_receipt`.
3. `hold(source, manifest, contract)` stages a candidate; `check(candidate, contract, manifest,
   requirements)` answers the gate. Evaluate with required receiver time and source description.
4. If accepted, `execute(candidate, plan)` prepares row changes and `publish(candidate, receipt)`
   commits rows and evidence. Execution may be deferred into `publish` for a table-format commit.
   On rejection, save the receipt. `abort(candidate)` releases staging and rolls back open work.

The primitives also include `load_ledger(contract)`, `receipts(table=None)` in persistence order
and `repair(contract)`. Optional `read(table, publication_id=None)` and `store.deliver` conveniences
are not required by `Store`; delivery conveniences delegate to the core.

The store must satisfy six obligations:

1. Check candidate rows before published rows change; execute only an accepted receipt's plan.
2. Publish rows, stamp, ledger and accepted receipt as one atomic step: either one transaction,
   or a table-format commit carrying the full receipt or a reference to an already durable receipt.
   Ordered writes plus idempotent plans are not atomic. Accounting alone offers no publication guarantee.
3. Compare and set against `receipt.base_publication_id`. Refuse a changed head, including cached
   checks against a stale base. A commit retry may not silently rebase an already approved plan.
4. Persist accepted and rejected receipts; retain accepted receipts in publication order for recovery.
5. Replay accepted manifest identity by returning its original receipt, with no second publication.
6. Read rows and claims under one publication identity; separate current reads can straddle a commit.

The publication chain is SHA-256 of UTF-8 `f"{previous}:{contract.identity}:{manifest.identity}"`;
the initial `previous` is the literal string `None`. It includes the full contract identity.
The stamp associates rows with that head; it is not a row checksum. Comparing it with the ledger
finds a ledger behind rows or external publication that changed the stamp. It cannot detect silent
row edits preserving the stamp, lost rejected receipts or joint loss of all evidence. Surviving
receipts or stamps disprove a fresh start when a ledger is missing. Stores must fail closed.

`Ledger.from_receipts(contract, receipts)` folds accepted receipts in publication order and checks
their ledgers and chain, ignoring rejections. Missing, reordered or foreign acceptances refuse
reconstruction. `repair` restores the ledger only when the reconstructed head equals the stamp;
it never relabels rows from ledger evidence. Rows require retained store versions to reconstruct.

## Algebraic guarantees

The same initial ledger and ordered accepted history produce the same ledger. Store delivery is
replay-idempotent by accepted-receipt lookup; `Ledger.apply` itself is not. Disjoint complete or
timestamp windows commute on regional evidence when both orders are admissible, but not on the
publication chain. The fold is not a join-semilattice or a merge of concurrent ledgers: order,
continuity and row replacement constrain admissibility. Rejection and shrink policy apply per
manifest, not to an arbitrary batch or cumulative history.

## Contract evolution

History binds `table`, `key`, `window_column` and the types of key/window columns. Stores refuse
changes to these bindings. Other fields may evolve, including freshness, shrink policy, costs,
window sizes, lookback, admitted shapes and nonbinding schema columns. Each accepted receipt retains
the contract used for that decision. The full contract, including policy with normalized numeric
spelling, participates in the chain; only binding differences cause history refusal.
`Ledger.with_contract(contract)` verifies compatibility and preserves regions, generation and head.
Reconstruction uses each accepted receipt's historical contract before applying the requested compatible contract.

## Versioned JSON

Structured wire values have `version: 1` and `kind` equal to the public class name. Field names match
constructor arguments; every field, including defaults, is present. Tuples encode as arrays, enums
as strings, mappings as objects. Missing or unknown fields, unsupported versions/kinds, invalid
wire types, overflowing tags and naive datetimes raise `SerializationError` before normalization.

Dates use `{"$date":"2026-10-18"}`, UTC datetimes
`{"$datetime":"2026-10-18T00:00:00+00:00"}`, durations `{"$seconds":604800.0}`. Canonical wire JSON
uses sorted keys, compact separators and ASCII escapes. Identity is SHA-256 of the UTF-8 identity
form: recursively omit `version` and fields equal to defaults; sort keys/tombstones lexically by
each tuple's compact canonical JSON (`[10]` before `[2]`); normalize integral floats to integers
and datetimes to UTC `+00:00`. Always exclude `Receipt.replayed`, even when true, so replay preserves
the original receipt identity. `identity_dict()` and `identity_json()` expose this form. Do not hash sidecar bytes.
Compatible future optional fields at defaults preserve identity; semantic changes need explicit
migration. Unknown wire versions are refused even when their identity form would be compatible.

`to_dict/from_dict` and `to_json/from_json` round-trip nested values. A ledger embeds contract,
regions, generation and publication head; a receipt embeds its decision and resulting ledger.
[Contract](examples/contract.json), [manifest](examples/manifest.json), [ledger](examples/ledger.json)
and [receipt](examples/receipt.json) are executable wire examples. Identity fixtures specify exact
bytes and hashes for independent implementations.

## Boundaries

The core uses only Python's standard library; reference stores and conformance support are separate.
The [reference guide](reference.md) describes concrete transports and the six-call accounting path.
The conformance suite exercises publication obligations against store implementations; passing it
is evidence for those scenarios, not a proof of arbitrary storage or source correctness.
