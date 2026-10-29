# The dogana protocol

<!-- protocol: version -->

Version 4 of the wire format, the `version: 4` every document carries; the package is versioned
separately. [Concepts](concepts.md) explains the same ideas with examples.

## Notation

- A row is <!-- protocol: rows --> **live** at an instant if the source holds it then; a **deleted** row was live once and
  is not any more; a **tombstone** is a delivered row saying its key is deleted.
- `t` is <!-- protocol: dates --> a delivery's read boundary, `produced_at`; a region's dates are `(c, d)`, `c` its
  `complete_as_of` and `d` its `deletions_as_of`.
- `[a, b]` includes <!-- protocol: intervals --> both ends, `(a, b]` leaves out `a`; a missing end, `None`, is unbounded.
- A delivery's **coverage** <!-- protocol: coverage --> is the whole table or a closed window of the window column.

## Trust model

1. A delivery is rows and a `Manifest`, the sender's declaration of the read. The `Contract` states
   the source's guarantees. dogana decides from the declarations and the ledger alone: it never
   reads rows or bytes, so it cannot tell a truncated full export with a matching count, an
   overstated window or an omitted late commit. A count the receiver derives from the file checks
   parsing, not completeness: only a count the export computed in the same read can catch a
   truncated file.
2. The invariant holds given truthful declarations: **a delivery never raises a date above what the
   declarations prove**. The core checks that regions partition the domain, date order, continuity
   and that evidence outside coverage is unchanged. It does not authenticate senders, verify
   business values or certify a query's answer.
