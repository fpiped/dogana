# The dogana protocol

Version 4 of the wire format, the `version: 4` every document carries; the package is versioned
separately. [Concepts](concepts.md) explains the same ideas with examples.

## Notation

- A row is **live** at an instant if the source holds it then; a **deleted** row was live once and
  is not any more; a **tombstone** is a delivered row saying its key is deleted.
- `t` is a delivery's read boundary, `produced_at`; a region's dates are `(c, d)`, `c` its
  `complete_as_of` and `d` its `deletions_as_of`.
- `[a, b]` includes both ends, `(a, b]` leaves out `a`; a missing end, `None`, is unbounded.
- A delivery's **coverage** is the whole table or a closed window of the window column.

## Trust model

1. A delivery is rows and a `Manifest`, the sender's declaration of the read. The `Contract` states
   the source's guarantees; the `Gate` checks declaration and rows against each other, never against
   the source. A truncated full export with a matching count, an overstated window or an omitted
   late commit passes. A count derived by the receiver checks parsing, not completeness: only a
   count the export computed in the same read can catch a truncated file.
2. The invariant holds given truthful extraction, the declared guarantees and correct store checks:
   **a delivery never raises a date above what those premises prove**. The core checks that regions
   partition the domain, date order, continuity and that evidence outside coverage is unchanged. It
   does not authenticate senders, verify business values or certify a query's answer.
3. A store reads a file once, into a copy only it can reach, hashing the bytes as it copies, so the
   rows it checks and publishes are the bytes `content_hash` names.
