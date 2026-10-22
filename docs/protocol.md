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
Keyless rows form a multiset: equal events retain separate occurrences.
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
thirty days ending October 19 start September 20. Datetime windows include both ends of the elapsed UTC duration.
Timestamp resolution is one microsecond; DATE planning uses UTC calendar dates. Without a declared
window schema type, rolling planning uses datetime bounds; a DATE window requires its schema type.
`lookback` is nonnegative and allowed only on timestamp shapes in keyed contracts; it extends a planned read back
from the weakest covered completeness boundary. Keyless appends cannot overlap.

## Choosing a shape

| Source behaviour | Shape | What it cannot express |
|---|---|---|
| Exhaustive table export | `Shape.full()` | Completeness inferred from a received count alone. |
| Exhaustive rolling window | `Shape.window(size=...)` | Mutable window membership or more than one window column. |
| One complete partition per file | Complete singleton window | A finite registered domain; space outside delivered partitions remains unknown. |
| Every changed row since a timestamp | `Shape.changes(checkpoint="timestamp", deletions="none")` | Deletions or repair of a missed full export without another full export. |
| Every changed row and deleted key since a timestamp | `Shape.changes(checkpoint="timestamp", deletions="tombstone")` | A missing base; deletion gaps heal only when the read reaches back through them. |
| Continuous opaque-cursor feed with deleted keys | `Shape.feed()` | Window-local cursors, missing generations or repair of an unknown base. |
| Immutable events retained from a declared start | `Shape.append()` | Updates, deletion or overlapping keyless intervals. |

Shape constructors take keywords; `Contract` and `Manifest` require keywords after `shapes` and
`produced_at`, respectively. `Contract.manifest(shape, produced_at, ...)` supplies table and schema
hash. `Shape.window_ending_at` centralizes inclusive DATE and elapsed datetime window arithmetic.

## Manifest

