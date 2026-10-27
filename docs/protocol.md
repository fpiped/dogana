# The dogana protocol

Version 3 of the wire format, the `version: 3` every document carries; the package is versioned
separately. This page defines the wire values, transitions and publication obligations.
[Concepts](concepts.md) explains the same ideas from the beginning, with examples.

## Reading this page

- A row is **live** at an instant if the source holds it then. A **deleted** row was live once and
  is not any more. A **tombstone** is a delivered row that says "this key is deleted".
- `t` is a delivery's read boundary, `produced_at`. A region's two dates are written `(c, d)`:
  `c` is `complete_as_of`, `d` is `deletions_as_of`.
- `[a, b]` is the interval from `a` to `b` with both ends included; `(a, b]` leaves out `a`.
  Code writes a missing end of an interval as `None`, meaning "no limit on that side".
- A delivery's **coverage** is the part of the table it speaks about: the whole table, or a closed
  window of the window column.

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

A store reads a delivered file once, into a copy only it can reach, and hashes those bytes while it
copies them, so the rows it checks and publishes are the bytes `content_hash` names. A CSV file is
RFC 4180: comma separators, double quotes escaped by doubling, UTF-8, a header row, no comments and
no other dialect; a sender counting rows must parse that grammar. A `datetime` value carries its UTC
offset, in the file as in the manifest: a time without one names no instant and is refused. A file
that does not parse is the sender's failure, recorded as a rejection, never a storage error.

`produced_at` is the source snapshot or read boundary. The extractor guarantees all changes through
that boundary, including its endpoint. Download completion, arrival time and a scheduler's logical
date are not that boundary. Required receiver time `now` rejects `produced_at > now`. A late commit
with an earlier `updated_at` is invisible to the gate. Overlapping keyed reads mitigate bounded lag;
no lookback proves that extraction was complete.

## Contract and shapes

`Contract` declares `table`, `key`, named `shapes`, optional `window_column`, optional `cursor_column`,
optional `deleted_column`, `types`, optional `schema_hash`, `max_shrink_fraction`, `freshness` and
`start_at`. `types` maps column names to logical types `int`, `float`, `str`, `bool`, `date`,
`datetime`. It covers exactly the union of key, window, cursor and deleted columns, with no other
columns; it is empty only when none are bound. Keys and window bounds use ordered scalar types; a
cursor uses `datetime` or `int`; the deleted column uses `bool`.

The data contract defines the columns. The delivery contract references it by an opaque
`schema_hash`, keeping column ownership in one place. The receiver obtains this string from its
ODCS (Open Data Contract Standard) document, dbt model or hand-written column list. When present, the manifest must carry the
same hash and the store checks its candidate against it. When absent, no schema check is required,
including for accounting-only contracts, where `schema_hash` is not applicable.

For an ODCS schema object, traverse its properties in declared column order, map each property's
name and logical type to an ordered `[name, type]` pair, then compute SHA-256 of
`json.dumps(pairs, ensure_ascii=True, separators=(",", ":")).encode("ascii")`. Use dogana's six
logical type names, or `decimal(p,s)` for an exact decimal of precision `p` and scale `s`, resolving
physical or nested types explicitly in the receiver. Hash only
these pairs, not the ODCS document, metadata or wire JSON. The example store and file drop use
this recipe; another store must agree with its receiver on the opaque hash.

`freshness` defaults to seven days; choose cadence plus arrival slack. `max_shrink_fraction` defaults
to 0.5, lies between zero and one, and is disabled only by explicit `None`.

A window column exists exactly when a window shape exists. Regions divide this one column's ordered
domain, not the key space. Its value must never change for a key, deletion and reinsertion
included: a creation, order, document or event date, never an update timestamp. A row whose window
value moves from one region to another can be missing from both, when the two were proven at
different instants. A window delivery therefore refuses a key published outside its window, and only
a delivery covering the whole table moves one. "Rows updated in the last 30 days" is therefore not a window: it is a changes delivery
whose `since` is 30 days back.
Keys are unique and non-null. Key and window values are strings, integers, dates or UTC datetimes;
`types` and manifest bounds must agree on type. Normalize aware datetimes to UTC before key
equality or interval arithmetic, including DST folds. Strings use code-point order, without Unicode
normalization or locale collation. Equal-endpoint windows denote one partition. String adjacency uses successor `s + "\x00"` and minimum `""`. Date and datetime domains respect
their representable minima and maxima; empty exterior intervals are discarded. A set of delivered
plant codes does not close the rest of the string domain; whole-table evidence requires a full read.

