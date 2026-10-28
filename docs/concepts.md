# Concepts

dogana from the beginning, each idea with an example. The [protocol](protocol.md) states the same
rules precisely.

## The problem

A source (an ERP, a SaaS API, a database you do not own) has a table that changes. Each file it
sends shows part of that table at one instant, its **read boundary** `produced_at`, and proves what
its kind of read proves:

- a full dump: every row is here, and every row missing was deleted;
- every order dated in the last 30 days: the same, for those days only;
- every row changed since yesterday: nothing new or updated is missing; nothing about deletions.

Once loaded, a row is just a row, and nobody remembers which file proved what. dogana keeps that
record beside the rows and refuses any file that would make it lie. "Proves" always means: if the
sender read the source as its manifest declares. dogana checks files against declarations, never
against the source.

## Two dates

For every part of a table dogana keeps two instants:

- **complete as of** (`c`): every row the source had at `c` is here;
- **deletions as of** (`d`): no row the source deleted before `d` is still here.

`d` is never later than `c`. When they are equal, that part is exactly the source at that instant;
when `c` is later, it is **patched**: rows deleted since `d` may remain. A full dump on Sunday gives
(Sunday, Sunday); Monday's changes without deletions give (Monday, Sunday). The rule behind every
decision: **a delivery never raises a date above what it proves**.

## Which instant to declare

`produced_at` is the instant of the source the file shows. A nightly job scheduled at midnight
queries at 02:00 and finishes the file at 02:40. It declares 02:00, the instant of its snapshot, or
for a changes read the bound it selects, `updated_at <= 02:00`. Midnight would claim a row deleted
at 01:00 is here; 02:40 would claim a row created at 02:10 is here.

A read that is not one snapshot, such as an API paged for forty minutes, shows no single instant.
A transaction that stamps `updated_at = 01:59:58` and commits at 02:00:03 is invisible at 02:00; a
keyed shape's `lookback` overlaps the next read to narrow that gap, without proving it closed.

## The window column

Dates are kept per range of one column, the **window column**, not per row. With `order_date` as
window column, a full dump on Sunday (Oct 25) and on Tuesday a file of the orders dated Oct 25 to
27:

| Order dates | Complete as of | Deletions as of |
|---|---|---|
| up to Oct 24 | Sunday | Sunday |
| Oct 25 to Oct 27 | Tuesday | Tuesday |
| from Oct 28 | Sunday | Sunday |

Each range is a **region**. Before any delivery there is one, **unknown**: nothing is proven there,
which is not the same as empty. A table without a window column is one region.

**A row's window value must never change**: an order, invoice or event date, never an update time.
With `updated_at`, an order edited on Oct 20 leaves the September region, and a later file of
September deletes it while no file carries it into October. No file can show this, so the rule is
the receiver's to keep.

## Shapes

A **shape** is a kind of file the receiver accepts: the whole table or a window, every current row
or only changes, deletions told by absence, by tombstones or not at all, and changes since a time, a
token the source hands out, or a log position.

| Source behavior | Shape | What it proves |
|---|---|---|
| A full export | `Shape.full()` | both dates, everywhere |
| Every order dated in the last 30 days | `Shape.window(size=timedelta(days=30))` | both dates, for those dates |
| One complete file per day or plant | a window with equal ends | both dates for that value |
| Rows with `updated_at` after the last read | `Shape.changes()` | complete as of this read; deletions do not move |
| The same, with the keys deleted since | `Shape.changes(deletions="tombstone")` | both dates, when the read starts no later than the deletions date |
| A change feed with a sync token | `Shape.feed()` | both dates, when each read starts from the previous token |
| CDC batches by log position | `Shape.feed(checkpoint="sequence")` after a full snapshot | the same, by position |
| Immutable events without a key | `Shape.append()` from `start_at` | both dates, when each read starts where the last ended |

A shape's **cost** tells the planner how expensive that file is to ask for.

## Deletions

Only **physical** deletions are hard: the row is gone, so no later export can show it. A
**tombstone** shape names them in the file: a row whose boolean **deleted column** is true deletes
its key, and its other values may be empty.

```text
id,order_date,amount,deleted
7,2026-10-26,120,false
9,,,true
```

Its manifest declares `rows=1` and `tombstones=1`, never the keys. A **soft delete**, a flag on a
kept row, may serve as the deleted column only if it moves `updated_at` (or the changes read never
selects the flagged row), nothing deletes physically (archiving, retention purges, legal erasure,
cascades) and the flag is on this table; otherwise the shape says `deletions="none"` and a periodic
full dump heals the gap. Debezium's new-record-state transform writes deletes as rows with a boolean
`__deleted`: that is a deleted column.

## Writing a contract

The receiver answers, per table: the **key**; the **window column**, if the source exports ranges of
a date fixed per row; the **shapes** the source can really produce, with their costs; the **deleted
column**, if a shape has tombstones; the **cursor column** a changes read selects on, so each row is
checked against the declared interval; and the **column hash** from the data contract that owns the
columns.

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

