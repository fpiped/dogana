# The dogana protocol

Version 1. The wire format and the rules below are authoritative for the reference library.

## What a delivery proves

A full extract proves that a missing row is deleted. Rows changed since Monday prove nothing about
unreported deletions. A complete thirty-day window proves absence only inside that window.

Razniewski and Nutt describe table completeness statements: the available table contains every ideal
row satisfying a condition. Motro separates completeness (no missing rows) from validity (no rows that
should be gone). A retained deleted row violates validity. Each region therefore has two dates.

One invariant governs publication: a delivery never raises a date above what it proves.

## Delivery contract and shape

A `Contract` declares `table`, `key`, named `shapes`, optional `window_column`, `schema`,
`max_shrink_fraction`, `freshness` and `start_at`. A schema is an ordered tuple of `Column(name, type)`;
logical types are `int`, `float`, `str`, `bool`, `date`, `datetime`. Its canonical JSON SHA-256 is the
schema hash. Column order is significant. Publishing rows requires a schema; accounting alone does not.
`max_shrink_fraction`, between zero and one, limits the fraction of published rows a complete delivery
may remove within its coverage. It is a guard against an accidentally truncated export, not a proof of
source correctness. `freshness` defaults to seven days.

A window column exists exactly when a window shape exists. Its value must be immutable for a key.
Moving keys between partitions or changing their window value requires a full delivery. This is a
source guarantee, not something the gate can infer from one delivery. Keys are non-null and unique.
An empty key means the source gives rows no identity. Such a contract admits complete shapes, which
replace their coverage wholesale, and append-only shapes: whole-domain, timestamp checkpoint, no
deletion signal. Named keys, tombstones and generation checkpoints need a key. A contract with an
append-only shape declares `start_at`, the beginning of its retained history. Window bounds and key
components are strings, integers, dates or UTC datetimes; window and key schema columns use these
logical types, and a manifest's window bounds must have the window column's declared type. Timezone-aware
inputs normalise to UTC before interval arithmetic and key equality, including repeated local times at
DST transitions, so two instants that share a local wall time are never one point.

A `Shape` has four semantic properties, plus a nonnegative relative extraction `cost` (default one)
and an optional positive `window_size` duration for automatic rolling-window planning. DATE windows
count calendar dates, including the final date: thirty days ending October 19 start September 20.
Their width must be a whole number of days. A declared integer or string window column cannot
use a duration: rolling duration windows require date or datetime values. Datetime windows span the duration with both endpoints
included:

| Property | Values | Meaning |
|---|---|---|
| coverage | `all`, `window`, `keys` | Whole domain, closed interval on the window column, or keys in a table-wide change stream. |
| completeness | `complete`, `changed_only` | All current rows in coverage, or all changes since the checkpoint. |
| deletions | `absence`, `tombstone`, `none` | Missing rows delete, explicitly named keys delete, or no delete signal. |
| checkpoint | `generation`, `timestamp`, `none` | Opaque source cursor, inclusive source timestamp, or no checkpoint. |

Complete means checkpoint `none` and deletion signal `absence`. Changed-only means a timestamp or
generation checkpoint and deletion signal `none` or `tombstone`. Named-key coverage is changed-only.
A generation is a table-wide cursor, so window coverage cannot use a generation checkpoint. These
rules admit twelve semantic shapes. Complete windows include both endpoints; equal endpoints
represent a partition such as plant `IT01`. Domains are ordered scalar values of one type.

## Manifest

`Manifest(table, shape, produced_at, window, since, generation, rows, schema_hash, keys, tombstones,
content_hash)` states one read's claim. The manifest is the sender's testimony: the gate verifies the
rows against it and never the source against the rows. Dates are timezone-aware UTC instants. `window` is required exactly for window
coverage. `since` is required exactly for changed-only shapes. Timestamp checkpoints are datetimes;
generation checkpoints and issued generations are nonempty strings. A generation feed must issue its
next generation. A changed-only delivery collapses changes to one final live row or terminal tombstone
per key at the read boundary. `rows` counts incoming live rows, excluding `tombstones`. Keys and tombstones are
tuples of composite key tuples. A named-key manifest enumerates its live `keys`; other keyed shapes
may omit that enumeration and refer to the candidate's keys instead. Tombstones require a tombstone
shape, have the contract's key width and cannot also be live keys.