An empty `key` means rows have no identity. Complete shapes replace their coverage wholesale.
Keyless rows form a multiset: equal events retain separate occurrences.
Keyless changed-only shapes must be whole-domain timestamp appends with no deletion signal and a
`start_at` bounding retained history: the events after it, never one at it. This declares that the
source only appends and never deletes or revises retained events. Tombstones, generations and sequences require keys.
An optional `cursor_column` names a column declared as `datetime` or `int` in `types`. A `datetime`
cursor checks timestamp reads against `(since, produced_at]`; an `int` cursor checks sequence reads
against `(since, generation]`; a cursor column must check one of the contract's changed-only shapes. Without it,
the gate cannot check individual rows against the declared extraction interval.

An optional `deleted_column` names a column declared as `bool` in `types`. It exists exactly when a
tombstone shape exists, and it is a column of its own, not a key, window or cursor column. In a
delivered file, a row whose deleted column is true is a tombstone: it names a deleted key, and its
other values are not published. They may be null; where present they must still have their
column's type. A row whose deleted column is false is live; a null is neither, and the
row count refuses it. A file may omit the column, and then every row is live. The column belongs to
the delivery, not to the table: it is never published and the column hash leaves it out. Only a
tombstone shape may deliver a true value. Because keys travel in the file, only `content_hash` lets a
manifest identify which keys a file deletes: without it, a second file with the same manifest is a
replay of the first and is never read. Declare it for tombstone shapes; `dogana manifest` and the
file drop always do. A source that soft-deletes (it keeps the row and sets a
flag) can deliver that flag here, as long as the soft delete also moves the row's update time and
the source never removes rows physically; otherwise the honest shape has deletions `none`. Every
date then speaks about the source's live rows, those whose flag is false, not about its physical
table.

A `Shape` has four semantic properties:

| Property | Values | Meaning |
|---|---|---|
| coverage | `all`, `window` | The whole table, or a closed window of the window column. |
| completeness | `complete`, `changed_only` | All current rows in coverage, or all changes since a checkpoint. |
| deletions | `absence`, `tombstone`, `none` | A missing row is deleted, a tombstone row names a deleted key, or nothing is said about deletions. |
| checkpoint | `generation`, `sequence`, `timestamp`, `none` | Opaque cursor, ordered integer position, source time, or no checkpoint. |

Complete shapes require `absence` and checkpoint `none`. Changed-only shapes require `timestamp`,
`generation` or `sequence` and deletions `none` or `tombstone`. Generations and sequences are
table-wide positions, so window feeds of either are forbidden, and a table holds one kind of
position. These rules admit ten semantic shapes: complete over the whole table or a window, and
changed-only over the whole table (three checkpoints) or a window (timestamp only), each with or
without tombstones.

Shapes also carry a nonnegative `cost` (default one): how expensive it is to ask the source for
that kind of file, relative to the other shapes. Only the planner reads it, to choose the cheapest
shape that proves a requirement. Shapes also carry an optional positive `window_size` and
optional `lookback` durations. `window_size` is a planning hint, not a push-window width constraint.
Rolling durations require date or datetime windows. DATE widths are whole days including both ends:
thirty days ending October 19 start September 20. Datetime windows include both ends of the elapsed UTC duration.
Timestamp resolution is one microsecond; DATE planning uses UTC calendar dates. Rolling planning
uses the window column's declared type in `types`.
`lookback` is nonnegative and allowed only on timestamp shapes in keyed contracts; it extends a planned read back
from the weakest covered completeness boundary. Keyless appends cannot overlap.

## Choosing a shape

