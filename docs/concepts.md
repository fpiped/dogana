# Concepts

This page explains dogana from the beginning, one idea at a time, each with an example. The
[protocol](protocol.md) is the precise specification of the same ideas.

## The problem

A source system (an ERP, a SaaS API, a database you do not own) has a table that changes over
time. A file it sends you is a photograph of part of that table, taken at one instant: the
**read boundary**, `produced_at`. What the photograph proves depends on how it was taken:

- a full dump proves that every row is present, and that every row missing from it was deleted;
- "every order dated in the last 30 days" proves the same, but only for those 30 days of orders;
- "every row changed since yesterday" proves that nothing new or updated is missing, and says
  nothing about rows deleted since yesterday.

Once the files are loaded into one table, a row is just a row: nobody remembers which file proved
what. dogana keeps that record next to the rows, and rejects any file that would make it lie.

## Two dates

For every part of the table, dogana keeps two instants.

- **complete as of** (`c`): every row the source had at instant `c` is here. Nothing is missing.
- **deletions as of** (`d`): no row deleted at the source before `d` is still here. A row deleted
  after `d` may still be here.

`d` is never later than `c`. When they are equal, that part of the table is exactly the source at
that instant. When `c` is later, the part is **patched**: it has every row, and possibly some rows
the source has deleted since `d`.

A full dump on Sunday gives (Sunday, Sunday). Monday's changes, without deletions, bring every new
and updated row, so `c` moves to Monday; they say nothing about deletions, so `d` stays on Sunday.
The rule behind every decision: **a delivery never raises a date above what it proves.**

## Where the dates live: the window column

The dates are not kept per row. They are kept per range of one column, the **window column**, that
the contract names. Take a table `orders` with `order_date` as its window column. Draw
`order_date` as a line, from the oldest possible date to the newest, and cut it into pieces; each
piece carries its two dates. After Sunday's full dump (Oct 25) and, on Tuesday (Oct 27), a file of
every order dated Oct 25 to Oct 27:

| Order dates | Complete as of | Deletions as of |
|---|---|---|
| up to Oct 24 | Sunday | Sunday |
| Oct 25 to Oct 27 | Tuesday | Tuesday |
| from Oct 28 | Sunday | Sunday |

Orders dated 25 to 27 are proven as of Tuesday, because Tuesday's file covered them. Every other
order is proven only as of Sunday: Tuesday's file said nothing about it. Each piece is a
**region**. The pieces touch end to end, so every possible `order_date` falls in exactly one.
Before any delivery there is one piece with no dates, **unknown**. Unknown is not empty: empty
would mean "there are no orders here", unknown means "nobody has proven anything here". A table
without a window column has one piece, the whole table.

**The window column's value must never change for a row.** A creation, order, invoice or event
date works; an update timestamp never does. Here is why. Suppose `order_date` were `updated_at`:

1. Oct 1, full dump: order 7 has `updated_at = Sep 15` and is in the table.
2. Oct 20: order 7 is edited at the source, and its `updated_at` becomes Oct 20.
3. Oct 25, a file of the rows dated Sep 1 to Sep 30: order 7 is no longer in that range, so it is
   not in the file, and replacing the range deletes it.

Now the piece Sep 1 to Sep 30 is proven as of Oct 25 and the rest as of Oct 1, so the table as a
whole claims "complete as of Oct 1". Order 7 existed on Oct 1, and it is gone: it moved from a
piece proven later to a piece proven earlier, and neither file carried it. A single file cannot
show that a value changed, so dogana cannot detect this; the rule is the receiver's to keep. A source that exports "rows updated in the last 30 days" sends changes, not
a window: see the next section.

## What a file can prove: shapes

A **shape** is a kind of file the receiver accepts, described by four answers:

| Question | Answers |
|---|---|
| Which part of the table? (coverage) | the whole table (`all`), or a range of the window column with both ends included (`window`) |
| Every current row, or only what changed? (completeness) | `complete`, or `changed_only` since a checkpoint |
| How are deletions told? (deletions) | a missing row is deleted (`absence`, complete files only), the file marks deleted keys (`tombstone`), or nothing is said (`none`) |
| Changed since what? (checkpoint) | nothing (`none`, complete files), a source time (`timestamp`), a token the source hands out with each read, such as an API's sync token (`generation`), or an ordered integer such as a log position (`sequence`) |

The constructors cover the common combinations:

| Source behaviour | Shape | What it proves |
|---|---|---|
| A full export | `Shape.full()` | both dates, everywhere |
| Every order dated in the last 30 days | `Shape.window(size=timedelta(days=30))` | both dates, for those dates |
| One complete file per plant or per day | a window whose two ends are equal | both dates for that value; values never delivered stay unknown |
| Rows with `updated_at` after the last read | `Shape.changes()` | complete as of this read; the deletions date does not move |
| The same, plus the keys deleted since | `Shape.changes(deletions="tombstone")` | both dates, when the read starts no later than the current deletions date |
| A change feed with a sync token | `Shape.feed()` | both dates, as long as each read starts from the token the previous read handed out |
| CDC batches by log position or offset | `Shape.feed(checkpoint="sequence")` after a full snapshot | the same, each batch starting where the last one ended |
| Immutable events with no key | `Shape.append()`, with `start_at` the instant history starts | both dates, as long as each read starts exactly where the last one ended |