`produced_at` is the source snapshot or read boundary, not arrival time. The extractor guarantees all
changes through this boundary are present, including equal checkpoint timestamps. Wall-clock read
completion alone does not prove this. The gate receives the receiver's clock as `now`; a manifest
whose `produced_at` is later than `now` is refused, because a future-dated acceptance would block
every later honest delivery. `content_hash` is an optional digest of the delivered bytes or rows,
computed by the sender or by a receiver that derives the manifest; when present it is part of the
identity, so a corrected file is a new delivery and not a replay. A `rows` count that the receiver
derived from the file proves nothing about truncation; only `max_shrink_fraction` guards it. A timestamp gap is refused: `since` must be at or before every
covered region's `complete_as_of` and at or before `produced_at`. Keyed upserts tolerate overlapping
intervals. Append-only intervals must start exactly at the previous boundary of every covered region, with
extraction using `(since, produced_at]`; otherwise overlapping reads would duplicate events without
keys. A complete delivery on a keyless table restarts that boundary for its coverage, so after a
window delivery the regions differ and an append is refused until a complete whole-table delivery
aligns them. An interval whose `since` equals `produced_at` is empty and carries no rows. The first
append starts at `start_at` and establishes only that declared retained history.

A manifest's identity is SHA-256 of its identity form, defined under Versioned JSON. Reapplying an
accepted identity returns the same publication and receipt, even after later deliveries, and the
receipt says it is a replay. A core gate replay decision has an empty
operation plan and leaves the current ledger intact; a store returns the original persisted receipt. Different manifests sharing a timestamp
are not interchangeable snapshots. A corrected file needs a corrected manifest.

## Ledger and regions

A `Ledger` contains its contract, disjoint `Region` values covering the entire ordered domain, the
current table generation, accepted manifest identities and the current publication identity. Initially
there is one unbounded unknown region. Each region has `low`, `high`, endpoint-open flags,
`complete_as_of` and `valid_as_of`. The generation is one cursor for the whole table: a feed asserts
that no key changed anywhere else, so one cursor is all it can continue from. `None` endpoints mean
unbounded. Both dates are absent together in unknown regions; otherwise `valid_as_of <= complete_as_of`.
Explicit unknown gaps prevent an undelivered plant from disappearing from table claims. Empty gaps
between successive integers, calendar dates or microsecond datetimes are discarded. Adjacency is
measured in the scalar domain, so [1, 9] and [10, 20] leave no unknown integer between them.

| State | Condition |
|---|---|
| unknown | Both dates absent. |
| complete | Dates equal and completeness within the freshness limit. |
| patched | Completeness newer than validity and completeness within the limit. |
| expired | Completeness older than the freshness limit. |

Freshness is evaluated against a caller-supplied `now`. Equality at the limit remains fresh. The
validity date remains visible when recent patches hide old deletion knowledge.

## Transitions

Every coverage boundary splits existing regions exactly, preserving the excluded endpoints.
Adjacent regions with identical evidence merge. Regions outside coverage are unchanged.

- A complete delivery at `t` replaces covered regions' dates with `(t, t)`. An empty complete
  partition decommissions it, proving absence there. A complete whole-domain delivery sets the
  table generation to the manifest's generation, or clears it; a complete window carries none.
- Changed-only deliveries require a known base throughout coverage. Named-key streams speak about all
  regions, including the assertion that no other key changed. They cannot bootstrap a partial base.
- The append-only first interval is the sole exception to the base requirement because the contract
  explicitly bounds retained history. Subsequent append intervals are contiguous, and a complete
  delivery on a keyless table starts a new contiguous history at its own boundary.
- A generation feed requires `since` to equal the table generation and advances it to the issued
  generation. A broken chain is refused until a complete whole-domain delivery resets the cursor.
- Changed-only coverage raises completeness to `t`. A tombstone delivery raises validity to `t` in
  every covered region whose `valid_as_of` is at or after `since`, because it reports every deletion
  since its checkpoint; where validity is older than `since`, deletions in between were never
  reported, and validity stays unchanged. Without a delete signal validity never moves.