| Source behavior | Shape | What it cannot express |
|---|---|---|
| Exhaustive table export | `Shape.full()` | Completeness inferred from a received count alone. |
| Exhaustive rolling window | `Shape.window(size=...)` | Mutable window membership or more than one window column. |
| One complete partition per file | Complete singleton window | A finite registered domain; space outside delivered partitions remains unknown. |
| Every changed row since a timestamp | `Shape.changes(checkpoint="timestamp", deletions="none")` | Deletions or repair of a missed full export without another full export. |
| Rows updated in the last N days, by update time | `Shape.changes()` with `since` N days back | Deletions; it is not a window. |
| Every changed row and deleted key since a timestamp, including a soft delete that moves the update time | `Shape.changes(checkpoint="timestamp", deletions="tombstone")` | A missing base; deletion gaps heal only when the read reaches back through them. |
| Continuous opaque-cursor feed with deleted keys | `Shape.feed()` | Window-local cursors, missing generations or repair of an unknown base. |
| Every change after a commit-ordered position (log sequence number, CDC or Kafka offset) | `Shape.changes(checkpoint="sequence")` or `Shape.feed(checkpoint="sequence")` | Positions assigned before commit, such as auto-increment ids under concurrent writers; updates that do not advance the position; positions beyond 2^53. |
| Immutable events retained from a declared start | `Shape.append()` | Updates, deletion or overlapping keyless intervals. |
| CDC stream in offset batches | `Shape.feed(checkpoint="sequence")` after a full snapshot issuing its offset | Missing offset ranges or source completeness inferred from offsets alone. |
| Immutable event stream in arrival-order intervals | `Shape.append()` with `start_at` | Event-time completeness or deduplication of a re-emitted event in a new batch. |

Shape constructors take keywords; `Contract` and `Manifest` require keywords after `shapes` and
`produced_at`, respectively. `Contract.manifest(shape, produced_at, ...)` supplies the table and
the contract's optional schema hash. `Shape.window_ending_at` centralizes inclusive DATE and elapsed datetime window arithmetic.

## Manifest

`Manifest(table, shape, produced_at, *, window, since, generation, rows, schema_hash, tombstones,
content_hash)` describes one read. `window` is required exactly for window coverage; `since` exactly
for changed-only shapes. Timestamp checkpoints are aware datetimes; generation checkpoints and
issued generations are nonempty strings. A generation feed must issue its next generation.
Sequence reads carry integer `since` and `generation` and cover the positions `(since, generation]`;
`generation` is at least `since`, and an empty interval carries no rows. Like `produced_at`,
`generation` is the extractor's promise that every change up to it is present, so positions must
follow commit order. An auto-increment id is assigned at insert: a transaction can commit a lower id
after a read has seen a higher one, and the next read starts past it. Read such ids only up to a
position below every open transaction, or reread with overlap, which keyed sequence reads admit.
`produced_at` must be earlier than the commit of the first change after `generation`: a consumer
behind the head uses the commit time of the last change it read, never the time it runs. Sequence
positions need a key; keyless tables append by timestamp. A delivery that issues a position issues
the table's kind: an integer when the contract has sequence shapes, else a string.
A complete whole-domain delivery may issue a generation; a complete window carries none.

Changed-only delivery collapses changes to one final row per key at its boundary: the live row, or a
tombstone row when the key ends deleted. `rows` counts the live rows and `tombstones` the tombstone
rows; together they are every row of the file. Keys travel in the rows, never in the manifest, so a
manifest stays the same size whatever the volume. Tombstones require a tombstone shape. A
zero-length interval contains no rows and no tombstones.

Timestamp deliveries with `cursor_column` use `(since, produced_at]`, including for keyed feeds.
Without that column, keyed extraction may include `since`; overlapping keyed reads are admissible.
Keyless appends always use `(since, produced_at]`, without overlap. `content_hash` is an optional
lowercase hex SHA-256 of the delivered bytes, supplied by sender or receiver. It participates in
identity, and the store verifies it against the bytes it read; a source without bytes cannot carry
one. Without a digest, manifest identity does not identify the content, so the file drop fills in
the digest of what it received.

## Corrections

