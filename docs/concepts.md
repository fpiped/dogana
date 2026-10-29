# Concepts

One example runs through this page: a shop whose ERP exports its `orders` table, and a loader that
puts the rows into the shop's own database. The [protocol](protocol.md) states the same rules
exactly, edge cases included.

## The problem

The **source** is the system that owns a table: an ERP, a SaaS API, a database you do not control.
Loading its table into your own database is two jobs, and they are often done by two parties.

The **sender** reads the source: it runs an export, a query or an API call. At that moment it knows
what it read: the instant, the part of the table, the number of rows. The **receiver** writes the
table, and sees only rows. The two may be different vendors, different systems, or one script that
does both; what the sender knew is gone once the rows land, unless it is written down.

A **read** is what the sender got: a copy of part of the table at one instant. A file is one way a
read arrives; a query result from a view, an API response or a batch from a stream are others. Each
kind of read proves something different:

- A full dump of `orders` has every order. An order you loaded before and that is missing now was
  deleted at the source.
- A read of every order dated in the last 30 days tells you the same, for those 30 days only.
- A read of every order changed since yesterday has every new and updated order. It says nothing
  about deleted orders, because a deleted order is no longer there to export.

Once loaded, the rows look the same whichever read brought them. You can no longer tell whether an
order is in your table because the source still has it, or because no read has told you it was
deleted.

dogana makes the sender write down what it read, in a **manifest**, and makes the receiver check it
and remember what it proved, in a record kept beside the rows. The receiver runs dogana: it checks
every delivery, updates the record, and refuses a delivery that would make the record wrong. The
discipline holds even when sender and receiver are one script, because the manifest is the only
place where the instant and the kind of read survive, and the record holds the script to its own
word.

dogana never sees the source: it checks each delivery only against what the sender declares about
it. If the export misses rows the source has, and the declaration counts only what was exported, no
check can tell.

## Two dates

