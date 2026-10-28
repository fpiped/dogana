# Concepts

dogana from the beginning, one idea at a time, each with an example. The [protocol](protocol.md)
specifies the same ideas precisely.

## The problem

A source system (an ERP, a SaaS API, a database you do not own) has a table that changes over
time. Each file it sends is a photograph of part of that table at one instant, the **read
boundary** `produced_at`. What the photograph proves depends on how it was taken:

- a full dump: every row is present, and every row missing from it was deleted;
- every order dated in the last 30 days: the same, for those 30 days of orders only;
- every row changed since yesterday: nothing new or updated is missing; nothing about deletions.

Once loaded into one table, a row is just a row: nobody remembers which file proved what. dogana
keeps that record beside the rows, and rejects any file that would make it lie.

"Proves" means: proves if the sender read the source as its manifest declares. dogana checks each
file against its declaration (rows, counts, window, bytes), never against the source. A full dump
that silently left out rows, with a count to match, passes.

## Two dates

For every part of the table, dogana keeps two instants:

- **complete as of** (`c`): every row the source had at `c` is here;
- **deletions as of** (`d`): no row the source deleted before `d` is still here; one deleted after
  `d` may be.

`d` is never later than `c`. When they are equal, that part is exactly the source at that instant.
When `c` is later, the part is **patched**: every row is here, plus possibly some the source has
deleted since `d`.

A full dump on Sunday gives (Sunday, Sunday). Monday's changes, without deletions, move `c` to
Monday and leave `d` on Sunday. The rule behind every decision: **a delivery never raises a date
above what it proves**.

## Which instant to declare

`produced_at` is the instant of the source the file shows, never when the file was written, sent
or received. A nightly job scheduled for midnight queries the source at 02:00 and finishes the file
at 02:40:

- it declares 02:00 when the query reads one snapshot taken then, and a changes read declares the
  upper bound it selects, `updated_at <= 02:00`;
- midnight, the scheduler's date, claims too much: a row deleted at 01:00 is not in the file, yet
  "complete as of midnight" says it is here;
- 02:40 claims too much too: a row created at 02:10 is not in the file either.