A corrected file is a new delivery by content hash and needs a later truthful read boundary.
Invalidating an accepted delivery requires rebuilding rows and ledger from the receipts before it,
then redelivering the corrected file and subsequent reads; ledger reconstruction alone cannot
recover rows. Supersession at the same boundary remains an open decision.

## Ledger and transitions

On the wire a `Ledger` carries its contract, disjoint regions covering the domain, table generation
and `publication_id`. Initially one unbounded region is unknown. `Region` has `low`, `high`, open-endpoint
flags, `complete_as_of` and `deletions_as_of`. `None` endpoints are unbounded. Evidence dates are absent
together, or `deletions_as_of <= complete_as_of`. No accepted-identity history is embedded in the ledger.

For a region with dates `(c, d)`, every row the source held at instant `c`, with a window value in
the region, is present: nothing is missing. Every row retained there was still live at the source at
some instant in `[d, c]`: nothing deleted before `d` is still here, while a row deleted after `d`
may be. Equal dates mean the region equals the source at that instant. Unknown is different from a
known empty region: unknown means no delivery has proven anything there.

Internally the ledger stores only the points where the evidence changes (cuts), each with the
evidence that holds from it to the next one. `Region` is the view of the pieces between cuts, used
on the wire and in claims, including endpoint flags. Neighbouring pieces with equal evidence merge;
space no delivery touched stays unknown. `Ledger.apply` performs accounting only, without row
certification or replay lookup. Store delivery owns replay idempotency.

## Transitions

| Delivery and previous evidence | Covered dates after delivery at t | Table generation |
|---|---|---|
| Complete whole domain, any evidence | `(t, t)` | Issued generation, or cleared. |
| Complete window, any evidence | `(t, t)` | Unchanged. |
| First keyless append, unknown | `(t, t)` | Issued generation if present, otherwise unchanged. |
| Later keyless append, known | `(t, t)` | Issued generation if present, otherwise unchanged. |
| Keyed timestamp changes without deletions, `(c, d)` | `(t, d)` | Issued generation if present, otherwise unchanged. |
| Keyed timestamp tombstones, `(c, d)`, `since <= d` | `(t, t)` | Issued generation if present, otherwise unchanged. |
| Keyed timestamp tombstones, `(c, d)`, `since > d` | `(t, d)` | Issued generation if present, otherwise unchanged. |
| Generation changes without deletions, `(c, d)` | `(t, d)` | Issued generation. |
| Generation tombstones, `(c, d)`, `c = d` | `(t, t)` | Issued generation. |
| Generation tombstones, `(c, d)`, `c > d` | `(t, d)` | Issued generation. |
| Keyed sequence changes, as generation changes | as generation changes | `generation`, the read's last position. |

- Manifest fields must match its admitted shape and contract, including bounds and key types.
- Every affected known region requires `t > complete_as_of`; a late full cannot overwrite a newer window.
- Changed-only coverage must be known throughout, except the first keyless append on an untouched ledger.
- The first keyless append requires `since = start_at`; later appends require `since = complete_as_of`
  in every affected region, so a newer complete window requires a full reset before global append.
- Timestamp changes require `since <= t` and `since <= complete_as_of` everywhere covered.
- Generation changes require `since` equal to the held table generation; a broken chain needs a full reset.
- Keyed sequence reads require `since <= position <= generation` for the held integer position: they may
  reread, never skip or move back. A missing or opaque position needs a complete delivery issuing one.
- The gate additionally requires `t <= receiver now`; accounting alone has no receiver clock.
- Each accepted delivery extends the publication chain with the previous head, contract and manifest
  identities; disjoint windows commute only on evidence, never on this ordered chain.

## Plans, checks and receipts

`ApplyPlan` contains ordered `Action` values. `replace_all` replaces the table. `replace_window`
removes rows in its closed window, removes earlier copies of candidate keys and inserts the candidate.
`upsert` replaces the published rows that share a key with a live candidate row; `tombstone` deletes
the published rows whose keys the candidate's tombstone rows name; `append` inserts without
matching. Keyless replacement does not match keys. The core describes operations but never reads rows.

