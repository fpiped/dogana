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
An empty key means append-only; such a contract declares `start_at`, the beginning of its retained
history, and admits only whole-domain timestamp shapes without deletions. Window bounds and key
components are strings, integers, dates or UTC datetimes; window and key schema columns use these
logical types.

A `Shape` has four semantic properties, plus a nonnegative relative extraction `cost` (default one)
and an optional positive `window_size` duration for automatic rolling-window planning:

| Property | Values | Meaning |
|---|---|---|
| coverage | `all`, `window`, `keys` | Whole domain, closed interval on the window column, or keys in a table-wide change stream. |
| completeness | `complete`, `changed_only` | All current rows in coverage, or all changes since the checkpoint. |
| deletions | `absence`, `tombstone`, `none` | Missing rows delete, explicitly named keys delete, or no delete signal. |
| checkpoint | `generation`, `timestamp`, `none` | Opaque source cursor, inclusive source timestamp, or no checkpoint. |

Complete means checkpoint `none` and deletion signal `absence`. Changed-only means a timestamp or
generation checkpoint and deletion signal `none` or `tombstone`. Named-key coverage is changed-only.
These rules admit fourteen semantic shapes. Complete windows include both endpoints; equal endpoints
represent a partition such as plant `IT01`. Domains are ordered scalar values of one type.

## Manifest

`Manifest(table, shape, produced_at, window, since, generation, rows, schema_hash, keys, tombstones)`
states one read's claim. Dates are timezone-aware UTC instants. `window` is required exactly for window
coverage. `since` is required exactly for changed-only shapes. Timestamp checkpoints are datetimes;
generation checkpoints and issued generations are nonempty strings. A generation feed must issue its
next generation. A changed-only delivery collapses changes to one final live row or terminal tombstone
per key at the read boundary. `rows` counts incoming live rows, excluding `tombstones`. Keys and tombstones are
tuples of composite key tuples. A named-key manifest enumerates its live `keys`; other keyed shapes
may omit that enumeration and refer to the candidate's keys instead. Tombstones require a tombstone
shape, have the contract's key width and cannot also be live keys.

`produced_at` is the source snapshot or read boundary, not arrival time. The extractor guarantees all
changes through this boundary are present, including equal checkpoint timestamps. Wall-clock read
completion alone does not prove this. A timestamp gap is refused: `since` must be at or before every
covered region's `complete_as_of` and at or before `produced_at`. Keyed upserts tolerate overlapping
intervals. Append-only intervals must start exactly at the previous boundary, with extraction using
`(since, produced_at]`; otherwise overlapping reads would duplicate events without keys. The first
append starts at `start_at` and establishes only that declared retained history.

A manifest's identity is SHA-256 of its canonical JSON. Reapplying an accepted identity returns the
same publication and receipt, even after later deliveries. A core gate replay decision has an empty
operation plan and leaves the current ledger intact; a store returns the original persisted receipt. Different manifests sharing a timestamp
are not interchangeable snapshots. A corrected file needs a corrected manifest.

## Ledger and regions

A `Ledger` contains its contract, disjoint `Region` values covering the entire ordered domain, the
current table generation, accepted manifest identities and the current publication identity. Initially
there is one unbounded unknown region. Each region has `low`, `high`, endpoint-open flags,
`complete_as_of`, `valid_as_of`, and the source `generation` it can continue from. `None` endpoints mean
unbounded. Both dates are absent together in unknown regions; otherwise `valid_as_of <= complete_as_of`.
Explicit unknown gaps prevent an undelivered plant from disappearing from table claims.

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

- A complete delivery at `t` replaces covered regions' dates with `(t, t)` and their generation with
  the manifest's generation. An empty complete partition decommissions it, proving absence there.
- Changed-only deliveries require a known base throughout coverage. Named-key streams speak about all
  regions, including the assertion that no other key changed. They cannot bootstrap a partial base.
- The append-only first interval is the sole exception to the base requirement because the contract
  explicitly bounds retained history. Subsequent append intervals are contiguous.
- A generation feed requires `since` to equal both the table generation and every affected region's
  generation. Advancing a window's cursor cannot silently advance another window's cursor. A complete
  full delivery resets the table cursor, including clearing it when no generation is supplied.
- Changed-only coverage raises completeness to `t`. Tombstones raise validity to `t` only where
  validity equalled completeness before the delivery. A feed cannot repair deletions missed by an
  earlier delivery without a delete signal. Else validity stays unchanged.
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
matching. The core never reads rows or executes a plan.

`Gate.requirements` returns named row checks. An adapter returns one `Check(name, passed, detail)` for
each requirement. Missing, duplicate or unexpected outcomes reject the delivery. Core checks validate
manifest structure, replay identity, base coverage, timestamp continuity, generation continuity and
monotonic time. Row checks verify exact row count, schema hash against both manifest and contract,
window containment (null bounds-column values fail), stable window values for existing keys,
non-null unique keys, live-key enumeration and
shrink tolerance. A window tombstone may not delete a published key outside its window. Complete
window deliveries also check the immutable window-column guarantee for keys already published.
Checks verify the delivered rows, not whether the source told the truth.