- Any nonduplicate delivery with `produced_at <= complete_as_of` in an affected known region is
  refused as a whole. This conservative rule keeps rows and dates consistent when files arrive out of
  order; even a partial overlap cannot overwrite a newer snapshot. Disjoint older windows are allowed.
- Unknown regions outside coverage stay unknown. Rejection leaves rows, ledger and publication intact.

`Ledger.apply` performs accounting and raises `RejectedDelivery` for an impossible transition. It does
not certify rows. Only a gate receipt with all required checks passing authorizes publication.

## Apply plan and gate

`ApplyPlan` is a value describing ordered `Action` values: `replace_all`, `replace_window`, `upsert`,
`tombstone`, `append`. Replacing a window deletes published rows inside it, matches candidate keys and
inserts the candidate. Upsert matches the contract key against candidate rows; named-key coverage also
verifies the manifest's enumeration. Tombstones delete exactly their named keys. Append inserts without
matching. A keyless complete delivery replaces its coverage without key matching. The core never reads rows or executes a plan.

`Gate.requirements` returns named row checks. A store returns one `Check(name, passed, detail)` for
each requirement. Missing, duplicate or unexpected outcomes reject the delivery. Core checks validate
manifest structure, replay identity, base coverage, timestamp continuity, generation continuity and
monotonic time. Row checks verify exact row count, schema hash against both manifest and contract,
window containment (null bounds-column values fail), non-null window values, non-null unique keys,
live-key enumeration and shrink tolerance. A window tombstone may not delete a published key outside
its window. A keyed row that moves into a window is admitted and the plan removes its earlier copy;
a row that moves out of a window leaves a hole the gate cannot see, which is why the window value is
a source guarantee. A check that cannot be evaluated because an earlier check failed is reported
with its name, `passed` false and the detail `not evaluated`, never with a predicate detail. Checks
verify the delivered rows, not whether the source told the truth. `max_shrink_fraction` defaults
to one half and is disabled only by an explicit `None`.