`Gate.requirements(contract, manifest)` names required row checks. The store returns exactly one
`Check(name, passed, detail)` for each; missing, duplicate or unexpected outcomes reject publication.

| Check | Predicate |
|---|---|
| row_count | The candidate's live rows equal `manifest.rows`, its tombstone rows equal `manifest.tombstones`, and no deleted column is null. |
| schema_hash | Candidate column hash and manifest hash equal `contract.schema_hash`; omitted when the contract hash is absent. |
| rows_in_window | Every live window value is non-null and inside the declared closed window, and so is the window value of every published row that shares a key with a delivered row, live or tombstone. |
| window_present | Every live window-column value is non-null, so each row falls in a region. |
| key_integrity | Every key, live or tombstoned, is non-null and appears once: a key cannot be both live and deleted. |
| shrink_tolerance | Complete delivery removes no more than the allowed fraction of previously published rows in coverage. |
| rows_in_interval | With a `datetime` cursor and a timestamp read, every live cursor value is non-null and in `(since, produced_at]`; with an `int` cursor and a sequence read, in `(since, generation]`. A tombstone's cursor value may be null; when present it must be in the same interval. |
| content_hash | With `manifest.content_hash`, the SHA-256 of the bytes the store read equals it. |

A key published in another region is refused rather than moved, since that region keeps its claim.
A row that leaves a window in the source can still leave a hole no candidate check sees, hence
source immutability remains necessary. Checks blocked by an
earlier failure return `passed=False, detail="not evaluated"`, not an unevaluated predicate's result.
Business row rules from quality tools run outside this fixed check set; the caller retains their outcomes.

`Gate.evaluate(ledger, manifest, outcomes, now=now, source=None)` requires receiver `now`. Core checks
validate structure, source time, base coverage, timestamp/generation continuity and monotonic time.
`Gate.preflight(ledger, manifest, now=now)` returns those core checks without row outcomes;
`Gate.reject(ledger, manifest, checks, now=now, source=None)` records a declared failure.
A `Receipt` records one decision: the manifest, the contract in force, the checks, `outcome`
(`accepted`, `rejected` or `replayed`), `received_at`, optional `source`, `base_publication_id`, the
publication identity and `ledger_identity`, the identity of the ledger the decision leaves. Acceptance
and replay carry a `Transition`: the plan, and the ledger's regions over the delivery's coverage
afterwards. A rejection leaves the previous ledger and publication. The ledger itself is never in a
receipt: it is the fold of the accepted receipts, and `ledger_identity` lets the fold check each step.
`accepted` and `replayed` are read from `outcome`. Receipts
are indexed by the receiver contract's table even if the rejected manifest names another table.
Their identity also covers check outcomes. A receipt grows with the regions its delivery covers, never
with the table's history, the delivered rows or the deleted keys.

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
| patched | `complete_as_of` newer than `deletions_as_of` and within the freshness limit. |
| expired | `complete_as_of` older than the freshness limit, whatever `deletions_as_of` says. |

State priority is unknown, expired, patched, complete. Aggregate dates are separate minima, absent
if any region is unknown; equal aggregate dates do not imply that every region is exact. The deletion date
remains visible even when recent patches hide old deletion knowledge.

`Claims.combine` takes those minima across tables. These are lower bounds on input evidence dates,
not completeness or deletion statements about one common snapshot or a joined result. Regions at
different dates may form a table exact at no instant. Without a common instant the minima give no
row guarantee for any query over several tables or regions, monotone ones included: a later read can
miss a row deleted after an earlier one and carry a later version of a row. That instant is
`exact_as_of`, in [Tables together](#tables-together). Negation (`NOT EXISTS`,
anti-joins, difference) swaps the completeness and deletion requirements for the negated input. With
exact items and complete-but-patched movements at the same boundary, "items with no movement" is
valid but possibly incomplete: a retained deleted movement hides an item. Exact aggregates need
both guarantees; `COUNT` and `SUM` as exact values are not ordinary monotone row queries.

`Requirement` names a table, minimum `complete_as_of`, optional minimum `deletions_as_of` and optional window.
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