3. The receiver owns the rows. The dates describe the rows only if the receiver refuses a delivery whose
   contents contradict its manifest, and saves rows and ledger in the same commit, so that no reader
   sees one without the other. dogana names neither how to check nor how to commit; it records the
   checks the receiver reports, and [the receiver](#the-receiver) states what it owes.
4. A `datetime`, in a read or a manifest, carries its UTC offset or is refused.
5. `produced_at` is the instant of the source the read shows: the extractor guarantees every
   change up to it, that instant included. When the download finished, when the delivery arrived and a
   scheduler's logical date are not that instant. The gate rejects `produced_at` later than the
   receiver's time `now`. A read that is not one snapshot, such as an API paged for forty minutes,
   shows no single instant: whatever it declares is wrong by the rows created or deleted while it
   ran. A transaction that stamps `updated_at = 01:59:58` and commits at 02:00:03 is invisible to a
   read at 02:00, and to the gate. A keyed shape's `lookback` rereads an overlap to narrow that gap;
   no lookback proves it closed.

## Contract and shapes

<!-- protocol-section: contract -->

<!-- protocol: declaration -->

`Contract` declares `table`, `key`, named `shapes`, optional `window_column`, `cursor_column` and
`deleted_column`, `types`, optional `schema_hash` and `start_at`.

**Types.** `types` maps exactly the key, window, cursor and deleted columns, no others, to logical
types `int`, `float`, `str`, `bool`, `date` or `datetime`; it is empty only when none are bound.
Keys and window bounds use ordered scalar types, a cursor `datetime` or `int`, the deleted column
`bool`.

**Column hash.** The data contract owns the columns; the delivery contract references them by an
opaque `schema_hash`, from an ODCS (Open Data Contract Standard) document, a dbt model or a column
list. When present, a manifest that does not carry the same hash is refused, and the receiver
computes the hash of the columns it loads and refuses a delivery with other columns; when absent, no
column check is required. For an ODCS schema object, build one `[name, type]`
pair per property, in declared order. The type is one of dogana's six type names, or `decimal(p,s)`
for an exact decimal; the receiver maps physical and nested types to these names. Hash
`json.dumps(pairs, ensure_ascii=True, separators=(",", ":")).encode("ascii")` with SHA-256. Only the
pairs are hashed.

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
exactly when it has a tombstone shape. In a read, a row where it is true is a tombstone: it names a
deleted key, and its other values are not published and may be null, though typed where present. A
row where it is false is live. A null is neither, and fails the row count. A read may omit the
column, and then every row is live; only a tombstone shape may deliver true. The column is never
published, and the column hash leaves it out.

<!-- protocol: deleted-content -->

The keys a file deletes travel in the file, not in the manifest, so only `content_hash` tells two
such files apart: without it, a second file with the same manifest is a replay, and the receiver
never reads it. Declare `content_hash` for tombstone shapes, as `dogana manifest` does.

<!-- protocol-scope: soft-deletes -->

<!-- protocol: soft-deletes -->

A soft delete is a flag on a row the source keeps. A soft-deleting source may declare a tombstone
shape, delivering the flag as the deleted column or keeping flagged rows as data with no
tombstones, only if three things hold:

1. setting the flag also moves the cursor column, since otherwise a changes read never selects the
   flagged row and the deletion is never delivered;
2. the source removes nothing physically: no archiving, retention purge, legal erasure or cascade
   to child tables;
3. the flag is on the table being loaded, not on another one.

<!-- protocol: soft-delete-fallback -->

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
3. `cost`, nonnegative, default one, is how expensive the read is to ask for; only the planner reads
   it.
4. `window_size`, optional and positive, is a planning hint, not a limit on pushed windows. Rolling
   durations need date or datetime windows, typed as in `types`: DATE widths are whole UTC calendar
   days including both ends (thirty days ending October 19 start September 20). Datetime widths are
   an elapsed UTC duration including both ends, at microsecond resolution.
5. `lookback`, nonnegative, is allowed only on timestamp shapes of keyed contracts. It starts a
   planned read that much earlier than the oldest `complete_as_of` it covers, so consecutive reads
   overlap. Keyless appends cannot overlap, since a reread event would count twice.

<!-- protocol: constructors -->

Shape constructors take keywords; `Contract` and `Manifest` take keywords after `shapes` and
`produced_at`. `Contract.manifest(shape, produced_at, ...)` fills in the table and the schema hash;
`Shape.window_ending_at` computes rolling windows. [Concepts](concepts.md#shapes) maps source
behaviors to shapes.

## Manifest

<!-- protocol: declaration -->

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
4. A changed-only read holds one final row per key: the live row, or a tombstone when the key ends
   deleted. `rows` counts live rows and `tombstones` tombstones, every row of the read between them;
   keys never travel in the manifest. Tombstones need a tombstone shape, and a zero-length interval
   holds no rows and no tombstones.
5. Timestamp reads with a cursor column cover `(since, produced_at]`; without one, keyed reads may
   include `since` and may overlap. Keyless appends always cover `(since, produced_at]`, never
   overlapping.
6. `content_hash`, optional, is the lowercase hex SHA-256 of the delivered bytes. It is part of the
   manifest's identity, so a corrected file is a new delivery, and the receiver verifies it against
   the bytes it loads. Without it, identity does not name the content.

**Corrections.** A corrected file is a new delivery by its hash and needs a later truthful boundary.
Invalidating an accepted delivery means rebuilding rows and ledger from the receipts before it, then
redelivering the correction and what followed; rebuilding the ledger alone recovers no rows.
Supersession at the same boundary is undecided.

## Ledger

<!-- protocol: structure -->

A `Ledger` carries its contract, disjoint regions covering the domain, the table's generation and
`publication_id`, and no history of accepted identities. It starts as one unbounded unknown region.
A `Region` has `low`, `high`, open-endpoint flags, `complete_as_of` and `deletions_as_of`: both
dates absent, or `d <= c`.

<!-- protocol: meaning -->

For a region `(c, d)`: every row the source held at `c` with a window value in the region is
present, and every row retained there was live at some instant in `[d, c]`. Equal dates mean the
region is the source at that instant. Unknown is not empty: nothing has been proven there.

<!-- protocol: accounting -->

The ledger stores only the points where the evidence changes, each with the evidence up to the
next point; regions are the view between points. Neighbouring regions with equal dates merge, and
values no delivery touched stay unknown. `Ledger.apply` does accounting only: it checks the
transition and updates dates, position and publication chain. Checking rows and looking up replays
belong to the receiver.

## Transitions

| Delivery and previous evidence | Covered dates after delivery at t | Table generation |
|---|---|---|
| Complete whole domain, any evidence <!-- protocol: full --> | `(t, t)` | Issued generation, or cleared. |
| Complete window, any evidence <!-- protocol: window --> | `(t, t)` | Unchanged. |
| First keyless append, unknown <!-- protocol: first-append --> | `(t, t)` | Issued if present, else unchanged. |
| Later keyless append, known <!-- protocol: later-append --> | `(t, t)` | Issued if present, else unchanged. |
| Keyed timestamp changes without deletions, `(c, d)` <!-- protocol: timestamp-upserts --> | `(t, d)` | Issued if present, else unchanged. |
| Keyed timestamp tombstones, `(c, d)`, `since <= d` <!-- protocol: timestamp-tombstones-covered --> | `(t, t)` | Issued if present, else unchanged. |
| Keyed timestamp tombstones, `(c, d)`, `since > d` <!-- protocol: timestamp-tombstones-uncovered --> | `(t, d)` | Issued if present, else unchanged. |
| Generation changes without deletions, `(c, d)` <!-- protocol: generation-upserts --> | `(t, d)` | Issued generation. |
| Generation tombstones, `(c, d)`, `c = d` <!-- protocol: generation-tombstones-exact --> | `(t, t)` | Issued generation. |
| Generation tombstones, `(c, d)`, `c > d` <!-- protocol: generation-tombstones-patched --> | `(t, d)` | Issued generation. |
| Keyed sequence changes <!-- protocol: sequence --> | as generation changes | `generation`, the read's last position. |

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

<!-- protocol-section: checks -->

<!-- protocol: plans -->

An `ApplyPlan` holds ordered `Action`s: `replace_all` replaces the table; `replace_window` removes
the rows in its closed window and earlier copies of candidate keys, then inserts the candidate.
`upsert` replaces published rows sharing a key with a live candidate row; `tombstone` deletes the
published rows the tombstones name; `append` inserts without matching. Keyless replacement matches
no keys. The core describes operations and never reads rows.

<!-- protocol: implied -->

A manifest implies facts about the rows it describes, and the receiver verifies them before
deciding. The read holds `rows` live rows and `tombstones` tombstones, and no deleted value is
null. Its columns hash to `schema_hash` and its bytes to `content_hash`. Every key, live or
tombstoned, is non-null and appears once. Every window value is non-null and, for a window
delivery, inside the window. With a `datetime` cursor, every live cursor value of a timestamp read
is in `(since, produced_at]`; with an `int` cursor, every one of a sequence read is in
`(since, generation]`; a tombstone's is null or in the same interval. The receiver passes its
results to the gate as `Check(name, passed, detail)` values; dogana names no check and runs none.

<!-- protocol: declarations -->

Every check a receiver runs compares the rows with a declaration, the manifest's or the contract's,
so an honest delivery never fails one. A rule about the source's data, such as a table that should
not halve or an amount that must be positive, declares nothing and is a business check, whose
result the receiver keeps for itself.

<!-- protocol: blocked -->

A window delivery's plan replaces its window and nothing else: a key already published in another
region is not moved, and a receiver refuses a delivery that carries one, since that region keeps its
claim. A row whose window value changes at the source can still leave a hole no check sees, so the
window column must never change.

<!-- protocol: evaluation -->

`Gate.evaluate(ledger, manifest, outcomes=(), *, now, source=None, joint=())` decides a delivery
from three core checks: `manifest` (the declarations agree with each other and the contract),
`receiver_time` and `transition` (the ledger admits the read). Then come the receiver's `outcomes`
and the `joint` checks of a group. Any failed check rejects. `Gate.preflight(ledger, manifest, now=now)`
returns the core checks alone; `Gate.reject(ledger, manifest, checks, now=now, source=None)`
records a failure the receiver found.

<!-- protocol: receipt -->

A `Receipt` records one decision: the manifest, the contract in force, the checks, `outcome`,
`received_at`, an optional `source`, `base_publication_id`, the publication identity, and
`ledger_identity`, the identity of the ledger the decision leaves; neither identity is ever
empty. `outcome` is `accepted`,
`rejected` or `replayed`; the `accepted` property is true for `accepted` and `replayed`, the
`replayed` property only for `replayed`. An
acceptance or a replay carries a `Transition`: the plan, and the regions over the coverage
afterwards. A rejection leaves ledger and publication as they were.

<!-- protocol: receipt-size -->

The ledger is never in a receipt: it is the result of applying the accepted receipts in order, and
`ledger_identity` checks each step. A receipt grows with its coverage, never with history, rows or
deleted keys. It is filed under the contract's table even when a rejected manifest names another,
and its identity covers its checks.

<!-- protocol: errors -->

`DoganaError` is the root of `InvalidContract` (with its subclass `SerializationError`),
`RejectedDelivery`, `PlanningError` and `InconsistentPublication`. Only declared protocol
violations become rejections; a receiver's operational failures are its own, and a failure after
its commit cannot revoke an acceptance.

## Claims and planning

<!-- protocol-section: claims -->

<!-- protocol: reading -->

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
   `until` as `produced_at` only if the read saw the source at `until`. Reads a sender
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

**Premise.** Every delivery comes from one consistent read of the source at `p`, stamped `p`, which
dogana cannot verify. One read is one snapshot of every table. READ COMMITTED, the default of
Postgres, Oracle and SQL Server, takes a new snapshot for each statement, so it is not one read. One
read is REPEATABLE READ, SERIALIZABLE or one exported snapshot shared by workers in Postgres; `START
TRANSACTION WITH CONSISTENT SNAPSHOT` in MySQL; a read-only transaction or queries `AS OF` one SCN
(system change number) in Oracle; SNAPSHOT isolation in SQL Server. Separate export jobs are not one
read, and one `produced_at` for them, such as one date in their file names, fabricates the instant.
The theorem needs no common transaction at the receiver: tables each exact at `p`, each read with
its ledger from one committed state, are consistent however they were published.

**Group.** `Group(contracts)` names tables read together, each once whatever its letter case. A
table belongs to one group, since deliveries accepted together replay together: a parent with several
children is one group. `Group.claims(ledgers, now, *, limit, windows=None)` returns every table's
regions together, each table clipped to its window.

**Delivery.** `Group.apply(ledgers, manifests, *, now, outcomes=None, sources=None)` takes one
manifest per table, with the receiver's checks of each table's rows, and decides them as one:

1. Each table is decided alone, as `Gate.evaluate` does.
2. The `together` check passes when every manifest has the same `produced_at` and every table is
   accepted alone. Its detail digests every manifest identity, so a receipt carrying it was accepted
   with exactly those deliveries.
3. That check joins every receipt. If any table is rejected, every table's receipt is a rejection;
   otherwise every receipt is an acceptance, and the receiver saves every table's rows and ledger in
   one commit, or nothing.

<!-- protocol: references -->

Rules the source keeps between its tables, such as every line naming an existing invoice, are not
checked: they need the rows, and the guarantee is `exact_as_of`, not a check. A receiver that wants
the tripwire joins the staged tables before committing.

**Reaching `(p, p)`.** `together` does not require the result to be exact; each table's shape
decides it over that table's coverage. These reach `(p, p)`: a complete delivery, a keyless append,
timestamp tombstones with `since` no later than the deletions date, and generation or sequence
tombstones when both dates were already equal. A table without changes delivers an empty read of
a tombstone changes shape at `p`. A reader of windows passes them to `Group.claims`.

**Replay.** A group replays whole: the receiver answers a repeated group with the receipts it
recorded when every manifest was accepted together, which the `together` digest identifies, after
verifying every declared hash against the bytes. Deliveries accepted alone, in another group or never
are a new decision, and the group needs a new read. Each table keeps its receipts and chain; a
group adds no wire kind.

<!-- protocol: atomicity -->

Committing every table at once serves availability, not the guarantee: without it a refused table
leaves the others ahead and the current tables have no common instant; with it, the instant moves in
one step.

<!-- protocol: planning -->

`plan_together(group, ledgers, at_least, *, windows=None)` returns nothing when the group is already
exact at an instant no earlier than `at_least`. Otherwise it returns one request per table with one
`until`, later than every table's evidence and no earlier than `at_least`, each the cheapest read
establishing `(until, until)` over its window. `until` is the earliest admissible boundary: the
extractor reads every table in one snapshot taken at or after it and stamps every manifest with its
instant.

## The receiver

<!-- protocol-section: receiver -->

<!-- protocol: sequence -->

The receiver is the code that loads a read's rows into its database. dogana decides; the receiver does, in
this order:

1. Looks the manifest's identity up among its receipts. An accepted one is a replay, answered with
   the original receipt once a declared `content_hash` matches the bytes; a repeated manifest over
   other bytes is refused, and a replay never reads the rows again.
2. Runs `Gate.preflight`; a failed check is recorded with `Gate.reject`, and nothing else happens.
3. Loads the rows where they can be checked without touching the published table, verifies them
   against the manifest, and passes the results to `Gate.evaluate` as `Check` values.
4. On acceptance, executes `receipt.transition.plan` on the published table and saves the new
   ledger, `ledger.follow(receipt)`, and the receipt in the same commit; on rejection, saves the
   receipt alone.

<!-- protocol-scope: obligations -->

What the receiver owes, for the dates to describe the rows:

1. Rows, ledger and receipt change in one atomic step: one transaction, or a table-format commit
   that carries the receipt or a reference to a durable copy. Writing the rows first and the ledger
   after is not atomic.
2. A decision is made against the ledger that describes the published rows. If the published
   chain head no longer equals `receipt.base_publication_id` when committing, the decision is stale
   and is made again; a retry never silently rebases an approved plan.
3. Accepted and rejected receipts are kept, accepted ones in publication order. The ledger is the
   fold of the accepted receipts, `Ledger.from_receipts`, and a stored ledger that differs from that
   fold is wrong.
4. Readers read rows and ledger from one committed state; separate reads can straddle a commit.

**Chain.** The publication chain is SHA-256 of UTF-8
`f"{previous}:{contract.identity}:{manifest.identity}"`, the first `previous` the string `None`;
`publication_id` is its head. A receiver that records the head its rows were published under can
tell a ledger behind its rows, or a publication from outside dogana. It cannot see a row edit that
keeps the head, a lost rejection or the loss of all evidence.

<!-- protocol: recovery -->

`Ledger.follow(receipt)` applies one accepted receipt and checks that it continues the chain and
leaves the ledger it names; `Ledger.from_receipts(contract, receipts)` folds the accepted ones in
order, ignoring rejections and refusing missing, reordered or foreign ones with
`InconsistentPublication`. Rebuilding the ledger recovers no rows: a receiver whose rows are in
doubt starts again with a complete whole-table delivery.

## Algebraic guarantees

<!-- protocol: laws -->

The same initial ledger and accepted history give the same ledger. A receiver's delivery is
idempotent by receipt lookup; `Ledger.apply` is not. Disjoint complete or timestamp windows commute on evidence
when both orders are admissible, never on the chain. Ledgers cannot be merged. Rejection applies per
manifest, not to a batch or a history.

## Contract evolution

<!-- protocol: compatibility -->

Once a table has history, its `table`, `key`, `window_column`, `deleted_column` and the types of
the key and window columns are fixed, and a receiver refuses a contract that changes them. The deleted
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

<!-- protocol: examples -->

`to_dict`/`from_dict` and `to_json`/`from_json` round-trip nested values. The
[contract](examples/contract.json), [manifest](examples/manifest.json),
[ledger](examples/ledger.json) and [receipt](examples/receipt.json) examples are executable,
[identity vectors](examples/identity-vectors.json) fix canonical forms and hashes for other
implementations, and [JSON Schemas](wire/README.md) cover all fourteen wire kinds.

## Boundaries

<!-- protocol: package -->

The package uses only the standard library and never reads rows or bytes: it decides from
declarations and the ledger. The [reference](reference.md) covers the command line and a receiver
written against the gate.

<!-- protocol: undecided -->

Undecided: a table namespace, finite string domains, more than one window column, receiver and
reader commands, supersession at the same boundary, and the compatibility policy.