`Manifest(table, shape, produced_at, *, window, since, generation, rows, schema_hash, keys, tombstones,
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
Without a digest, manifest identity does not identify the content.

## Corrections

A corrected file is a new delivery by content hash and needs a later truthful read boundary.
Invalidating an accepted delivery requires rebuilding rows and ledger from the receipts before it,
then redelivering the corrected file and subsequent reads; ledger reconstruction alone cannot
recover rows. Supersession at the same boundary remains an open decision.

## Ledger and transitions

`Ledger` contains only its contract, disjoint regions covering the domain, table generation and
`publication_id`. Initially one unbounded region is unknown. `Region` has `low`, `high`, open-endpoint
flags, `complete_as_of` and `valid_as_of`. `None` endpoints are unbounded. Evidence dates are absent
together, or `valid_as_of <= complete_as_of`. No accepted-identity history is embedded in the ledger.

For a region with dates `(c, v)`, every source row live there at `c` is present. Every retained row
was live at some source instant in `[v, c]`; deletions after `v` may remain. Equal dates mean the
region equals the source at that instant. Unknown is different from a known empty region.

Internally a step function stores sorted cuts with Unknown or Known evidence to their right.
`Region` remains the wire and claims view, including endpoint flags. Adjacent equal evidence has
one run; untouched space remains unknown. `Ledger.apply` performs accounting only, without row
certification or replay lookup. Store delivery owns replay idempotency.

## Transitions

| Delivery and previous evidence | Covered dates after delivery at t | Table generation |
|---|---|---|
| Complete whole domain, any evidence | `(t, t)` | Issued generation, or cleared. |
| Complete window, any evidence | `(t, t)` | Unchanged. |
| First keyless append, unknown | `(t, t)` | Issued generation if present, otherwise unchanged. |
| Later keyless append, known | `(t, t)` | Issued generation if present, otherwise unchanged. |
| Keyed timestamp changes without deletions, `(c, v)` | `(t, v)` | Issued generation if present, otherwise unchanged. |
| Keyed timestamp tombstones, `(c, v)`, `since <= v` | `(t, t)` | Issued generation if present, otherwise unchanged. |
| Keyed timestamp tombstones, `(c, v)`, `since > v` | `(t, v)` | Issued generation if present, otherwise unchanged. |
| Generation changes without deletions, `(c, v)` | `(t, v)` | Issued generation. |
| Generation tombstones, `(c, v)`, `c = v` | `(t, t)` | Issued generation. |
| Generation tombstones, `(c, v)`, `c > v` | `(t, v)` | Issued generation. |

- Manifest fields must match its admitted shape and contract, including bounds and key types.
- Every affected known region requires `t > complete_as_of`; a late full cannot overwrite a newer window.
- Changed-only coverage must be known throughout, except the first keyless append on an untouched ledger.
- The first keyless append requires `since = start_at`; later appends require `since = complete_as_of`
  in every affected region, so a newer complete window requires a full reset before global append.
- Timestamp changes require `since <= t` and `since <= complete_as_of` everywhere covered.
- Generation changes require `since` equal to the held table generation; a broken chain needs a full reset.
- The gate additionally requires `t <= receiver now`; accounting alone has no receiver clock.
- Each accepted delivery extends the publication chain with the previous head, contract and manifest
  identities; disjoint windows commute only on evidence, never on this ordered chain.

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
Business row rules from quality tools run outside this fixed check set; the caller retains their outcomes.

`Gate.evaluate(ledger, manifest, outcomes, now=now, source=None)` requires receiver `now`. Core checks
validate structure, source time, base coverage, timestamp/generation continuity and monotonic time.
`Gate.preflight(ledger, manifest, now=now)` returns those core checks without row outcomes;
`Gate.reject(ledger, manifest, checks, now=now, source=None)` records a declared failure.
The `Receipt` contains manifest, checks, `outcome` (`accepted`, `rejected` or `replayed`), `received_at`, optional `source`,
`base_publication_id`, transition, resulting ledger, claims at receipt time, and publication identity.
Acceptance and replay have a transition; rejection retains the previous ledger and publication.
`accepted` and `replayed` are derived compatibility properties. Receipts
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

1. `load_ledger(contract)` obtains the current evidence. `accepted_receipt(manifest.identity)`
   returns a stored acceptance on replay, with `outcome="replayed"`, original identity and receiver time.
2. Preflight the declaration and transition. `publish(None, receipt)` records a rejection against
   its expected parent without changing published rows or evidence.
3. `hold(source, manifest, contract)` stages a candidate; `check(candidate, contract, manifest,
   requirements)` answers the fixed row checks against one base publication.
4. `publish(candidate, receipt)` compares that base and atomically executes an accepted plan with
   its rows and evidence, or records rejection; `abort(candidate)` releases staged work after failure.

`receipts(table=None)` returns persistence order. These seven methods are the required `Store` port.
An accepted publication requires a held and checked candidate. Rejected publication also compares
and sets the expected parent, and refuses a receipt that cannot decode before writing the log.
`publication_stamp(table)` and `repair(contract)` form the optional stamped-ledger tier; ordinary
read methods and `store.deliver` are conveniences, not required port methods.

The store must satisfy six obligations:

1. Check candidate rows before published rows change; execute only an accepted receipt's plan.
2. Publish rows, ledger, accepted receipt and any row stamp as one atomic step: either one transaction,
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
`Ledger.reconcile(contract, receipts, current, stamp, repair=False)` diagnoses disagreement;
with `repair=True` it returns reconstructed evidence only for a matching stamp. `LedgerAhead`
means accepted evidence exceeds the row stamp and needs a fresh full replacement; `StampAhead`
means rows lack matching current evidence and need receipt recovery or external restoration.
Both subclass `InconsistentPublication`, so stores and conformance tests match types, not messages.
For consistent reads a store that verifies receipt retention can supply `retained_head`; diagnosis
then checks that receipt's ledger and hash against the stamp without replaying its prefix. Repairs
and all mismatches still fold the full retained history.

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
Reconstruction uses each accepted receipt's historical contract, then the requested compatible contract.

## Versioned JSON

Structured wire values have `version: 1` and `kind` equal to the public class name. Field names match
constructor arguments; every field, including defaults, is present. Tuples encode as arrays, enums
as strings, mappings as objects. Missing or unknown fields, unsupported versions/kinds, invalid
wire types, overflowing tags and naive datetimes raise `SerializationError` before normalization.

Dates use `{"$date":"2026-10-18"}`, UTC datetimes
`{"$datetime":"2026-10-18T00:00:00+00:00"}`, durations `{"$seconds":604800}`. Canonical wire JSON
uses sorted keys, compact separators and ASCII escapes. Identity is SHA-256 of [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)
(JSON Canonicalization Scheme) applied to the identity dictionary: recursively omit `version` and
fields equal to defaults; sort keys/tombstones lexically by each tuple's canonical JSON in UTF-16 code-unit order; normalize datetimes
to UTC `+00:00`. A replay's `outcome` is normalized to `accepted` for identity, retaining its
original receipt identity. `identity_dict()` and `identity_json()` expose this form.
JCS emits UTF-8, sorts object keys by UTF-16 code units and formats finite binary64 numbers using
the ECMAScript shortest round-trip rule, including `-0` as `0`. Integer inputs must be exactly
representable as binary64; unsupported values are refused rather than rounded. For portable integer
keys across languages, stay within `[-(2**53-1), 2**53-1]`; larger identifiers can use strings.
Full wire JSON retains exact integer values, while JCS may spell a large exact binary64 value with
a different shortest decimal token. Do not hash sidecar bytes.
UTC datetimes omit fractional seconds when zero, otherwise use six digits. Durations encode integral
seconds exactly, or fractional seconds only when float conversion preserves microseconds; otherwise
construction fails. Typed strings reject lone Unicode surrogates.
Compatible future optional fields at defaults preserve identity; semantic changes need explicit
migration. Unknown wire versions are refused even when their identity form would be compatible.

`to_dict/from_dict` and `to_json/from_json` round-trip nested values. A ledger embeds contract,
regions, generation and publication head; a receipt embeds its decision and resulting ledger.
[Contract](examples/contract.json), [manifest](examples/manifest.json), [ledger](examples/ledger.json)
and [receipt](examples/receipt.json) are executable wire examples.
[Identity vectors](examples/identity-vectors.json) specify exact canonical forms and hashes for independent implementations.
[JSON Schema 2020-12 documents](wire/) cover all sixteen wire kinds; semantic rules remain in this protocol.

## Boundaries

The core uses only Python's standard library; reference stores and conformance support are separate.
The [reference guide](reference.md) describes concrete transports and the six-call accounting path.
The conformance suite exercises publication obligations against store implementations; passing it
is evidence for those scenarios, not a proof of arbitrary storage or source correctness.

Open owner decisions are public vocabulary, a table namespace, finite string or multidimensional
domains, a CLI, same-boundary supersession, and release and compatibility policy; none is added here.