## Tables together

`Claims.exact_as_of` is the instant `p` at which every region of the claims has the dates `(p, p)`,
or `None` when there is none. Table dates are minima, so equal table dates do not imply it: a window
proven later than the rest of its table gives equal minima and no exact instant. On one table's
claims `exact_as_of` states that the table equals the source at `p`; on claims combined across
tables, that every one of them does. Freshness does not affect it; `state` still reports an expired
region.

If every region a query reads, in every table it reads, equals the source at the same instant `p`,
the query answers as the source would have at `p`: filters, joins, unions, negation and exact
aggregates alike, provided both sides compare and compute the same way (collation, null handling;
a floating-point sum depends on the order of its terms), and every rule the source keeps between its tables, such as each line naming an
order, holds between the published tables. The premise, which dogana cannot verify, is that every
file comes from one consistent read of the source at `p`, stamped with that read's instant. One read
means one snapshot covering every table. A transaction at READ COMMITTED, the default of Postgres,
Oracle and SQL Server, takes a new snapshot for each statement and is not one read. Use REPEATABLE
READ, SERIALIZABLE or one exported snapshot shared by parallel workers in Postgres, `START
TRANSACTION WITH CONSISTENT SNAPSHOT` in MySQL, a read-only transaction or queries `AS OF` one SCN (system change number) in
Oracle, SNAPSHOT isolation in SQL Server. Separate export jobs are not one read, and stamping them
with one `produced_at`, for example through one date in their filenames, fabricates the instant.
No common store transaction is needed for the guarantee: two tables each read with its ledger under
one publication identity (obligation 6), each exact at `p`, are consistent however they were
published.