4. CSV is [RFC 4180](https://www.rfc-editor.org/rfc/rfc4180): comma, double quotes escaped by
   doubling, UTF-8, a header row, no comments, no other dialect; a sender counting rows parses that
   grammar. A `datetime`, in a file or a manifest, carries its UTC offset or is refused. A file that
   does not parse is a recorded rejection, never a storage error.
5. `produced_at` is the instant of the source the file shows: the extractor guarantees every
   change up to it, that instant included. When the download finished, when the file arrived and a
   scheduler's logical date are not that instant. The gate rejects `produced_at` later than the
   receiver's time `now`. A read that is not one snapshot, such as an API paged for forty minutes,
   shows no single instant: whatever it declares is wrong by the rows created or deleted while it
   ran. A transaction that stamps `updated_at = 01:59:58` and commits at 02:00:03 is invisible to a
   read at 02:00, and to the gate. A keyed shape's `lookback` rereads an overlap to narrow that gap;
   no lookback proves it closed.

## Contract and shapes

`Contract` declares `table`, `key`, named `shapes`, optional `window_column`, `cursor_column` and
`deleted_column`, `types`, optional `schema_hash` and `start_at`.

**Types.** `types` maps exactly the key, window, cursor and deleted columns, no others, to logical
types `int`, `float`, `str`, `bool`, `date` or `datetime`; it is empty only when none are bound.
Keys and window bounds use ordered scalar types, a cursor `datetime` or `int`, the deleted column
`bool`.

**Column hash.** The data contract owns the columns; the delivery contract references them by an
opaque `schema_hash`, from an ODCS (Open Data Contract Standard) document, a dbt model or a column
list. When present, the manifest carries the same hash and the store checks its candidate against
it; when absent, no schema check is required. For an ODCS schema object, build one `[name, type]`
pair per property, in declared order. The type is one of dogana's six type names, or `decimal(p,s)`
for an exact decimal; the receiver maps physical and nested types to these names. Hash
`json.dumps(pairs, ensure_ascii=True, separators=(",", ":")).encode("ascii")` with SHA-256. Only the
pairs are hashed. The example store and file drop use this recipe; another store must agree with
its receiver on the hash.

**Window column.** A contract has one exactly when it has a window shape. Regions divide the
window column's values, not the keys. A key's window value never changes, even if the row is
deleted and inserted again: use a creation, order, document or event date, never an update time. A
row whose value moves between two regions proven at different instants can be missing from both.
So a window delivery refuses a key already published outside its window, and only a whole-table
delivery can move a key. "Rows updated in the last 30 days" is not a window: it is a changes
delivery whose `since` is 30 days back.

**Domains.** Keys are unique and non-null. Key and window values are strings, integers, dates or UTC
datetimes, of the type in `types`, as are manifest bounds. Aware datetimes are normalized to UTC
before equality or arithmetic, DST folds included. Strings use code-point order, without Unicode
normalization or collation; the successor of `s` is `s + "\x00"` and the minimum `""`. Date and
datetime domains end at the smallest and largest values Python represents, and a window reaching
one of them creates no empty region beyond it. A
window whose two ends are equal is one partition, such as one day or one plant. Delivering
partitions never proves the values no partition covers: that needs a full read.

**Keyless tables.** An empty `key` means rows have no identity: two equal events stay two rows, and
a complete delivery replaces everything in its coverage. A keyless shape of changes is an append:
whole table, timestamp checkpoint, no deletions. The source only appends and never deletes or
revises a row. The first append on an untouched ledger starts at the contract's `start_at`, the
start of the history the source keeps, and holds events after it, never at it. Tombstones,
generations and sequences need keys.

**Cursor column.** The column a changes read selects on, typed `datetime` or `int` in `types`.
With a `datetime` cursor, every live row of a timestamp read has its cursor in
`(since, produced_at]`; with an `int` cursor, every live row of a sequence read has it in
`(since, generation]`. A tombstone's cursor may be null. Without a cursor column, rows are not
checked against the declared interval.

**Deleted column.** A `bool` column, distinct from key, window and cursor. A contract has one
exactly when it has a tombstone shape. In a file, a row where it is true is a tombstone: it names a
deleted key, and its other values are not published and may be null, though typed where present. A
row where it is false is live. A null is neither, and fails the row count. A file may omit the
column, and then every row is live; only a tombstone shape may deliver true. The column is never
published, and the column hash leaves it out.

The keys a file deletes travel in the file, not in the manifest, so only `content_hash` tells two
such files apart: without it, a second file with the same manifest is a replay and is never read.
Declare `content_hash` for tombstone shapes, as `dogana manifest` and the file drop do.

A soft delete is a flag on a row the source keeps. A soft-deleting source may declare a tombstone
shape, delivering the flag as the deleted column or keeping flagged rows as data with no
tombstones, only if three things hold:

1. setting the flag also moves the cursor column, since otherwise a changes read never selects the
   flagged row and the deletion is never delivered;
2. the source removes nothing physically: no archiving, retention purge, legal erasure or cascade
   to child tables;
3. the flag is on the table being loaded, not on another one.

Otherwise the shape's deletions are `none`, and a periodic complete read removes the deleted rows.
With the flag as the deleted column, dates speak of the rows whose flag is false.

**Shapes.** A `Shape` has four properties:

| Property | Values | Meaning |
|---|---|---|
| coverage | `all`, `window` | The whole table, or a closed window of the window column. |
| completeness | `complete`, `changed_only` | All current rows in coverage, or all changes since a checkpoint. |
| deletions | `absence`, `tombstone`, `none` | A missing row is deleted, a tombstone names a deleted key, or nothing is said. |
| checkpoint | `generation`, `sequence`, `timestamp`, `none` | Opaque cursor, ordered integer position, source time, or none. |

1. Complete shapes have deletions `absence` and checkpoint `none`; changed-only shapes a timestamp,
   generation or sequence checkpoint and deletions `none` or `tombstone`.
2. Generations and sequences are positions in the whole table, so no window shape uses them, and a
   table uses one kind of position. That leaves ten shapes: complete, over the table or a window;
   changed-only over the table, with each of the three checkpoints, or over a window, by timestamp
   only. Each changed-only shape comes with or without tombstones.
3. `cost`, nonnegative, default one, is how expensive the file is to ask for; only the planner reads
   it.
4. `window_size`, optional and positive, is a planning hint, not a limit on pushed windows. Rolling
   durations need date or datetime windows, typed as in `types`: DATE widths are whole UTC calendar
   days including both ends (thirty days ending October 19 start September 20). Datetime widths are
   an elapsed UTC duration including both ends, at microsecond resolution.
5. `lookback`, nonnegative, is allowed only on timestamp shapes of keyed contracts. It starts a
   planned read that much earlier than the oldest `complete_as_of` it covers, so consecutive reads
   overlap. Keyless appends cannot overlap, since a reread event would count twice.

Shape constructors take keywords; `Contract` and `Manifest` take keywords after `shapes` and
`produced_at`. `Contract.manifest(shape, produced_at, ...)` fills in the table and the schema hash;
`Shape.window_ending_at` computes rolling windows. [Concepts](concepts.md#shapes) maps source
behaviors to shapes.

## Manifest

`Manifest(table, shape, produced_at, *, window, since, generation, rows, schema_hash, tombstones,
content_hash)` describes one read.

1. `window` is required exactly for window coverage, `since` exactly for changed-only shapes.
   Timestamp checkpoints are aware datetimes; generations are nonempty strings, and a generation
   feed issues its next one.
2. Sequence reads carry integers and cover `(since, generation]`, with `generation >= since`; an
   empty interval has no rows. Like `produced_at`, `generation` promises every change up to it, so
   positions must follow commit order. An auto-increment id does not: it is assigned at insert, so
   id 41 can commit after a read has already seen id 42. Read such ids only below every open
   transaction, or reread with overlap, which keyed sequence reads allow. `produced_at` must come
   before the commit of the first change after `generation`, so a consumer behind the head of the
   log declares the commit time of the last change it read. Keyless tables append by timestamp.
3. A delivery that issues a position issues the table's kind: an integer when the contract has
   sequence shapes, else a string. A complete whole-table delivery may issue one; a complete window
   never does.
4. A changed-only file holds one final row per key: the live row, or a tombstone when the key ends
   deleted. `rows` counts live rows and `tombstones` tombstones, every row of the file between them;
   keys never travel in the manifest. Tombstones need a tombstone shape, and a zero-length interval
   holds no rows and no tombstones.
5. Timestamp reads with a cursor column cover `(since, produced_at]`; without one, keyed reads may
   include `since` and may overlap. Keyless appends always cover `(since, produced_at]`, never
   overlapping.
6. `content_hash`, optional, is the lowercase hex SHA-256 of the delivered bytes, from sender or
   receiver. It is part of the manifest's identity, and the store verifies it against the bytes it
   read; rows without bytes cannot carry one. Without it, identity does not name the content, so the
   file drop fills it in.

**Corrections.** A corrected file is a new delivery by its hash and needs a later truthful boundary.
Invalidating an accepted delivery means rebuilding rows and ledger from the receipts before it, then
redelivering the correction and what followed; rebuilding the ledger alone recovers no rows.
Supersession at the same boundary is undecided.

## Ledger

A `Ledger` carries its contract, disjoint regions covering the domain, the table's generation and
`publication_id`, and no history of accepted identities. It starts as one unbounded unknown region.
A `Region` has `low`, `high`, open-endpoint flags, `complete_as_of` and `deletions_as_of`: both
dates absent, or `d <= c`.

For a region `(c, d)`: every row the source held at `c` with a window value in the region is
present, and every row retained there was live at some instant in `[d, c]`. Equal dates mean the
region is the source at that instant. Unknown is not empty: nothing has been proven there.

The ledger stores only the points where the evidence changes, each with the evidence up to the
next point; regions are the view between points. Neighbouring regions with equal dates merge, and
values no delivery touched stay unknown. `Ledger.apply` does accounting only: it checks the
transition and updates dates, position and publication chain. Checking rows and looking up replays
belong to store delivery.

## Transitions

| Delivery and previous evidence | Covered dates after delivery at t | Table generation |
|---|---|---|
| Complete whole domain, any evidence | `(t, t)` | Issued generation, or cleared. |
| Complete window, any evidence | `(t, t)` | Unchanged. |
| First keyless append, unknown | `(t, t)` | Issued if present, else unchanged. |
| Later keyless append, known | `(t, t)` | Issued if present, else unchanged. |
| Keyed timestamp changes without deletions, `(c, d)` | `(t, d)` | Issued if present, else unchanged. |
| Keyed timestamp tombstones, `(c, d)`, `since <= d` | `(t, t)` | Issued if present, else unchanged. |
| Keyed timestamp tombstones, `(c, d)`, `since > d` | `(t, d)` | Issued if present, else unchanged. |
| Generation changes without deletions, `(c, d)` | `(t, d)` | Issued generation. |
| Generation tombstones, `(c, d)`, `c = d` | `(t, t)` | Issued generation. |
| Generation tombstones, `(c, d)`, `c > d` | `(t, d)` | Issued generation. |
| Keyed sequence changes | as generation changes | `generation`, the read's last position. |

1. The manifest matches its shape and contract, bounds and key types included.
2. Every affected known region has `t > complete_as_of`: a late full cannot overwrite a newer
   window.
3. Every region a changed-only delivery covers is known. The one exception is the first keyless
   append on an untouched ledger, whose `since` equals `start_at`. Every later append has `since`
   equal to `complete_as_of` in every region it covers. Once a complete window moves one region
   ahead, no single `since` fits them all, and a whole-table append needs a complete whole-table
   delivery first.
4. Timestamp changes have `since <= t` and `since <= complete_as_of` everywhere covered.
5. Generation changes have `since` equal to the held generation; a broken chain needs a full reset.
6. Keyed sequence reads have `since <= position <= generation` for the held integer position: they
   may reread, never skip or go back. A missing or opaque position needs a complete delivery issuing
   one.
7. The gate also requires `t <= now`; `Ledger.apply` alone has no receiver clock, so it cannot.
8. Each acceptance extends the publication chain with the previous head, contract and manifest;
   disjoint windows commute on evidence, never on the chain.

## Plans, checks and receipts

An `ApplyPlan` holds ordered `Action`s: `replace_all` replaces the table; `replace_window` removes
the rows in its closed window and earlier copies of candidate keys, then inserts the candidate.
`upsert` replaces published rows sharing a key with a live candidate row; `tombstone` deletes the
published rows the tombstones name; `append` inserts without matching. Keyless replacement matches
no keys. The core describes operations and never reads rows.

`Gate.requirements(contract, manifest)` names the row checks; the store answers exactly one
`Check(name, passed, detail)` per name, and a missing, duplicate or unexpected answer rejects.

| Check | Predicate |
|---|---|
| row_count | Live rows equal `manifest.rows`, tombstones `manifest.tombstones`, and no deleted value is null. |
| schema_hash | Candidate and manifest hashes equal `contract.schema_hash`; absent when the contract has none. |
| rows_in_window | Every live window value is non-null and inside the window, and so is that of every published row sharing a key with a delivered row. |
| window_present | Every live window value is non-null, so each row falls in a region. |
| key_integrity | Every key, live or tombstoned, is non-null and appears once. |
| rows_in_interval | With a `datetime` cursor and a timestamp read, every live cursor value is non-null and in `(since, produced_at]`; with an `int` cursor and a sequence read, in `(since, generation]`. A tombstone's may be null, else in the same interval. |
| content_hash | With `manifest.content_hash`, the SHA-256 of the bytes read equals it. |

Every row check compares the rows with a declaration, the manifest's or the contract's, so an
honest delivery never fails one. A rule about the source's data, such as a table that should not
halve or an amount that must be positive, declares nothing and is a business check.

A window delivery refuses a key already published in another region instead of moving it, since
that region keeps its claim. A row whose window value changes at the source can still leave a hole
no check sees, so the window column must never change. A check that an earlier failure blocks
answers `passed=False, detail="not evaluated"`. Business rules run outside this set, and the caller
keeps their results.

`Gate.evaluate(ledger, manifest, outcomes, now=now, source=None)` adds the core checks: structure,
source time, base coverage, continuity and monotonic time. `Gate.preflight(ledger, manifest,
now=now)` returns them without rows; `Gate.reject(ledger, manifest, checks, now=now, source=None)`
records a declared failure.

A `Receipt` records one decision: the manifest, the contract in force, the checks, `outcome`,
`received_at`, an optional `source`, `base_publication_id`, the publication identity, and
`ledger_identity`, the identity of the ledger the decision leaves; neither identity is ever
empty. `outcome` is `accepted`,
`rejected` or `replayed`; the `accepted` property is true for `accepted` and `replayed`, the
`replayed` property only for `replayed`. An
acceptance or a replay carries a `Transition`: the plan, and the regions over the coverage
afterwards. A rejection leaves ledger and publication as they were.

The ledger is never in a receipt: it is the result of applying the accepted receipts in order, and
`ledger_identity` checks each step. A receipt grows with its coverage, never with history, rows or
deleted keys. It is filed under the contract's table even when a rejected manifest names another,
and its identity covers its checks.

`DoganaError` is the root of `InvalidContract` (with its subclass `SerializationError`),
`RejectedDelivery`, `PlanningError` and `StoreError` (with `InconsistentPublication`). Only declared
protocol violations become rejections; operational failures and programming errors propagate after
cleanup, and a failure after commit cannot revoke an acceptance.

## Claims and planning

`Ledger.claims(now, *, limit, window=None)` cuts the regions to `window`, if given, and states each
one at the reader's time `now`. How old is too old is the reader's decision, not the table's, so
every call names it. `limit`, a nonnegative duration, expires a region whose `complete_as_of` is
older than `limit` at `now`. A region exactly `limit` old is not expired, and `timedelta.max`
expires none. Claims never show a past state of the ledger.

| State | Condition |
|---|---|
| unknown | Both dates absent. |
| complete | Dates equal, and not expired. |
| patched | `complete_as_of` newer than `deletions_as_of`, and not expired. |
| expired | `complete_as_of` older than `limit` at `now`, whatever `deletions_as_of` says. |

1. The table's state is its worst region's, in the order unknown, expired, patched, complete. Its
   two dates are the minimum of each date over its regions, taken separately, and absent if any
   region is unknown. Equal minima do not make every region exact. The table's deletions date stays
   visible even when recent changes have raised every completeness date.
2. The claims of several tables, as `Group.claims` returns them, take the same minima across
   tables. They are lower bounds on each input's dates, not a statement about one snapshot or a
   joined result. Without a common instant they
   guarantee no row of a query over several tables or regions, even a query without negation. A
   later read can miss a row deleted after an earlier read, and carry a later version of a row.
3. Negation (`NOT EXISTS`, anti-joins, difference) swaps the two requirements for the negated
   input. Take items exact at `p` and movements complete as of `p` but patched: "items with no
   movement" lists only right answers, but may miss some, since a deleted movement still present
   hides its item. Exact `COUNT` and `SUM` need both dates.
4. `Requirement` names a table, a minimum `complete_as_of`, an optional minimum `deletions_as_of`
   and window. `plan_delivery` returns nothing if claims suffice, else the cheapest admitted shape
   that establishes it, ties broken by name, checking each candidate transition against the ledger.
   A feed is excluded when a covered region is unknown, or when the requirement needs a deletions
   date a feed cannot reach, since a feed starts from the held position and repairs deletions only
   where both dates are equal. A window shape meets a requirement on a bounded window; a
   requirement on the whole table needs a whole-table shape. No suitable shape raises
   `PlanningError`. The planned boundary may be later than the required date, because it must be
   later than every covered `complete_as_of`, and the extractor must then read at that boundary.
5. `next_extraction(ledger, shape_name, until, *, window=None)` derives a rolling window from
   `window_size`, extended back to meet the last complete evidence. For a timestamp shape, `since`
   is the earliest covered `complete_as_of` minus `lookback`; an append needs every covered region
   to share one `complete_as_of`; a generation request starts from the held generation. Declare
   `until` as `produced_at` only if the read saw the source at `until`. Files a sender
   pushes and reads a receiver pulls pass the same gate.

## Tables together

**Exact.** A region is exact at `p` when its dates are `(p, p)`, and claims are exact at `p` when
every region is. `Claims.exact_as_of` is that `p`, or `None`. Equal table dates do not imply it,
since they are minima: a window proven later than the rest of its table gives equal minima and no
common instant. Freshness does not affect it; `state` still reports expiry.

**Theorem.** If every region a query reads, in every table, is exact at the same `p`, the query
answers as the source would have at `p` (filters, joins, unions, negation and exact aggregates).
This requires that both sides compare and compute alike (collation, nulls, the order of a
floating-point sum) and every rule the source keeps between its tables holds between the published
ones.

**Premise.** Every file comes from one consistent read of the source at `p`, stamped `p`, which
dogana cannot verify. One read is one snapshot of every table. READ COMMITTED, the default of
Postgres, Oracle and SQL Server, takes a new snapshot for each statement, so it is not one read. One
read is REPEATABLE READ, SERIALIZABLE or one exported snapshot shared by workers in Postgres; `START
TRANSACTION WITH CONSISTENT SNAPSHOT` in MySQL; a read-only transaction or queries `AS OF` one SCN
(system change number) in Oracle; SNAPSHOT isolation in SQL Server. Separate export jobs are not one
read, and one `produced_at` for them, such as one date in their file names, fabricates the instant.
The theorem needs no common store transaction: tables each exact at `p`, each read with its ledger
under one publication (obligation 6), are consistent however they were published.

**Group and reference.** `Group(contracts, references=())` names tables read together, each once
whatever its letter case. A table belongs to one group, since a table accepted in one group does not
replay in another: a parent with several children is one group. `Group.claims(ledgers, now, *,
limit, windows=None)` returns every table's regions together, each table clipped to its window.
`Reference(table, columns, parent)` states that every live row of `table` whose `columns` are all
non-null names, in order, the key of a `parent` row; it has one column per parent key column, and
both tables are in the group.

**Delivery.** `deliver_together(store, group, deliveries, now)` takes one `(source, manifest)` per
table and decides them as one:

1. Each table runs a single delivery up to its decision: replay lookup, preflight, `hold`, `check`.
2. The `together` check passes when every manifest has the same `produced_at` and every table is
   accepted alone. Its detail digests every manifest identity and the references, so a receipt
   carrying it was accepted with exactly those files.
3. When the group has references and `together` passed, the store answers one more check,
   `references`, on the rows each table would hold once its plan is applied. It covers only the
   references `Group.unchecked` gives no reason to skip.
4. Both checks join every receipt through `Gate.evaluate(..., joint=checks)`. If any receipt is a
   rejection, every table records its refusal; otherwise `publish_together` publishes every table's
   rows, ledger and receipt in one atomic step.

**When a reference is tested.** Only when the child's delivered coverage ends exact at `p` and the
parent is complete as of `p` over its whole domain. Then every child row there was live at `p`, and
so was every parent row it can name, which is therefore present. A child row whose reference columns
are all non-null and name no parent row proves the premise false, or that the source breaks the
rule. In every other case the reference passes untested and the detail says why, so an honest
delivery is never refused for one. A child without tombstones, or a parent delivered by windows,
leaves it untested, and `exact_as_of` tells the reader. A tested reference covers every child row in
the coverage, so the store scans the child and joins the parent on each delivery. Values compare as
values, int and float alike; a reference column typed other than its parent key column is a
declaration error, and fails as soon as one row names a parent.

**Reaching `(p, p)`.** `together` does not require the result to be exact; each table's shape
decides it over that table's coverage. These reach `(p, p)`: a complete delivery, a keyless append,
timestamp tombstones with `since` no later than the deletions date, and generation or sequence
tombstones when both dates were already equal. A table without changes delivers an empty file of
a tombstone changes shape at `p`. A reader of windows passes them to `Group.claims`.

**Replay and races.**

1. A group replays whole or is refused whole: when every manifest was accepted in one delivery of
   these files under these references, every receipt replays, provided every declared hash still
   matches the bytes. When any was accepted alone, in another delivery or under other references,
   every table is refused, and the group needs a new read.
2. A failure after the group's commit returns the committed receipts. If another delivery accepts
   the same manifests in the meantime, this one is decided by rule 1, as if the other came first, so
   the outcome does not depend on timing. If that other delivery lands while this one is recording
   refusals, the call raises `StoreError`, and a retry decides it. Only a publication that committed
   some tables and not others raises `InconsistentPublication`. Two concurrent deliveries with equal
   manifests and receiver times give one receipt, so the one that published nothing may also be
   answered `accepted`.
3. Each table keeps its receipts, chain and reconstruction; a group adds no wire kind.

Atomic publication serves availability, not the guarantee: without it a refused table leaves the
others ahead and the current tables have no common instant; with it, the instant moves in one step.

`plan_together(group, ledgers, at_least, *, windows=None)` returns nothing when the group is already
exact at an instant no earlier than `at_least`. Otherwise it returns one request per table with one
`until`, later than every table's evidence and no earlier than `at_least`, each the cheapest read
establishing `(until, until)` over its window. `until` is the earliest admissible boundary: the
extractor reads every table in one snapshot taken at or after it and stamps every manifest with its
instant.

`TogetherStore` adds `check_references(group, candidates)` and `publish_together(group, entries)`,
`candidates` mapping each table to its checked candidate and `entries` pairing one candidate per
table with its accepted receipt. The store must:

1. answer the references check on the rows the candidates would produce, without changing published
   rows, only for candidates that passed their own checks and references `Group.unchecked` leaves;
2. verify that each receipt agrees with its checks, that the `together` check equals the one
   recomputed from the receipts and the references. When there are references, verify that the
   `references` answer is the one it gave itself for exactly these candidates after their last
   check. Without that answer it refuses to publish;
3. publish every entry or none in one atomic step, under the six obligations of a single
   publication.

## Store protocol and publication

`Store` and `Extractor` are structural `typing.Protocol`s; `extract(contract, request)` returns
`(manifest, source)`, whose representation belongs to the store. `dogana.deliver(store, contract,
source, manifest, now)` owns the sequence:

1. `load_ledger(contract)`; `accepted_receipt(manifest.identity)` returns a stored acceptance as
   `replayed`, with original identity and receiver time. A manifest with `content_hash` replays only
   once `digest(source)` equals it, else it is refused; a replay compares bytes, never rows against
   today's contract.
2. Preflight; `publish(None, receipt)` records a rejection against its expected parent without
   changing rows or evidence.
3. `hold(source, manifest, contract)` stages a candidate; `check(candidate, contract, manifest,
   requirements)` answers the row checks against one base publication.
4. `publish(candidate, receipt)` compares that base and atomically executes an accepted plan with
   its rows and evidence, or records the rejection; `abort(candidate)` releases staged work after a
   failure.

These six, with `receipts(table=None)`, in persistence order, and `digest(source)`, the SHA-256 of a
source's bytes or `None` for rows, are the eight required methods. Publishing an acceptance needs a
held and checked candidate. Recording a rejection also compares and sets the parent, and a receipt
that cannot be decoded is refused before the log changes. `publication_stamp(table)` and
`repair(contract)` form the optional stamped tier, `check_references` and `publish_together` the
[together tier](#tables-together); read methods and `store.deliver` are conveniences.

The store's six obligations:

1. Check candidate rows before published rows change; execute only an accepted receipt's plan.
2. Publish rows, ledger, receipt and any stamp in one atomic step: one transaction, or a
   table-format commit carrying the receipt or a reference to a durable one. Ordered writes with
   idempotent plans are not atomic, and `Ledger.apply` alone guarantees nothing about rows.
3. Compare and set against `receipt.base_publication_id`, refusing a changed head, cached checks on
   a stale base included; a commit retry never silently rebases an approved plan.
4. Persist accepted and rejected receipts; keep accepted ones in publication order.
5. Answer a replay with its original receipt and no second publication, after verifying a declared
   `content_hash` against the bytes.
6. Read rows and claims under one publication identity; separate reads can straddle a commit.

**Chain and recovery.** The publication chain is SHA-256 of UTF-8
`f"{previous}:{contract.identity}:{manifest.identity}"`, the first `previous` the string `None`. The
store stamps the published rows with that head; the stamp is not a checksum of the rows. Comparing
it with the ledger finds a ledger behind its rows, or a publication from outside dogana. It cannot
see a row edit that keeps the stamp, a lost rejection or the loss of all evidence. When a ledger is
missing, surviving receipts or stamps show the table is not new. Stores fail closed.

`Ledger.follow(receipt)` applies one accepted receipt and checks its publication and ledger
identities; `Ledger.from_receipts(contract, receipts)` folds the accepted ones in order, ignoring
rejections and refusing missing, reordered or foreign ones. `repair` restores the ledger only when
the rebuilt head equals the stamp, never relabelling rows; rows need retained store versions.
`Ledger.reconcile(contract, receipts, current, stamp, *, repair=True, retained_head=None)`
diagnoses, and with `repair` returns rebuilt evidence only for a matching stamp: `LedgerAhead`,
evidence beyond the stamp, needs a fresh full replacement; `StampAhead`, rows without matching
evidence, needs receipt recovery or restoration. Both are `InconsistentPublication`, matched by
type. A store that verifies retention can pass `retained_head`: diagnosis then checks the current
ledger against that receipt's `ledger_identity` and its hash against the stamp, without replaying
the prefix. Repairs and mismatches still fold the whole history.

## Algebraic guarantees

The same initial ledger and accepted history give the same ledger. Store delivery is idempotent by
receipt lookup; `Ledger.apply` is not. Disjoint complete or timestamp windows commute on evidence
when both orders are admissible, never on the chain. Ledgers cannot be merged. Rejection applies per
manifest, not to a batch or a history.

## Contract evolution

Once a table has history, its `table`, `key`, `window_column`, `deleted_column` and the types of
the key and window columns are fixed, and stores refuse a contract that changes them. The deleted
column decides which rows the dates describe, so a new one makes old dates speak of other rows.
Everything else may evolve: costs, window sizes, lookback, shapes, cursor columns, the column hash.
Each
receipt keeps its contract. Every contract change alters the chain, since the whole contract, with
normalized numbers, enters it, but only a change to a fixed field refuses the history.
`Ledger.with_contract(contract)` checks compatibility and keeps regions, generation and head;
reconstruction uses each receipt's contract, then the requested one.

## Versioned JSON

1. Structured values carry `version: 4` and `kind`, the class name. Earlier versions are refused:
   version 1 manifests (dogana 0.1) listed keys, version 2 receipts (0.2 and 0.3) carried ledgers,
   version 3 contracts (0.4 to 0.6) carried a shrink limit.
2. Field names match constructor arguments, and every field is present, defaults included. Tuples
   are arrays, enums strings, mappings objects. JSON has one number type: a number with a zero
   fraction, such as `4.0` or `-0.0`, is the integer it spells wherever an integer is expected, as
   JSON Schema reads it. Missing or unknown fields, unknown versions or kinds, wrong types,
   overflowing tags and naive datetimes raise `SerializationError` before normalization.
3. Tags: dates `{"$date":"2026-10-18"}`; datetimes `{"$datetime":"2026-10-18T00:00:00+00:00"}`,
   RFC 3339 with `Z` or an offset and at most six fractional digits, emitted in UTC without
   fractional seconds when zero, else with six digits; durations `{"$seconds":604800}`, nonnegative
   and integral or with exactly representable microseconds. A tag outside its grammar, a duration
   that would round and a datetime that would lose digits are refused, never truncated. Strings
   reject lone surrogates.
4. Canonical JSON has sorted keys, compact separators and ASCII escapes. Identity is SHA-256 of
   [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) (JSON Canonicalization Scheme) over the identity
   form: `version` and fields at their defaults omitted recursively, datetimes in UTC `+00:00`, a
   replay's `outcome` as `accepted`. `identity_dict()` and `identity_json()` expose it.
5. JCS emits UTF-8, sorts keys by UTF-16 code units and prints binary64 numbers by the ECMAScript
   shortest round trip, `-0` as `0`. Integers must be exact binary64, never rounded; portable keys
   stay within `[-(2**53-1), 2**53-1]`, larger ones as strings. Full wire JSON keeps exact integers,
   while JCS may spell a large exact binary64 value with another shortest token. Do not hash sidecar
   bytes.
6. Optional future fields at defaults keep identity; semantic changes need migration, and unknown
   versions are refused even with a compatible identity.

`to_dict`/`from_dict` and `to_json`/`from_json` round-trip nested values. The
[contract](examples/contract.json), [manifest](examples/manifest.json),
[ledger](examples/ledger.json) and [receipt](examples/receipt.json) examples are executable,
[identity vectors](examples/identity-vectors.json) fix canonical forms and hashes for other
implementations, and [JSON Schemas](wire/README.md) cover all fifteen wire kinds.

## Boundaries

The package uses only the standard library, beside pytest for the conformance suite, and parses no
data file. The [reference](reference.md) covers the example store, the command line and the
accounting path; passing the conformance suite is evidence for its scenarios, not a proof.

Undecided: a table namespace, finite string domains, more than one window column, receiver and
reader commands, supersession at the same boundary, and the compatibility policy.