`types` names only the columns dogana reads. `freshness`, seven days by default, is the cadence plus
arrival slack; a complete file that removes more than half the rows it covers is refused.

With this contract, a `changes` file since Sunday, after Sunday's `full` and Tuesday's `last3`,
proves the whole table as of its read. The same file since Monday is refused: an order dated Oct 10
and changed on Sunday evening is in neither Sunday's file nor Monday's.

## Delivering and reading

The **gate** checks a file's **manifest** against the contract and the ledger (a changes file needs
a proven base, a checkpoint leaves no gap), the **store** checks the rows (counts, keys, values,
column hash, bytes, shrinkage), then publishes rows, dates and a **receipt** in one transaction, or
records a refusal. The same manifest again is a **replay**, answered by its receipt. The **ledger**
of regions is rebuilt from the receipts.

A reader asks the ledger for **claims** at its current time: each region is unknown, **complete**,
patched or **expired** (older than the freshness), and a table takes its worst state and earliest
dates. Questions with a "not" need the deletions date: "items
with no movement" over patched movements can miss an item whose deleted movement remains.
`plan_delivery` names the cheapest read that gives the dates a reader needs.

## Tables read together

A table is **exact at p** when every region has both dates at p: it holds exactly the rows the
source had at p. If every table a query reads is exact at the same p, the query answers as the
source would have at p, whatever it computes (a join, a `NOT EXISTS`, a total), because its inputs
are the source's tables at p. `Claims.exact_as_of` names that instant, or none.

Tables exact each on its own are not enough:

| Time | Source | Delivery |
|---|---|---|
| 10:00 | | `invoices` read: invoice 1 |
| 10:03 | invoice 2 created, with its line 11 | |
| 10:05 | | `invoice_lines` read: lines 10 and 11 |

Invoices are exact at 10:00 and lines at 10:05, so the join finds line 11 without its invoice, and
`exact_as_of` is none: the reader knows not to trust the join.

One instant takes one read: every table from one snapshot, every manifest stamped with its instant.
One transaction is not always one snapshot: at the default isolation level of most databases each
statement sees the data as of its own start [5]; the [protocol](protocol.md#tables-together) lists
the settings that give one. The snapshot is the sender's word, like every declaration.

The receiver declares the tables as a `Group`, with the rules the source keeps between them as
`Reference`s (each line names an invoice), and delivers them with `deliver_together`, which:

1. checks each file as if it came alone;
2. requires one `produced_at` for all;
3. checks each reference, when a failure would prove something;
4. publishes every table in one transaction, or none.

A reference is checked only when an honest snapshot cannot fail it: the lines are exact at p where
delivered, and the invoices complete as of p over the whole table. Then a line naming a missing
invoice proves the files are not one snapshot, and every table is refused. Otherwise an honest
delivery could fail it: patched lines may keep a line the source deleted with its invoice, and an
invoice may sit in a region proven before it existed. So the check is skipped and the receipt says
why. It catches orphans only: separate reads stamped with one instant pass when no line
happens to be orphaned.

Publishing in one step spares readers new lines beside old invoices, but the guarantee comes from
the dates: a reader reads rows and ledgers under one publication and checks `exact_as_of`. Every
later delivery must bring what it covers back to `(p, p)`: a complete file or a keyless append, or
changes with tombstones covering every deletion since the deletions date. A window file does so only
inside its window, so a reader of windows passes them to `Group.claims`. Changes without tombstones
leave a table patched and the group without an instant; a table that did not change sends an empty
changes file at p. `plan_together` names one read per table at one shared boundary.

## What it is not

Not change data capture, a scheduler, an extractor, a quality tool or a stream processor; and not
SQLMesh intervals, Iceberg snapshots or dbt snapshots, which record processed intervals, table
versions and row history, not what the source's files proved.

## Related work

Motro split a database's integrity into completeness, every true fact is stored, and validity, every
stored fact is true [1, 2]: complete as of dates the first, deletions as of the part of the second
about deleted rows. Levy, then Razniewski and Nutt, studied statements that part of a table is
complete and when they make a query's answer complete [3, 4]. A region is such a statement, dated;
[tables read together](#tables-read-together) is its case where every part is exact at one instant.

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
| Sender, receiver, reader | Writes a file and its manifest; decides on it; asks how far to trust the rows. |
| Contract, shape | The receiver's declaration of a table; a kind of file it accepts. |
| Manifest | The sender's declaration of one file. |
| Read boundary | `produced_at`, the source instant the file shows. |
| Region | A range of the window column with its two dates, or unknown. |
| Patched | Complete as of a later date than deletions. |
| Exact at p | Both dates at p everywhere. |
| Tombstone | A delivered row whose deleted column is true. |
| Receipt, replay | The record of a decision; the same manifest again. |
| Ledger, claims | Every region with its dates; their states at the reader's time. |
| Group, reference | Tables read from one snapshot; a rule naming rows across them. |