`Group(contracts, references=())` names tables read together, each once whatever the letter case,
since many stores resolve table names regardless of case. Give each table one group: a parent with
several children forms one group holding all of them, because a table accepted in one group does not
replay in another. `Group.claims(ledgers, now, windows=None)` combines the tables' claims, each
clipped to an optional window; its `exact_as_of` is the group's common instant. `Reference(table,
columns, parent)` states that every live row of `table` whose `columns` are all non-null names, in
that order, the key of a row of `parent`; it has one column per key column of the parent, and both
tables belong to the group.

`deliver_together(store, group, deliveries, now)` takes one `(source, manifest)` per table of the
group and decides them as one delivery:

1. Each table runs the steps of a single delivery up to its own decision: replay lookup, preflight,
   `hold` and `check`.
2. The `together` check passes when every manifest has the same `produced_at` and every table is
   accepted on its own. Its detail names the delivery: a digest of every table's manifest identity
   and the group's references, so a receipt carrying it was accepted with exactly those files.
3. With references and a passed `together` check, the store answers one `references` check on the
   rows that would result from every table's plan. `Group.unchecked` names the references the
   delivery cannot test: a reference is tested only when the child's delivered coverage ends exact
   at `p` and the parent is complete as of `p` over its whole domain. Then every child row in that
   coverage was live at `p` and every row it can name is present, so a row whose reference columns
   are all non-null and name no parent row proves the premise false: the files were not one read at
   `p`, or the source does not keep the rule. An untested reference passes, and the check's detail
   says why, so an honest delivery is never refused for a reference. Values compare as values, int
   and float alike; a reference column of another type than its parent key column is a declaration
   error, and fails the check as soon as one row names a parent.
4. Both checks join every table's receipt, through `Gate.evaluate(..., joint=checks)`. If any
   receipt is a rejection, every table is rejected and records its refusal; otherwise
   `publish_together` publishes every table's rows, ledger and receipt in one atomic step.

Each table keeps its own receipts, publication chain and reconstruction, and a group adds no wire
kind. A group replays whole or is refused whole: when every manifest was accepted in one delivery of
these files under these references, every receipt replays, provided every declared content hash
still matches the bytes; otherwise, when any was accepted alone, in another delivery or under other
references, every table is refused, and the group needs a new read. A failure after its own commit
returns the committed receipts. A failed delivery whose tables another delivery accepted meanwhile is
decided the same way, as a replay or a refusal, so the outcome does not depend on timing; when the
other delivery lands while refusals are being recorded, the call raises `StoreError`, and a retry
decides it. Only a group publication that committed some of its tables and not others raises
`InconsistentPublication`. Two concurrent deliveries with equal manifests and receiver times
produce one receipt, so the one that published nothing may also be answered `accepted`.

The `together` check does not require the result to be exact; the shapes decide that, over what
each delivery covers: a full delivery everywhere, a window delivery inside its window, so pass the
windows to `Group.claims`. A covered region reaches `(p, p)` with a complete delivery or a keyless
append, with timestamp tombstones whose `since` is no later than its deletion date, or with
generation or sequence tombstones when its two dates were equal. A table without changes still
delivers an empty changes file at `p`.

Atomic publication is for availability, not for the guarantee: without it, the current tables have
no common instant between two publications, and a refused table leaves the others ahead. With it,
the common instant moves from one boundary to the next in one step, and a refused delivery leaves
every table where it was.

A reference therefore needs a child delivered with its deletions and a parent delivered over its
whole domain. A child delivered without tombstones, or a parent delivered by windows, leaves the
reference untested: the group can be accepted with orphan rows, and its `exact_as_of` tells the
reader that it is not one instant. A tested reference covers every child row in the coverage, not
only the delivered ones, so the store scans the child and joins it with the parent on each delivery.

`plan_together(group, ledgers, at_least, windows=None)` returns no request when the group is already
exact at an instant no earlier than `at_least`. Otherwise it returns one request per table, all with
the same `until`, later than every table's evidence and no earlier than `at_least`, each the
cheapest read that establishes `(until, until)` over its window. `until` is the earliest admissible
boundary: the extractor reads every table in one consistent snapshot taken at or after it, and
stamps every manifest with that snapshot's instant.

`TogetherStore` extends `Store` with `check_references(group, candidates)` and
`publish_together(group, entries)`, where `candidates` maps each table to its checked candidate and
`entries` pairs one candidate per table of the group with its accepted receipt. The store must:

1. answer the references check on the rows the candidates would produce, without changing published
   rows, only for candidates that passed their own checks, and only for the references
   `Group.unchecked` leaves to test;
2. verify every receipt against its own checks, the `together` check recomputed from them and the
   group's references, and, when the group has references, the references check it answered for
   those references on exactly those candidates after their last check; without that answer, refuse
   to publish;
3. publish every entry, or none, in one atomic step, under the six obligations of a single
   publication.

## Store protocol and publication

`Store` and `Extractor` are structural `typing.Protocol` interfaces. An extractor implements
`extract(contract, request)`, returning `(manifest, source)`. The source representation belongs to
the store. `dogana.deliver(store, contract, source, manifest, now)` owns the shared sequence:

1. `load_ledger(contract)` obtains the current evidence. `accepted_receipt(manifest.identity)`
   returns a stored acceptance on replay, with `outcome="replayed"`, original identity and receiver time.
   A manifest with `content_hash` replays only once `digest(source)` equals it; different bytes are
   refused. A replay compares bytes, never the rows against today's contract.
2. Preflight the declaration and transition. `publish(None, receipt)` records a rejection against
   its expected parent without changing published rows or evidence.
3. `hold(source, manifest, contract)` stages a candidate; `check(candidate, contract, manifest,
   requirements)` answers the fixed row checks against one base publication.
4. `publish(candidate, receipt)` compares that base and atomically executes an accepted plan with
   its rows and evidence, or records rejection; `abort(candidate)` releases staged work after failure.

`receipts(table=None)` returns persistence order; `digest(source)` returns the SHA-256 of the bytes a
source holds, or `None` for rows without bytes. These eight methods are the required `Store` port.
An accepted publication requires a held and checked candidate. Rejected publication also compares
and sets the expected parent, and refuses a receipt that cannot decode before writing the log.
`publication_stamp(table)` and `repair(contract)` form the optional stamped-ledger tier;
`check_references` and `publish_together` form the optional [together tier](#tables-together).
Ordinary read methods and `store.deliver` are conveniences, not required port methods.

The store must satisfy six obligations:

1. Check candidate rows before published rows change; execute only an accepted receipt's plan.
2. Publish rows, ledger, accepted receipt and any row stamp as one atomic step: either one transaction,
   or a table-format commit carrying the full receipt or a reference to an already durable receipt.
   Ordered writes plus idempotent plans are not atomic. Accounting alone offers no publication guarantee.
3. Compare and set against `receipt.base_publication_id`. Refuse a changed head, including cached
   checks against a stale base. A commit retry may not silently rebase an already approved plan.
4. Persist accepted and rejected receipts; retain accepted receipts in publication order for recovery.
5. Replay accepted manifest identity by returning its original receipt, with no second publication,
   after verifying a declared `content_hash` against the delivered bytes.
6. Read rows and claims under one publication identity; separate current reads can straddle a commit.

The publication chain is SHA-256 of UTF-8 `f"{previous}:{contract.identity}:{manifest.identity}"`;
the initial `previous` is the literal string `None`. It includes the full contract identity.
The stamp associates rows with that head; it is not a row checksum. Comparing it with the ledger
finds a ledger behind rows or external publication that changed the stamp. It cannot detect silent
row edits preserving the stamp, lost rejected receipts or joint loss of all evidence. Surviving
receipts or stamps disprove a fresh start when a ledger is missing. Stores must fail closed.

`Ledger.follow(receipt)` applies one accepted receipt's manifest and checks the result against the
receipt's publication and ledger identities; `Ledger.from_receipts(contract, receipts)` folds accepted
receipts in publication order this way, ignoring rejections. Missing, reordered or foreign acceptances refuse
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
publication chain. Two ledgers cannot be merged: order, continuity and row replacement decide
what is admissible. Rejection and shrink policy apply per
manifest, not to an arbitrary batch or cumulative history.

## Contract evolution

History binds `table`, `key`, `window_column` and the types of key/window columns. Stores refuse
changes to these bindings. Other fields may evolve, including freshness, shrink policy, costs,
window sizes, lookback, admitted shapes, cursor columns and the data-contract hash. Each accepted receipt retains
the contract used for that decision. The full contract, including policy with normalized numeric
spelling, participates in the chain; only binding differences cause history refusal.
`Ledger.with_contract(contract)` verifies compatibility and preserves regions, generation and head.
Reconstruction uses each accepted receipt's historical contract, then the requested compatible contract.

## Versioned JSON

Structured wire values have `version: 3` and `kind` equal to the public class name. Earlier versions
are refused: version 1 manifests (dogana 0.1) listed keys, and version 2 receipts (dogana 0.2 and
0.3) carried whole ledgers. Field names match
constructor arguments; every field, including defaults, is present. Tuples encode as arrays, enums
as strings, mappings as objects. Missing or unknown fields, unsupported versions/kinds, invalid
wire types, overflowing tags and naive datetimes raise `SerializationError` before normalization.

Dates use `{"$date":"2026-10-18"}`, UTC datetimes
`{"$datetime":"2026-10-18T00:00:00+00:00"}`, durations `{"$seconds":604800}`. Canonical wire JSON
uses sorted keys, compact separators and ASCII escapes. Identity is SHA-256 of [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)
(JSON Canonicalization Scheme) applied to the identity dictionary: recursively omit `version` and
fields equal to defaults; normalize datetimes to UTC `+00:00`. A replay's `outcome` is normalized to `accepted` for identity, retaining its
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
[JSON Schema 2020-12 documents](wire/README.md) cover all fifteen wire kinds; semantic rules remain in this protocol.

## Boundaries

The package uses only Python's standard library, beside pytest for the conformance suite, and parses
no data file: rows are read by a store, and the repository's example store sits outside the package.
The [reference guide](reference.md) describes concrete transports and the six-call accounting path.
The conformance suite exercises publication obligations against store implementations; passing it
is evidence for those scenarios, not a proof of arbitrary storage or source correctness.

Not yet decided: a table namespace, finite string domains, more than one window column, receiver and
reader commands, supersession at the same boundary, and the compatibility policy.