Each shape also has a **cost**: how expensive it is to ask the source for that kind of file, as a
number relative to the other shapes (a full dump at 10 is ten times a changes read at 1). Only the
planner uses it, to choose the cheapest file that proves what a reader needs.

## Deletions

The hard part is always deletions, and only **physical** ones: the row disappears from the source,
so no later export can contain it. A **soft delete** keeps the row and sets a flag
(`is_deleted = true`): it is an update, and a changes export carries it like any other.

A file of a tombstone shape tells deletions in its own rows. The contract names a boolean
**deleted column**; a row whose deleted column is true is a **tombstone**: its key is deleted, and
its other values are not published, so they can be left empty.

```text
id,order_date,amount,deleted
7,2026-10-26,120,false
9,,,true
```

This file updates order 7 and deletes order 9. Its manifest declares `rows=1` (live rows) and
`tombstones=1`, never the keys themselves, so it stays small whatever the volume. The deleted
column is never published, and a file may leave it out when it deletes nothing.

```python
orders = Contract(
    "orders",
    key=("id",),
    shapes={"full": Shape.full(), "changes": Shape.changes(deletions="tombstone")},
    types={"id": "int", "deleted": "bool"},
    deleted_column="deleted",
)
```

A soft-deleting source can declare a tombstone shape, delivering its flag as the deleted column or
keeping flagged rows as data with no tombstones, only when three things hold:

1. the soft delete also moves `updated_at`, otherwise the changes read never selects the flagged
   row and the delete is never delivered;
2. nothing ever deletes physically: archiving jobs, retention purges, legal erasure, cascades on
   child tables;
3. the flag lives on the table being loaded, not in another table.

If any of them fails, the honest shape says `deletions="none"`, and a periodic full dump heals the
gap. Debezium's new-record-state transform can rewrite deletes as rows with a `__deleted` field;
that field, as a boolean, is a deleted column.

## A week, step by step

The contract: table `orders`, key `id`, window column `order_date`, and three shapes: `full`
(cost 10), `last3`, every order dated in the last 3 days (cost 2), and `changes`, every order
created or changed since a given moment plus its tombstones (cost 1).

**Sunday, Oct 25: a full file.** Every order date is proven as of Sunday.

| Order dates | Complete as of | Deletions as of |
|---|---|---|
| all | Sunday | Sunday |

**Tuesday, Oct 27: a `last3` file for order dates Oct 25 to Oct 27.** Those three days move to
Tuesday; everything else stays at Sunday, the table shown above.

**Wednesday, Oct 28: a `changes` file with everything since Sunday.** It covers every order date,
and its tombstones cover every deletion since Sunday, so the whole table is proven as of Wednesday
and the three pieces merge back into one.

| Order dates | Complete as of | Deletions as of |
|---|---|---|
| all | Wednesday | Wednesday |

**The same `changes` file, but with everything since Monday: rejected.** Orders dated up to Oct 24
were proven only as of Sunday. An order dated Oct 10 that changed on Sunday evening is in neither
file: Sunday's was taken before the change, and Wednesday's starts on Monday, after it. dogana
rejects the file rather than claim Wednesday for those orders.

## What happens to a file

1. The receiver writes a **contract** for each table: its key, its window column, the shapes it
   accepts, the deleted column if a shape has tombstones, and the hash of the table's columns.
2. Every file arrives with a **manifest**: its shape, its read boundary, its window or checkpoint,
   how many live rows and tombstones it holds, and the SHA-256 of its bytes. A sender writes it with
   `dogana manifest` or `Contract.manifest`.
3. The **gate** first checks the manifest against the contract and the ledger (a changes file needs
   a proven base, a checkpoint must leave no gap), then has the **store** check the rows against the
   manifest: counts, keys, windows, cursor values, column hash, bytes, and how much a complete file
   shrinks the table.
4. The store publishes the rows, the new dates and a **receipt** in one transaction, or records a
   rejection and changes nothing. The same manifest delivered again is a **replay**: it returns the
   original receipt and publishes nothing. Receipts are the history; the **ledger**, the current
   pieces and their dates, can always be rebuilt from them.

## Reading the dates

A reader asks the ledger for **claims** at its current time. Each piece gets a **state**:

| State | Meaning |
|---|---|
| unknown | nothing has been proven there |
| complete | both dates are equal: the piece is the source at that instant |
| patched | complete as of a later date than deletions: deleted rows may remain |
| expired | complete as of more than the contract's freshness (seven days by default) before the reader's time |