A read that is not one snapshot, such as an API paged for forty minutes, shows no single instant:
declaring its start is wrong by the rows created or deleted while it ran. Read one snapshot when
the source has one ([the settings](protocol.md#tables-together)). In a changes read, a transaction
that stamps `updated_at = 01:59:58` and commits at 02:00:03 is invisible at 02:00; in a keyed
contract a shape's `lookback` makes the planned next read overlap it, which narrows the gap without
proving it closed. Every time carries its UTC offset.

## Where the dates live: the window column

The dates are kept per range of one column, the **window column**, not per row. Take `orders` with
`order_date` as its window column, a full dump on Sunday (Oct 25), and on Tuesday (Oct 27) a file
of every order dated Oct 25 to Oct 27:

| Order dates | Complete as of | Deletions as of |
|---|---|---|
| up to Oct 24 | Sunday | Sunday |
| Oct 25 to Oct 27 | Tuesday | Tuesday |
| from Oct 28 | Sunday | Sunday |

Each piece is a **region**, and every possible `order_date` falls in exactly one. Before any
delivery there is one region, **unknown**: not "there are no orders" but "nothing is proven here".
A table without a window column has one region, the whole table.

**The window column's value must never change for a row.** A creation, order, invoice or event
date works; an update timestamp never does. Suppose `order_date` were `updated_at`:

1. Oct 1, full dump: order 7 has `updated_at = Sep 15`.
2. Oct 20: order 7 is edited, and its `updated_at` becomes Oct 20.
3. Oct 25, a file of the rows dated Sep 1 to Sep 30: order 7 is no longer in that range, so
   replacing the range deletes it.

The table still claims "complete as of Oct 1", yet order 7 existed on Oct 1 and is gone: it moved
between two regions proven at different instants, and neither file carried it. No single file
shows that a value changed, so this rule is the receiver's to keep. "Rows updated in the last 30
days" is a changes read, not a window.

## What a file can prove: shapes

A **shape** is a kind of file the receiver accepts, described by four answers:

| Question | Answers |
|---|---|
| Which part of the table? (coverage) | the whole table (`all`), or a range of the window column, both ends included (`window`) |
| Every current row, or only what changed? (completeness) | `complete`, or `changed_only` since a checkpoint |
| How are deletions told? (deletions) | a missing row is deleted (`absence`, complete files only), the file marks deleted keys (`tombstone`), or nothing is said (`none`) |
| Changed since what? (checkpoint) | nothing (`none`, complete files), a source time (`timestamp`), a token the source hands out with each read (`generation`), or an ordered integer such as a log position (`sequence`) |

| Source behavior | Shape | What it proves |
|---|---|---|
| A full export | `Shape.full()` | both dates, everywhere |
| Every order dated in the last 30 days | `Shape.window(size=timedelta(days=30))` | both dates, for those dates |
| One complete file per plant or per day | a window whose two ends are equal | both dates for that value; values never delivered stay unknown |
| Rows with `updated_at` after the last read | `Shape.changes()` | complete as of this read; the deletions date does not move |
| The same, plus the keys deleted since | `Shape.changes(deletions="tombstone")` | both dates, when the read starts no later than the current deletions date |
| A change feed with a sync token | `Shape.feed()` | both dates, when each read starts from the token the previous one handed out |
| CDC batches by log position or offset | `Shape.feed(checkpoint="sequence")` after a full snapshot | the same, each batch starting where the last one ended |
| Immutable events with no key | `Shape.append()`, with `start_at` the instant history starts | both dates, when each read starts exactly where the last one ended |

A shape's **cost** says how expensive that file is to ask for, relative to the others (a full dump
at 10 costs ten changes reads at 1). Only the planner uses it.

## Deletions

The hard part is **physical** deletions: the row disappears from the source, so no later export
can contain it. A **soft delete** keeps the row and sets a flag: it is an update, carried like any
other.

A tombstone shape tells deletions in its own rows. The contract names a boolean **deleted column**;
a row where it is true is a **tombstone**, which deletes its key and leaves its other values empty:

```text
id,order_date,amount,deleted
7,2026-10-26,120,false
9,,,true
```

This file updates order 7 and deletes order 9. Its manifest declares `rows=1` and `tombstones=1`,
never the keys, so it stays small whatever the volume. The deleted column is never published, and
a file that deletes nothing may leave it out.

A soft-deleting source may declare a tombstone shape, delivering its flag as the deleted column
or keeping flagged rows as data, only when the soft delete also moves `updated_at` (or the changes
read never selects the flagged row), nothing ever deletes physically (archiving, retention purges,
legal erasure, cascades) and the flag lives on the table being loaded. Otherwise the honest shape says
`deletions="none"`, and a periodic full dump heals the gap. Debezium's new-record-state transform
rewrites deletes as rows with a boolean `__deleted`: that is a deleted column.

## Writing a contract

The receiver answers six questions per table:

1. **Key**: the columns that identify a row; none for immutable events.
2. **Window column**: a date that never changes for a row, when the source exports ranges of it;
   otherwise none, and the table is one region.
3. **Shapes**: one per kind of file the source can really produce, with its cost.
4. **Deleted column**: the boolean that marks tombstones, when a shape has them.
5. **Cursor column**: the `updated_at` or position a changes read selects on, so every row is
   checked to lie in the declared interval.
6. **Column hash**: the `schema_hash` of the columns, from the data contract that owns them (an
   ODCS document, a dbt model, a column list), so a file with other columns is rejected.

```python
orders = Contract(
    "orders",
    key=("id",),
    shapes={
        "full": Shape.full(cost=10),
        "last3": Shape.window(size=timedelta(days=3), cost=2),
        "changes": Shape.changes(deletions="tombstone"),
    },
    window_column="order_date",
    cursor_column="updated_at",
    deleted_column="deleted",
    types={"id": "int", "order_date": "date", "updated_at": "datetime", "deleted": "bool"},
)
```

`types` lists only the columns dogana reads. `freshness` is the export cadence plus arrival slack,
seven days by default; `max_shrink_fraction` rejects a complete file that removes more than half
the rows it covers.

## A week, step by step

With the contract above:

- **Sunday, Oct 25, `full`**: the whole table is proven as of Sunday.
- **Tuesday, Oct 27, `last3` for Oct 25 to Oct 27**: those days move to Tuesday, the rest stays at
  Sunday (the table in the window column section).
- **Wednesday, Oct 28, `changes` since Sunday**: it covers every order date and every deletion
  since Sunday, so the whole table is proven as of Wednesday and the regions merge back into one.
- **The same file, but since Monday: rejected.** Orders dated up to Oct 24 were proven only as of
  Sunday. One that changed on Sunday evening is in neither file: Sunday's was taken before the
  change, Wednesday's starts after it.

## What happens to a file

1. The sender writes a **manifest**: shape, read boundary, window or checkpoint, live rows,
   tombstones and the SHA-256 of the bytes (`dogana manifest` or `Contract.manifest`).
2. The **gate** checks the manifest against the contract and the ledger (a changes file needs a
   proven base, a checkpoint must leave no gap), then has the **store** check the rows: counts,
   keys, windows, cursor values, column hash, bytes, and how much a complete file shrinks the table.
3. The store publishes the rows, the new dates and a **receipt** in one transaction, or records a
   rejection and changes nothing. The same manifest again is a **replay**: it returns the original
   receipt and publishes nothing.

Receipts are the history; the **ledger**, the current regions and their dates, is rebuilt from
them.

## Reading the dates

A reader asks the ledger for **claims** at its current time. Each region gets a **state**:

| State | Meaning |
|---|---|
| unknown | nothing is proven there |
| complete | both dates equal: the region is the source at that instant |
| patched | complete as of a later date than deletions: deleted rows may remain |
| expired | complete as of longer ago than the contract's freshness |

A table's state is the worst of its regions (unknown, expired, patched, complete) and its dates
the earliest. `Claims.combine` does the same across tables: a report joining them trusts its inputs
no further than those dates, and the join itself is not certified. Questions with a "not" reverse
which date matters: "items with no movement" over a patched movements table can miss an item,
because a movement deleted at the source may still be here.

The planner works the other way round: `plan_delivery` names the cheapest read that would establish
the dates a reader needs, and `next_extraction` the next read of a shape.

## Tables read together

Orders and their lines are two tables, each proven on its own, and they can still disagree:

| Time | Source | Delivery |
|---|---|---|
| 10:00 | | `orders` read |
| 10:03 | order 42 created, with its line | |
| 10:05 | | `order_lines` read, with order 42's line |

Both deliveries are correct, yet a join finds a line whose order is missing. `Claims.combine` gives
10:00, an evidence date, not a picture of the source at 10:00: the 10:05 read holds rows created
after 10:00.

A set of tables is the source at one instant when every region of every table has both dates at
that instant. Then any query over them, a join, a `NOT EXISTS`, a total, answers as the source
would have. Equal table dates are not enough, since a table's dates are its earliest region's.
`Claims.exact_as_of` checks every region and names that instant, or none: here the tables are
exact at 10:00 and 10:05, so there is no common instant, and the reader knows not to trust the
join.

To have one, read every table from one snapshot of the source, so every file carries its instant.
One transaction is not always one snapshot: at the default isolation level of most databases each
statement sees the data as of its own start [5]; the [protocol](protocol.md#tables-together) lists
the settings that give one. Then deliver the files together: a `Group` names the tables and the
references between them, and `deliver_together` decides them as one delivery. If one file fails,
no table changes; if all pass, every table is published in one step. `plan_together` names one
read per table at one shared boundary.

A reference is checked only when a failure would prove something: when the lines file makes its
coverage exact, deletions included, and the orders are complete over the whole table. Then a line
naming a missing order proves the files were not one snapshot, or that the source does not keep
the rule, and the whole delivery is refused. Otherwise the receipt says why it was not checked.
Tables without one shared snapshot, from different systems or separate jobs, are delivered apart
with `deliver`; stamping them with one read time would fabricate the instant.

## What it is not

- Not change data capture: CDC transports changes; dogana accounts for what a delivered batch
  proves. A CDC replica that already applies inserts, updates and deletes may not need it.
- Not a scheduler or an extractor: the planner proposes reads, your job runs them.
- Not a quality tool: it checks what bytes can prove (counts, keys, ranges, hashes), never whether
  the source exported everything, nor business rules.
- Not a stream processor: a micro-batch committed from a stream is one delivery; there are no
  event-time windows or watermarks.
- Not SQLMesh intervals, Iceberg snapshots or dbt snapshots: those record processed intervals,
  table versions and row history; dogana records source evidence and deletion coverage.

## Related work

The two dates are a dated form of two properties database research has long named. Motro split a
database's integrity into completeness, every true fact is stored, and validity, every stored fact
is true [1, 2]: complete as of speaks about the first, deletions as of about the part of the second
that concerns rows the source deleted. Levy, then Razniewski and Nutt, studied statements that part
of a table is complete, and when they make a query's answer complete [3, 4]. A region is such a
statement, dated and maintained one delivery at a time; [tables read together](#tables-read-together)
is its case where every part is exact at one instant.

1. A. Motro. [Completeness Information and Its Application to Query Processing](https://www.vldb.org/conf/1986/P170.PDF).
   VLDB 1986, 170-178.
2. A. Motro. [Integrity = Validity + Completeness](https://doi.org/10.1145/76902.76904). ACM
   Transactions on Database Systems 14(4), 1989, 480-502.
3. A. Y. Levy. [Obtaining Complete Answers from Incomplete Databases](https://www.vldb.org/conf/1996/P402.PDF).
   VLDB 1996, 402-412.
4. S. Razniewski, W. Nutt. [Completeness of Queries over Incomplete Databases](https://doi.org/10.14778/3402707.3402715).
   Proceedings of the VLDB Endowment 4(11), 2011, 749-760.
5. H. Berenson, P. Bernstein, J. Gray, J. Melton, E. O'Neil, P. O'Neil. [A Critique of ANSI SQL
   Isolation Levels](https://doi.org/10.1145/223784.223785). SIGMOD 1995, 1-10.

## Terms

| Term | Meaning |
|---|---|
| Source | The system whose table is exported: an ERP, a SaaS API, a database. |
| Sender | Whoever produces a file and its manifest from the source. |
| Receiver | Whoever holds the contract, the store and the ledger, and decides on each file. |
| Reader | Whoever asks the ledger how far to trust the rows. |
| Contract | The receiver's declaration for one table: key, window, cursor and deleted columns, accepted shapes, freshness, shrink limit, column hash. |
| Shape | A kind of file the receiver accepts: coverage, completeness, deletions, checkpoint, cost. |
| Manifest | The sender's declaration of one file: shape, read boundary, window or checkpoint, live rows, tombstones, column hash, SHA-256. |
| Read boundary | `produced_at`: the source instant the file shows. |
| Window column | The date, fixed for each row, along which the dates are kept. |
| Region | One range of the window column, with its two dates or unknown. |
| Patched | Complete as of a later date than deletions: rows the source deleted may remain. |
| Tombstone | A delivered row whose deleted column is true: its key is deleted. |
| Gate | The checks that turn a manifest and its rows into acceptance or rejection. |
| Store | Checks candidate rows, then publishes rows, ledger and receipt together. |
| Receipt | The record of one decision: accepted, rejected or replayed, with its checks. |
| Replay | The same manifest delivered again: the original receipt, nothing published. |
| Ledger | Every region of a table with its dates, rebuilt from accepted receipts. |
| Claims | The regions and their states at the reader's time. |
| Group | Tables read from one snapshot, and the references between them. |
| Reference | A rule the source keeps between two tables: each row of one names a row of the other. |