For a table, or for each range of it (see [the window column](#the-window-column)), dogana keeps
two instants:

- **complete as of** (`c`): every row the source had at `c` is in your table.
- **deletions as of** (`d`): no row the source deleted before `d` is still in your table.

On Sunday the source sends a full dump of `orders`. Your table now holds exactly the orders the
source had on Sunday, so both dates are Sunday.

On Monday it sends the orders changed since Sunday. Every new and updated order arrives, so the
table is complete as of Monday. An order deleted on Monday morning is not in the read, so it is
still in your table, and the deletions date stays at Sunday.

`d` is never later than `c`. When both equal an instant `p`, the table holds exactly the rows the
source had at `p`. When `c` is later, the table is **patched**: it may still hold rows the source
deleted between `d` and `c`.

Every decision dogana makes follows one rule: **a read moves a date only as far as the read
proves.**

## The window column

dogana can keep the two dates separately for each range of one column, the **window column**. Its
value never changes on a row; usually it is a date, such as `order_date`. Ranges are needed because
some reads cover only part of that column, such as the orders of the last 30 days.

On Sunday, October 25, the source sends a full dump. On Tuesday it sends every order dated October
25 to 27. The table now has three ranges:

| Order dates | Complete as of | Deletions as of |
|---|---|---|
| up to October 24 | Sunday | Sunday |
| October 25 to 27 | Tuesday | Tuesday |
| from October 28 | Sunday | Sunday |

The last range is proven too: Sunday's dump had every order, including any dated in the future.

A range with its two dates is a **region**, and the list of a table's regions is its **ledger**.
Before the first delivery, the table is one region with no dates, called **unknown**. Unknown means
nothing has been proven there yet; it does not mean empty. A table without a window column is
always one region.

**A row's window value must never change.** Use an order date, an invoice date or an event date,
never an update time. Suppose the window column is `updated_at`, and a September order is edited on
October 20:

- A read of the October range, taken before the edit, does not have the order: it was still a
  September row.
- A read of the September range, taken after the edit, does not have it either, because its
  `updated_at` is now October 20. dogana removes it as deleted.

The order is now in neither range, and both ranges say they are complete. No read shows the
mistake, so choosing a column that never changes is up to you.

## Shapes

A **shape** is one kind of read the receiver accepts. Shapes differ in three ways:

- **coverage**: the whole table, or a window of the window column;
- **completeness**: every current row, or only the rows changed since a point;
- **deletions**: told by absence (a missing row was deleted), by tombstones (next section), or not
  at all.

A shape of changes also says how that point, its **checkpoint**, is measured: a time (`timestamp`),
a token the source hands out with each read (`generation`), or a position in the source's log
(`sequence`).

| What the source can send | Shape | What it proves |
|---|---|---|
| A full export | `Shape.full()` | Both dates, for the whole table |
| Every order dated in the last 30 days | `Shape.window(size=timedelta(days=30))` | Both dates, for those days |
| One complete read per day or per plant | A window whose two ends are equal | Both dates, for that day or plant |
| The rows whose `updated_at` is after the last read | `Shape.changes()` | Complete as of the read; the deletions date does not move |
| The same, plus the keys deleted since | `Shape.changes(deletions="tombstone")` | Both dates, if the read starts no later than the deletions date |
| A change feed with a sync token | `Shape.feed()` | Both dates, if both were already equal (after a full export) and each read starts from the token the previous one issued |
| Change data capture batches by log position | `Shape.feed(checkpoint="sequence")` | Both dates, if both were already equal (after a full snapshot) and each batch starts where the previous one ended |
| Events without a key that never change | `Shape.append()` | Both dates, if each read starts where the previous one ended |

Each shape has a **cost**: how expensive that read is to ask the source for. dogana uses it to pick
the cheapest read that gives a reader the dates it needs.

## Deletions

The difficult case is a **physical** deletion, also called a hard delete: the row is gone from the
source, so no later export can show it. A read can report it in two ways.

A **complete** read holds every current row of what it covers, such as a full dump or a window
read. It reports deletions by absence.

A **tombstone** is a row in a read that says its key was deleted. The table's contract (below)
names a boolean column, the **deleted column**. A row where that column is true is a tombstone, and
its other values may be empty:

<!-- illustration: CSV sample showing a live row and a tombstone, not command output -->
```text
id,order_date,amount,deleted
7,2026-10-26,120,false
9,,,true
```

Order 7 is a current row; order 9 was deleted. The delivery's [manifest](#the-manifest) counts the
tombstones, here one, but never lists the keys.

A **soft delete** is a flag, such as `is_deleted`, on a row the source does not remove. It can be
the deleted column only if three things hold:

- setting the flag also moves the cursor column, usually `updated_at`, so a read of changes picks
  the row up;
- the source never removes a row physically, not even by archiving or retention purges;
- the flag is on this table, not on another one.

Otherwise declare the shape with `deletions="none"`, and send a full dump from time to time to
remove the deleted rows.

## Writing a contract

A **contract** is the receiver's description of one table. It names:

- the **key**: the columns that identify a row;
- the **window column**, if the source can export ranges of it;
- the **shapes** the source can produce, each with its cost;
- the **deleted column**, if a shape carries tombstones;
- the **cursor column**: the column a read of changes filters on, such as `updated_at`. The
  manifest declares the interval the read covers, from `since` to `produced_at`, and dogana checks
  that every current row of the read has its cursor value inside it;
- the **column hash**, `schema_hash`: a SHA-256 of the column names and types, computed from
  wherever the columns are defined, such as a data contract, a dbt model or a list of columns. A
  delivery with other columns is refused.

```python
from datetime import timedelta

from dogana import Contract, Shape

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

`types` gives the type of the columns dogana reads: key, window, cursor and deleted.

Take the reads of [the window column](#the-window-column) example:

- Sunday's `full` read proves the whole table as of Sunday.
- Tuesday's `last3` read proves the orders dated October 25 to 27 as of Tuesday.
- On Wednesday a `changes` read arrives with every change since Sunday's read. Its tombstones
  report the deletions, so it proves the whole table as of Wednesday, both dates.

The same delivery with only the changes since Monday is refused. Most of the table is proven only up to
Sunday's read, so a change made between that read and Monday would be in no read, such as an order
dated October 10 and changed on Sunday evening.

## The manifest

Every read arrives with a **manifest**, the sender's declaration about that one read:

- the table and the shape;
- `produced_at`, the instant the source was read (next section);
- the window, for a window read;
- `since`, where a read of changes starts, and for a feed `generation`, the token or position it
  ends at;
- `rows`, the number of current rows, and `tombstones`, the number of tombstones;
- `content_hash`, the SHA-256 of the file's bytes.

It usually travels as a JSON file beside the data file, such as `orders.csv.json`; the
[example manifest](examples/manifest.json) shows one, and `dogana manifest` writes one from a
shell.

## Which instant to declare

`produced_at`, the read's **read boundary**, is the instant the source was read.
dogana checks only that it is not in the future and is later than the dates it moves. The rest is up
to the sender.

A nightly job is scheduled at midnight. It queries the source at 02:00 and finishes writing the
file at 02:40. The read shows the source at 02:00, so it declares 02:00. For a read of changes,
02:00 is the upper bound of its query, `updated_at <= 02:00`. The other two times are wrong:

- Midnight claims the read has every order the source had at midnight. An order deleted at 01:00
  existed at midnight, but the read, taken at 02:00, does not have it.
- 02:40 claims the read has every order the source had at 02:40. An order created at 02:10 is not
  in it.

A **snapshot** is a read that sees the whole source at one instant. One database transaction can be
one, at the right isolation level (see [tables read together](#tables-read-together)). A read that
is not one snapshot, such as an API paged for forty minutes, shows no single instant. Whatever
instant it declares, it is wrong by the rows created or deleted while it ran.

## Delivering a read

A **delivery** is one read with its manifest, handed to dogana by the **loader**, the receiver's
code that puts rows into the database. It is decided in three steps:

1. The **gate** checks the manifest against the contract and the ledger. For example, a read of
   changes needs a base: the table must already be proven everywhere the read covers, and the read
   must start no later than the complete as of date there.
2. The loader checks the rows against the manifest, since only it can see them: the counts, unique
   keys, window values inside the window, the column hash, and the bytes against `content_hash`.
   It reports the results, and dogana records them.
3. If every check passes, the decision is an acceptance with a **plan**: the row operations the
   read implies, such as replace the table, replace a window, upsert, delete the tombstoned keys or
   append. The loader executes the plan and saves the new ledger and the receipt in the same
   transaction, so readers see all of them or none. If any check fails, the delivery is refused, rows
   and ledger stay as they were, and the refusal is recorded.

A **receipt** is the record of one decision: the manifest, the checks and the outcome. The ledger
can be rebuilt from the accepted receipts. The same manifest sent again is a **replay**: the loader
finds the first receipt by the manifest's identity, answers with it and changes nothing, so a job
that failed halfway can send its delivery again.

## Reading

A **reader** is whoever queries the loaded rows. Before trusting them, it asks the ledger for its
**claims**: the state of each region at the reader's current time.

| State | Meaning |
|---|---|
| unknown | Nothing is proven. |
| complete | Both dates are equal, and not expired. |
| patched | Complete as of a later date than deletions, and not expired. |
| expired | Complete as of a date older than the limit the reader gives. |

A table's state is its worst region's, in the order unknown, expired, patched, complete. Its dates
are the earliest complete as of and the earliest deletions as of among its regions, and it has no
dates at all while any region is unknown.

Which date a reader needs depends on the question:

- The rows a query lists ("orders over 1,000 euros") include every one the source had at `c`, when
  every region the query reads is complete as of the same `c`. They may include orders deleted
  since `d`.
- A question with "no" or "not" ("customers with no order in October") also needs the deletions
  date: a deleted order still in the table makes its customer look active, so the customer is
  wrongly left out of the answer.
- An exact count or sum needs both dates.
- Regions or tables proven at different instants guarantee none of this, as the next section
  shows.

`plan_delivery` works the other way: given the dates a reader needs, it names the cheapest read to
ask the source for ([quick start, step 6](quickstart.md#7-ask-for-the-next-read)).

## Tables read together

A query often reads several tables, such as invoices and their lines. A table is **exact at `p`**
when every region has both dates equal to `p`: it holds exactly the rows the source had at `p`.

If every table a query reads is exact at the same `p`, the query gives the answer the source would
have given at `p`, whatever it computes: a join, a `NOT EXISTS`, a total. Its inputs are the
source's tables at `p`, so its output is too. `Claims.exact_as_of` returns that instant, or none
when there is no such instant.

Each table being exact on its own is not enough:

| Time | At the source | Delivered |
|---|---|---|
| 10:00 | | `invoices`, read now: invoice 1 |
| 10:03 | invoice 2 is created, with its line 11 | |
| 10:05 | | `invoice_lines`, read now: lines 10 and 11 |

Invoices are exact at 10:00 and lines at 10:05. A join finds line 11 but not its invoice.
`exact_as_of` returns none, so the reader knows not to trust the join.

**One instant needs one snapshot.** The sender reads every table in one snapshot and stamps every
manifest with its instant. One transaction is not always one snapshot: at the default isolation
level of most databases, two queries in one transaction can see different data [5]. The
[protocol](protocol.md#tables-together) lists the settings that give one snapshot. dogana cannot
check that the read was one snapshot.

**A group.** The receiver declares the tables read together as a **group**. `Group.apply` then
decides their deliveries as one:

1. it decides each delivery as if it came alone;
2. it requires the same `produced_at` on every manifest;
3. if any delivery is refused, every delivery is refused. Otherwise the loader saves every
   table's rows and ledger in one transaction, or nothing.

Rules the source keeps between the tables, such as "every invoice line names an existing invoice",
are not checked: they need the rows, and the guarantee is `exact_as_of`, not a check. A loader that
wants a tripwire against separate reads stamped with one instant joins the staged tables before
committing; it catches only a line that happens to lose its invoice.

**Keeping one instant.** Committing every table at once means a reader never sees new lines beside
old invoices. Even so, a reader must read the rows and the ledgers together, from one committed
state of the database, and check `exact_as_of`.

To keep a common instant, every later delivery must leave each table with both dates equal to the
new instant. These reads do it:

- a complete read;
- an append of keyless events;
- changes with tombstones whose `since` is no later than the deletions date;
- a feed with tombstones, when both dates were already equal.

A window read does it only inside its window, so a reader that queries only those windows passes
them to `Group.claims`, which then judges only those ranges. Changes without tombstones leave a
table patched, and the group without an instant. For a table with no changes, the sender sends an
empty read of its tombstone changes shape, stamped with the new instant. `plan_together` returns
one read per table, all at one shared instant.

## What it is not

dogana does not capture changes from a database log, schedule jobs, extract data, test data quality
or process streams. It also differs from tools that keep history: SQLMesh records which intervals
a model processed, Iceberg records versions of a table, dbt snapshots record each row's history.
None of them records what the source's reads proved.

## Related work

Knowing when a table holds every row it should is an old question in database research.

- Motro [1, 2] separated two properties of a database. It is *valid* when every row it stores is
  true, and *complete* when every true row is stored. dogana's two dates measure them: complete as
  of `c` is completeness at `c`, and deletions as of `d` is validity against a single kind of
  error, keeping a row the source has already deleted.
- Levy [3], then Razniewski and Nutt [4], let a database state that part of a table is complete,
  such as "all orders of 2025", and showed when such statements make a query's answer complete. A
  region is such a statement, with a date.
- Berenson and others [5] analyse the isolation levels of ANSI SQL and the anomalies each allows. At
  READ COMMITTED, the default of most databases, two statements of one transaction can see
  different data, which is why tables read together need one snapshot.

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
| Source | The system that owns the table, such as an ERP, a SaaS API or a database. |
| Read | The rows the source held at one instant, in one shape. |
| Sender | Whoever provides reads from the source and writes their manifests. |
| Receiver | Whoever runs dogana and decides whether to accept each delivery. |
| Reader | Whoever queries the loaded rows and needs to know how far to trust them. |
| Complete as of | Every row the source had at that instant is in the table. |
| Deletions as of | No row the source deleted before that instant is still in the table. |
| Patched | Complete as of a later instant than deletions: rows deleted in between may remain. |
| Window column | A column whose value never changes on a row, usually a date such as `order_date`. |
| Region | A range of the window column, with its two dates. |
| Unknown | The state of a region where nothing has been proven yet. It is not the same as empty. |
| Ledger | The list of a table's regions with their dates. |
| Shape | One kind of read the receiver accepts, such as a full dump or the rows changed since the last read. |
| Checkpoint | How a shape of changes measures where a read starts: a time, a token or a log position. |
| Complete read | A read that holds every current row of what it covers, such as a full dump or a window read. |
| Tombstone | A row in a read that says its key was deleted. Its deleted column is true. |
| Contract | The receiver's description of one table: its key, its columns and its shapes. |
| Manifest | The sender's declaration about one read: its shape, instant, row count and hash. |
| Read boundary | `produced_at`, the instant the source was read. |
| Snapshot | A read that sees the whole source at one instant. |
| Delivery | One read with its manifest, handed to dogana by the loader. |
| Gate | The part of dogana that checks a manifest against the contract and the ledger. |
| Loader | The receiver's code that puts rows into the database: it checks the rows, executes the plan and commits rows with the ledger. |
| Plan | The row operations a receipt returns for the loader to execute. |
| Receipt | The record of the decision on one delivery, whether accepted or refused. |
| Replay | The same manifest sent again. dogana answers with the first receipt and changes nothing. |
| Claims | The state of each region at the moment a reader asks. |
| Expired | The state of a region whose complete as of date is older than the limit the reader gives. |
| Exact at p | Every region has both dates equal to p: the table holds exactly the rows the source had at p. |
| Group | Tables read together, from one snapshot of the source. |