The table's state is the worst of its pieces, in the order unknown, expired, patched, complete, and
its dates are the earliest of its pieces. `Claims.combine` does the same across several tables: a
report that joins them can trust its inputs no further than those dates, and even then the join
itself is not certified. Questions with a "not" reverse which date matters. "Items with no
movement" over a patched movements table can miss an item: a movement deleted at the source may
still be in the table, so the item looks moved.

The planner works the other way round: given the dates a reader needs, `plan_delivery` names the
cheapest read that would establish them, and `next_extraction` names the next read of a shape.

## Tables read together

Order headers and their lines are two tables, and every line names its order. Each table can be
proven on its own and the two can still disagree:

| Time | Source | Delivery |
|---|---|---|
| 10:00 | | `orders` read |
| 10:03 | order 42 created, with its line | |
| 10:05 | | `order_lines` read, with order 42's line |

Both deliveries are correct: `orders` is complete as of 10:00, `order_lines` as of 10:05. A report
joining lines to orders finds a line whose order is missing. `Claims.combine` gives 10:00 for both
dates, but that is an evidence date, not a picture of the source at 10:00: the 10:05 read holds rows
created after 10:00, like order 42's line, and misses rows deleted after 10:00.

A piece whose two dates are equal is the source at that instant: nothing missing, nothing extra. A
set of tables is the source at one instant when every piece of every table has both dates at that
same instant. Equal table dates are not enough, because a table's dates are the earliest of its
pieces: the window table above has both dates on Sunday while three of its days are proven on
Tuesday. Then any query over the tables, a join, a `NOT EXISTS`, a total, answers as the source would
have. `Claims.exact_as_of` checks every piece and names that instant, or nothing. In the example the
two tables are exact at 10:00 and 10:05, so there is no common instant, and a reader knows not to
trust the join.

To have one, read every table from one consistent snapshot of the source, so every file carries
that snapshot's instant. One transaction is not always one snapshot: at the default isolation level
of most databases each statement sees the data as of its own start. The
[protocol](protocol.md#tables-together) lists the settings that give one snapshot. Then deliver the
files together. A `Group` names the tables and the references between them, and `deliver_together`
decides the files as one delivery: if one file fails a check, no table changes, and if all pass,
every table is published in one step, so a reader never sees new lines with old orders.
`plan_together` names one read per table at one shared boundary.

A reference adds one check, run only when a failure would prove something: when the lines file makes
the part of the table it covers exact, with its deletions, and the orders are complete over the whole
table. Then every line in that part must name an order present once both files are published. A line
naming a missing order proves the two files were not one snapshot, or that the source does not keep
the rule, and the whole delivery is refused. Otherwise the reference is not checked, and the receipt
says why. Tables without one shared snapshot, from different systems or exported by separate jobs,
are delivered separately with `deliver`: only the earliest dates remain, and no reference is checked.
Stamping them with one read time would fabricate the instant.

## What it is not

- Not change data capture: CDC transports changes; dogana accounts for what a delivered batch
  proves. A CDC replica that already applies inserts, updates and deletes may not need dogana.
- Not a scheduler or an extractor: the planner proposes reads, your job runs them.
- Not a quality tool: it checks what bytes can prove (counts, keys, ranges, hashes), never whether
  the source really exported everything, nor business rules.
- Not a stream processor: a micro-batch committed from a stream is one delivery; there are no
  event-time windows or watermarks.
- Not SQLMesh intervals, Iceberg snapshots or dbt snapshots: those record processed intervals,
  table versions and row history; dogana records source evidence and deletion coverage.

## Terms

| Term | Meaning |
|---|---|
| Contract | The receiver's declaration for one table: key, window, cursor and deleted columns with their types, accepted shapes, freshness, shrink limit and column hash. |
| Shape | A kind of file the receiver accepts: coverage, completeness, deletions, checkpoint and cost. |
| Manifest | The sender's declaration of one file: shape, read boundary, window or checkpoint, live rows, tombstones, column hash and SHA-256. |
| Read boundary | `produced_at`: the source instant the file describes, never the time it was written or received. |
| Tombstone | A delivered row whose deleted column is true: its key is deleted. |
| Gate | The checks that turn a manifest and its rows into acceptance or rejection. |
| Region | One piece of the window column's range, with its two dates or unknown. |
| Ledger | Every region of a table with its dates; rebuilt from accepted receipts. |
| Claims | The regions and their states at the reader's current time. |
| Receipt | The record of one decision: accepted, rejected or replayed, its checks, its receiver time and the resulting ledger. |
| Store | Holds and checks candidate rows, then publishes rows, ledger and receipt together. |
| Group | Tables read together, at one read boundary, and the references between them. |
| Reference | A rule the source keeps between two tables: each row of one names the key of a row of the other. |

The data contract (an ODCS document, a dbt model, a column list) owns the columns; the delivery
contract carries only the types of the columns it reads and an opaque hash of the rest. Store
authors run `dogana.conformance.StoreConformance`; see [reference implementations](reference.md),
[integrations](integrations.md) and [architecture](architecture.md). DuckDB and the file drop are
executable reference implementations, not production warehouse support.