`Gate.evaluate` combines core checks and adapter outcomes into a `Receipt`: manifest, checks, acceptance,
transition (before/after regions and plan), resulting ledger and claims, receipt identity and publication
identity. A rejected receipt references the existing publication. A successful publication identity is
a hash of the previous publication, contract and manifest identities. Receipt identity also includes
check outcomes. The audit history includes rejected attempts without making them publications.

## Claims and pull planning

`Ledger.claims(now, limit, window)` clips to the requested window (default whole domain). `Claims`
contains each table/region/state and the minimum of each date; either aggregate date is absent if any
region is unknown. `Claims.combine` takes the same minima across tables. State priority is unknown,
expired, patched, complete; the individual regions retain every other weakness.

`Requirement` names a table, minimum completeness date, optional minimum validity date and optional
window. `plan_delivery` returns no request when claims suffice, otherwise the admitted shape with the
lowest declared cost that can establish the requirement, breaking ties by shape name. An unknown base
or stale deletion knowledge excludes change feeds from proving current validity. Finite window
requirements can use complete windows; whole-domain requirements need whole-domain evidence. If no
shape suffices, `PlanningError` explains the failure. Cost is a contract estimate, not a measured query
optimizer. `next_extraction` derives a window from `window_size` and `until`, or a timestamp interval
from the weakest covered completeness boundary to `until`. Each candidate request is simulated
against the ledger before it can be chosen; tombstones with an already sufficient validity date may
still be the cheapest way to advance completeness. Generation requests use the held cursor.
A request's `until` becomes the manifest's source boundary after extraction.

## Interfaces and entry points

`Store` and `Extractor` are `typing.Protocol` interfaces, not base classes. A store exposes `deliver(contract, source, manifest)` as the gated push facade, holds a candidate,
runs required checks, executes a plan, publishes rows with ledger and receipt atomically, aborts on
failure, reads a publication, loads a ledger and persists rejected receipts. `Extractor.extract(contract,
request)` returns a manifest and an opaque row source consumable by a store. Extraction and storage
are outside the core.

Push supplies a source plus manifest. Pull passes an `ExtractionRequest` to an extractor, then submits
the returned source and manifest through the same gate. The next request comes from required claims or
from `next_extraction`. Both modes produce identical ledgers for identical deliveries.

The DuckDB adapter uses a transaction against the published table, with the incoming file held in a
temporary candidate table first. DuckDB isolation hides uncommitted changes and permits atomic rows,
ledger JSON and receipt JSON without a second mutable copy. Immutable publication tables support
`read(..., publication_id=...)`; these copies deliberately trade storage for a small reference adapter.
Contracts cannot change within a table's history. One store instance serializes deliveries.

The file-drop adapter accepts CSV or Parquet paths. `<file>.json` is the optional sidecar. Without it,
an explicit full-match filename regular expression supplies named groups `shape`, `produced_at`,
`rows`, and optional `low`, `high`, `since`, `generation`. Bounds use the contract column's logical type;
`schema_hash` defaults to the declared schema hash and is checked against the actual file. File arrival
order is delivery order; there is no watcher, file mover or scheduler. CSV types are inferred from all
rows. For a header-only CSV, the declared schema supplies types because the payload contains no type
evidence. Parquet retains its physical schema even when empty. Successful candidates normalize logical types to DuckDB BIGINT, DOUBLE,
VARCHAR, BOOLEAN, DATE and TIMESTAMPTZ, so publication types do not depend on file inference. Neither encoding silently accepts
missing or retyped columns.

## Versioned JSON

Every structured value has `version: 1` and `kind` equal to its public class name. Field names match
constructor arguments. Tuples encode as arrays, enums as their string values, mappings as objects.
UTC datetimes encode as `{"$datetime":"2026-10-18T00:00:00+00:00"}`, dates as
`{"$date":"2026-10-18"}`, durations as `{"$seconds":604800.0}`. Other supported bounds are strings
and integers. Type tags distinguish an ISO-looking plant code from a date. Unsupported versions,
unknown kinds, unknown fields, invalid types and naive datetimes are refused. Canonical JSON uses
sorted keys and compact separators. `to_dict/from_dict` and `to_json/from_json` round-trip every public
wire value, including nested ledger and receipt values. Persistence belongs to the store.

A full shape and its manifest are:

```json
{"version":1,"kind":"Shape","coverage":"all","completeness":"complete","deletions":"absence","checkpoint":"none","cost":1,"window_size":null}
```

```json
{"version":1,"kind":"Manifest","table":"stock","shape":"full","produced_at":{"$datetime":"2026-10-18T00:00:00+00:00"},"window":null,"since":null,"generation":null,"rows":2,"schema_hash":null,"keys":[],"tombstones":[]}
```

A contract encodes `shapes` as an object of these shape values and `schema` as an array of
`{"version":1,"kind":"Column","name":"id","type":"int"}` values. A ledger embeds that contract,
its region array, generation, accepted identity array and publication identity. A receipt embeds the
manifest, checks, transition, resulting ledger and claims. Replaying accepted manifests from receipts
reconstructs ledger evidence; reconstructing historical rows still needs the store's publications.

## Boundaries

The protocol declares, verifies, accounts and answers claims. Extraction belongs to dlt, Airbyte,
Spark or an existing script; scheduling to an orchestrator; business row rules to a quality tool; rows
to a store or table format. The core imports only the Python standard library and works with every
adapter removed. Adapters perform all I/O behind optional extras.