`Gate.evaluate` combines core checks and store outcomes into a `Receipt`: manifest, checks, acceptance,
`received_at` (the receiver's `now`), `replayed`, an optional `source` description the store
supplies, transition (before/after regions and plan), resulting ledger and claims at `received_at`,
receipt identity and publication identity. A rejected receipt references the existing publication. Receipt indexing uses the receiver
contract table, even when the rejected manifest names a different table. A successful publication identity is
a hash of the previous publication, contract and manifest identities. Receipt identity also includes
check outcomes. Readers recompute ledger claims with their current time to evaluate freshness. The audit history includes rejected attempts without making them publications. A failure after
commit cannot turn a publication into a rejection: the store returns the committed receipt on retry
or after recovering from cleanup failure. Unexpected exceptions and interrupts release staged
candidates and roll back any open transaction before propagating; an error must not leave a
half-executed candidate on a reusable connection.

## Claims and pull planning

`Ledger.claims(now, limit, window)` clips to the requested window (default whole domain). `Claims`
contains each table/region/state and the minimum of each date; either aggregate date is absent if any
region is unknown. `Claims.combine` takes the same minima across tables. State priority is unknown,
expired, patched, complete; the individual regions retain every other weakness.

A table's aggregate dates are lower bounds: every region is complete as of at least
`complete_as_of`, not the table at one instant. `Claims.combine` is the same lower bound across
tables. A query inherits those bounds only when it is monotone (a join, a filter, a union, an
aggregate over the inputs): a missing input row can only remove output rows, a ghost row can only
add them. Under negation (NOT EXISTS, an anti-join, a difference) the roles swap for the negated
input: a ghost row in the subtrahend removes a true answer, so the query's completeness depends on
the subtrahend's validity and the query's validity on its completeness. "Items with no movement"
computed over a patched movements table is valid but incomplete. The protocol gives the dates; the
reader applies the polarity of the query.

`Requirement` names a table, minimum completeness date, optional minimum validity date and optional
window. `plan_delivery` returns no request when claims suffice, otherwise the admitted shape with the
lowest declared cost that can establish the requirement, breaking ties by shape name. An unknown base
or stale deletion knowledge excludes change feeds from proving current validity. Finite window
requirements can use complete windows; whole-domain requirements need whole-domain evidence. If no
shape suffices, `PlanningError` explains the failure. Cost is a contract estimate, not a measured query
optimizer. `next_extraction` derives a rolling window from `window_size` and `until`, anchored to the ledger:
the window starts at the earlier of `until` minus the size and the first instant after the newest
complete evidence below `until`, so a late run leaves no unknown sliver behind it. A timestamp
interval runs from the weakest covered completeness boundary to `until`. `plan_delivery` reads a
requirement as minima and chooses a read boundary that is admissible, the later of the required date
and the newest covered evidence plus one unit, so a feasible later snapshot is never reported
impossible. A scheduler's logical date is not a read boundary. Each candidate request is simulated
against the ledger before it can be chosen; tombstones with an already sufficient validity date may
still be the cheapest way to advance completeness. Generation requests use the held cursor.
A request's `until` becomes the manifest's source boundary after extraction.

## Store obligations

The core never writes rows. Whoever does, a reference store or a user's own loader, meets the
obligations below; together they are the store contract, and the conformance suite checks them.

1. Checks before rows. Every `CheckRequirement` the gate names is answered against the candidate
   rows before any published row changes, and the plan executes only for an accepted receipt.
2. One publication. A publication is one accepted manifest, its candidate, its checks against a
   named base publication, the resulting rows and ledger, and its accepted receipt. The store
   exposes them as one committed step, the publication point, and a rejected delivery leaves rows
   and ledger unchanged. Two mechanisms give that step:
   - one transaction, when rows and ledger live in the same engine: rows, ledger, receipt and
     stamp commit together or not at all;
   - one table-format commit, when rows live in Iceberg, Delta or DuckLake: the commit carries the
     full receipt (or a reference to an already durable one), the expected parent publication and
     the resulting publication identity, and the ledger is derived from the committed history.
   Ordered writes with idempotent plans are not a third mechanism: a crash between rows and ledger
   leaves a stale ledger that a replay then confirms as success. A loader without either mechanism
   may use the core for accounting and must say that its publications are unprotected.
3. Compare and set. The checks, the plan and the receipt are computed against a base publication.
   The publication point refuses when the current publication is not that base, so two writers
   cannot both publish from the same ledger, and cached check outcomes are never reused against a
   different base.
4. Receipts are kept, in order. Every receipt, accepted or rejected, is persisted; the accepted ones
   in publication order reconstruct the ledger and its identity chain. Order matters: two disjoint
   windows commute on regions but not on the chain.
5. Replay returns the stored receipt. A manifest whose identity is already accepted produces no
   second publication; the store answers with the receipt it holds, marked as a replay.
6. One publication per read. A reader that combines rows and claims reads both under one
   publication identity; current rows and a separately loaded ledger can straddle a commit.

The publication stamp is the store's record of the current publication identity, written next to
the rows at the publication point. `load_ledger` compares the stamp with the ledger and refuses
deliveries while they differ. The stamp detects exactly two faults: a ledger behind the rows (a
crash after the publication point on a store whose ledger is a cache, repaired by folding the
receipts) and rows replaced outside the gate that moved the stamp (repaired only by a complete
delivery). It is an association label, not a checksum: it does not detect row edits that leave the
stamp in place, lost rejected receipts, or the joint loss of stamp and ledger, and a store that
promises those adds content digests or a retained trusted head. No stamp, no ledger and no accepted
receipts is a new table; retained accepted receipts disprove a fresh start.

`Ledger.from_receipts(contract, receipts)` ignores rejected attempts, folds accepted receipts in
publication order and checks each resulting ledger and publication identity; a missing, reordered or
foreign accepted receipt refuses reconstruction. `repair(contract)` restores only a ledger whose
reconstructed publication equals the stamp; it never stamps rows from ledger evidence.

## Contract evolution

A table's history binds `table`, `key`, `window_column` and the types of the window column and the
key; those cannot change, and a store refuses a contract that changes them. Everything else may
change between deliveries: `freshness`, `max_shrink_fraction`, shape costs and window sizes, new
shapes, and schema columns that are neither key nor window. The publication identity hashes the
contract, so the chain records exactly which contract each publication was judged against.

## Interfaces and entry points

`Store` and `Extractor` are `typing.Protocol` interfaces, not base classes. A store exposes
`deliver(contract, source, manifest)` as the gated push facade, holds a candidate, runs required
checks, executes a plan, publishes rows with ledger and receipt, aborts on failure, reads a
publication, loads a ledger, persists and lists receipts, and repairs a ledger from receipts. `Extractor.extract(contract, request)` returns a
manifest and an opaque row source consumable by a store. Extraction and storage are outside the core.

Push supplies a source plus manifest. Pull passes an `ExtractionRequest` to an extractor, then submits
the returned source and manifest through the same gate. The next request comes from required claims or
from `next_extraction`. Both modes produce identical ledgers for identical deliveries. A file feed may declare its shape
once at the receiver when filenames omit it. A declared filename pattern supplies the source read
boundary; missing row counts are counted from the file. A window without named bounds derives its
closed interval from the shape's `window_size`, ending at that UTC boundary (UTC calendar dates for DATE).
Missing shape evidence or missing bounds without a window size are refused. File arrival time is
never evidence of a source read boundary.

A loader that is not a `Store` can still meet the obligations with the core alone: load the ledger,
`manifest.check(contract)`, `Gate.requirements`, run the checks, `Gate.evaluate`, execute
`receipt.transition.plan`, save `receipt.ledger` and the receipt under obligation 2. The conformance
suite in `dogana.conformance` runs the same scenarios against any store object and is the proof a
store meets the contract. Reference stores and their constraints are described in
[reference](reference.md).

## Versioned JSON

Every structured value has `version: 1` and `kind` equal to its public class name. Field names match
constructor arguments. Every field is present, including null and empty defaults; otherwise a lost
field could silently change the declaration or its identity during decoding. Missing fields are
refused, as are overflowing scalar tags, through `SerializationError`. Tuples encode as arrays, enums as their string values, mappings as objects.
Wire field types are checked before constructor normalization; an array cannot stand in for a mapping
and a string cannot stand in for a key tuple. Scalar-tag names remain valid shape names.
UTC datetimes encode as `{"$datetime":"2026-10-18T00:00:00+00:00"}`, dates as
`{"$date":"2026-10-18"}`, durations as `{"$seconds":604800.0}`. Other supported bounds are strings
and integers. Type tags distinguish an ISO-looking plant code from a date. Unsupported versions,
unknown kinds, unknown fields, invalid types and naive datetimes are refused. Canonical JSON uses
sorted keys, compact separators and ASCII escapes, and is the wire form. The identity form is the
canonical JSON of a value with `version` removed, every field equal to its default omitted, `keys`
and `tombstones` sorted, integral floats written as integers and datetimes as UTC with `+00:00`.
An identity therefore survives a version that adds an optional field, and two implementations that
follow this page compute the same identity; the examples carry their identities as test vectors. `to_dict/from_dict` and `to_json/from_json` round-trip every public
wire value, including nested ledger and receipt values. Persistence belongs to the store.

A full shape and its manifest are:

```json
{"version":1,"kind":"Shape","coverage":"all","completeness":"complete","deletions":"absence","checkpoint":"none","cost":1,"window_size":null}
```

```json
{"version":1,"kind":"Manifest","table":"stock","shape":"full","produced_at":{"$datetime":"2026-10-18T00:00:00+00:00"},"window":null,"since":null,"generation":null,"rows":2,"schema_hash":null,"keys":[],"tombstones":[]}
```

Complete executable wire examples are [contract](examples/contract.json),
[manifest](examples/manifest.json), [ledger](examples/ledger.json) and [receipt](examples/receipt.json).
The tests decode them, replay their delivery and reproduce the ledger and receipt.

A contract encodes `shapes` as an object of these shape values and `schema` as an array of
`{"version":1,"kind":"Column","name":"id","type":"int"}` values. A ledger embeds that contract,
its region array, generation, accepted identity array and publication identity. A receipt embeds the
manifest, checks, transition, resulting ledger and claims. Replaying accepted manifests from receipts
reconstructs ledger evidence; reconstructing historical rows still needs the store's publications.

## Boundaries

The protocol declares, verifies, accounts and answers claims. Extraction belongs to dlt, Airbyte,
Spark or an existing script; scheduling to an orchestrator; business row rules to a quality tool; rows
to a store or table format. The core imports only the Python standard library and works with every
reference implementation removed. Reference implementations perform all I/O behind optional extras.
