# Concepts

One example runs through this page: a shop whose ERP sends its `orders` table as files. The
[protocol](protocol.md) states the same rules exactly, edge cases included.

## The problem

The **source** is the system that owns a table: an ERP, a SaaS API, a database you do not control.
You cannot query it whenever you like. Instead it sends you files, and you load them into your own
database.

Each file is a copy of part of the table at one instant, and each kind of file tells you something
different:

- A full dump of `orders` has every order. An order you loaded before and that is missing now was
  deleted at the source.
- A file of every order dated in the last 30 days tells you the same, for those 30 days only.
- A file of every order changed since yesterday has every new and updated order. It says nothing
  about deleted orders, because a deleted order is no longer there to export.

Once loaded, the rows look the same whichever file brought them. You can no longer tell whether an
order is in your table because the source still has it, or because no file has told you it was
deleted.

dogana keeps a record of what each file proved, beside the rows. It checks every file, updates the
record, and refuses a file that would make the record wrong.

The **sender** is whoever exports the files from the source. The **receiver** runs dogana and
decides which files to accept. dogana never sees the source: it checks each file only against what
the sender declares about it. If the export misses rows the source has, and the declaration counts
only what was exported, no check can tell.

## Two dates

For a table, or for each range of it (see [the window column](#the-window-column)), dogana keeps
two instants:

- **complete as of** (`c`): every row the source had at `c` is in your table.
- **deletions as of** (`d`): no row the source deleted before `d` is still in your table.

On Sunday the source sends a full dump of `orders`. Your table now holds exactly the orders the
source had on Sunday, so both dates are Sunday.

On Monday it sends the orders changed since Sunday. Every new and updated order arrives, so the
table is complete as of Monday. An order deleted on Monday morning is not in the file, so it is
still in your table, and the deletions date stays at Sunday.

`d` is never later than `c`. When both equal an instant `p`, the table holds exactly the rows the
source had at `p`. When `c` is later, the table is **patched**: it may still hold rows the source
deleted between `d` and `c`.

Every decision dogana makes follows one rule: **a file moves a date only as far as the file
proves.**

## The window column

dogana can keep the two dates separately for each range of one column, the **window column**. Its
value never changes on a row; usually it is a date, such as `order_date`. Ranges are needed because
some files cover only part of that column, such as the orders of the last 30 days.

On Sunday, October 25, the source sends a full dump. On Tuesday it sends every order dated October
25 to 27. The table now has three ranges:

| Order dates | Complete as of | Deletions as of |
|---|---|---|
| up to October 24 | Sunday | Sunday |
| October 25 to 27 | Tuesday | Tuesday |
| from October 28 | Sunday | Sunday |

The last range is proven too: Sunday's dump had every order, including any dated in the future.

A range with its two dates is a **region**, and the list of a table's regions is its **ledger**.
Before the first file, the table is one region with no dates, called **unknown**. Unknown means
nothing has been proven there yet; it does not mean empty. A table without a window column is
always one region.

**A row's window value must never change.** Use an order date, an invoice date or an event date,
never an update time. Suppose the window column is `updated_at`, and a September order is edited on
October 20:

- A file of the October range, read before the edit, does not have the order: it was still a
  September row.
- A file of the September range, read after the edit, does not have it either, because its
  `updated_at` is now October 20. dogana removes it as deleted.

The order is now in neither range, and both ranges say they are complete. No file shows the
mistake, so choosing a column that never changes is up to you.

## Shapes

A **shape** is one kind of file the receiver accepts. Shapes differ in three ways:

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
| One complete file per day or per plant | A window whose two ends are equal | Both dates, for that day or plant |
| The rows whose `updated_at` is after the last read | `Shape.changes()` | Complete as of the read; the deletions date does not move |
| The same, plus the keys deleted since | `Shape.changes(deletions="tombstone")` | Both dates, if the read starts no later than the deletions date |
| A change feed with a sync token | `Shape.feed()` | Both dates, if each read starts from the token the previous one issued |
| Change data capture batches by log position | `Shape.feed(checkpoint="sequence")`, after a full snapshot | Both dates, if each batch starts where the previous one ended |
| Events without a key that never change | `Shape.append()` | Both dates, if each read starts where the previous one ended |

Each shape has a **cost**: how expensive that file is to ask the source for. dogana uses it to pick
the cheapest file that gives a reader the dates it needs.

## Deletions

The difficult case is a **physical** deletion, also called a hard delete: the row is gone from the
source, so no later export can show it. A file can report it in two ways.

A **complete** file holds every current row of what it covers, such as a full dump or a window
file. It reports deletions by absence.

A **tombstone** is a row in a file that says its key was deleted. The table's contract (below)
names a boolean column, the **deleted column**. A row where that column is true is a tombstone, and
its other values may be empty:

```text
id,order_date,amount,deleted
7,2026-10-26,120,false
9,,,true
```

Order 7 is a current row; order 9 was deleted. The file's [manifest](#the-manifest) counts the
tombstones, here one, but never lists the keys.

A **soft delete** is a flag, such as `is_deleted`, on a row the source does not remove. It can be
the deleted column only if three things hold:

- setting the flag also moves `updated_at`, so a read of changes picks the row up;
- the source never removes a row physically, not even by archiving or retention purges;
- the flag is on this table, not on another one.

Otherwise declare the shape with `deletions="none"`, and send a full dump from time to time to
remove the deleted rows.

## Writing a contract

A **contract** is the receiver's description of one table. It names:

- the **key**: the columns that identify a row;
- the **window column**, if the source can export ranges of it;
- the **shapes** the source can really produce, each with its cost;
- the **deleted column**, if a shape carries tombstones;
- the **cursor column**: the column a read of changes filters on, such as `updated_at`. The
  manifest declares the interval the read covers, from `since` to `produced_at`, and dogana checks
  that every current row of the file has its cursor value inside it;
- the **column hash**, `schema_hash`: a SHA-256 of the column names and types, computed from
  wherever the columns are defined, such as a data contract, a dbt model or a list of columns. A
  file with other columns is refused.

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
    freshness=timedelta(days=2),
)
```

`types` gives the type of the columns dogana reads: key, window, cursor and deleted. `freshness` is
how old the complete as of date may grow before a reader should stop trusting it: here two days,
for daily files plus some slack. The default is seven days.

Take the files of [the window column](#the-window-column) example:

- Sunday's `full` file proves the whole table as of Sunday.
- Tuesday's `last3` file proves the orders dated October 25 to 27 as of Tuesday.
- On Wednesday a `changes` file arrives with every change since Sunday's read. Its tombstones
  report the deletions, so it proves the whole table as of Wednesday, both dates.

The same file with only the changes since Monday is refused. Most of the table is proven only up to
Sunday's read, so a change made between that read and Monday would be in no file, such as an order
dated October 10 and changed on Sunday evening.

## The manifest

Every file arrives with a **manifest**, the sender's declaration about that one file:

- the table and the shape;
- `produced_at`, the instant the file's data was read from the source (next section);
- the window, for a window file;
- `since`, where a read of changes starts, and for a feed `generation`, the token or position it
  ends at;
- `rows`, the number of current rows, and `tombstones`, the number of tombstones;
- `content_hash`, the SHA-256 of the file's bytes.

It usually travels as a JSON file beside the data file, such as `orders.csv.json`; the
[example manifest](examples/manifest.json) shows one. The [reference](reference.md#file-drop) shows
how to derive it from the file's name instead.

## Which instant to declare

`produced_at`, the file's **read boundary**, is the instant its data was read from the source.
dogana checks only that it is not in the future and is later than the dates it moves. The rest is up
to the sender.

A nightly job is scheduled at midnight. It queries the source at 02:00 and finishes writing the
file at 02:40. The file shows the source at 02:00, so it declares 02:00. For a file of changes,
02:00 is the upper bound of its query, `updated_at <= 02:00`. The other two times are wrong:

- Midnight claims the file has every order the source had at midnight. An order deleted at 01:00
  existed at midnight, but the file, read at 02:00, does not have it.
- 02:40 claims the file has every order the source had at 02:40. An order created at 02:10 is not
  in it.

A **snapshot** is a read that sees the whole source at one instant. One database transaction can be
one, at the right isolation level (see [tables read together](#tables-read-together)). A read that
is not one snapshot, such as an API paged for forty minutes, shows no single instant. Whatever
instant it declares, it is wrong by the rows created or deleted while it ran.

## Delivering a file

A **delivery** is one file with its manifest, handed to dogana. It is decided in three steps:

1. The **gate** checks the manifest against the contract and the ledger. For example, a file of
   changes needs a base: the table must already be proven everywhere the file covers, and the read
   must start no later than the complete as of date there.
2. The **store**, the code that writes to your database, checks the rows against the manifest: for
   example the counts, unique keys, window values inside the window, the column hash, and the bytes
   against `content_hash`.
3. If every check passes, the store publishes the rows, the new ledger and a receipt in one
   transaction: readers see all of them from then on, or none. If any check fails, the whole file
   is refused, rows and ledger stay as they were, and the refusal is recorded.

A **receipt** is the record of one decision: the manifest, the checks and the outcome. dogana can
rebuild the ledger from the accepted receipts. The same manifest sent again is a **replay**: dogana
returns the first receipt and changes nothing, so a job that failed halfway can simply send its file
again.

## Reading

A **reader** is whoever queries the loaded rows. Before trusting them, it asks the ledger for its
**claims**: the state of each region at the reader's current time.

| State | Meaning |
|---|---|
| unknown | Nothing is proven. |
| complete | Both dates are equal, and recent enough for the contract's freshness. |
| patched | Complete as of a later date than deletions, and recent enough. |
| expired | Complete as of a date older than the contract's freshness allows. |

A table's state is its worst region's, in the order unknown, expired, patched, complete. Its dates
are the earliest complete as of and the earliest deletions as of among its regions.

Which date a reader needs depends on the question:

- The rows a query lists ("orders over 1,000 euros") include every one the source had at `c`, when
  every region the query reads is complete as of the same `c`. They may include orders deleted
  since `d`.
- A question with "no" or "not" ("customers with no order in October") also needs the deletions
  date: a deleted order still in the table makes its customer look active.
- An exact count or sum needs both dates.
- Regions or tables proven at different instants guarantee none of this, as the next section
  shows.

`plan_delivery` works the other way: given the dates a reader needs, it names the cheapest file to
ask the source for ([quick start, step 6](quickstart.md#6-ask-for-the-next-read)).

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

**A group.** The receiver declares the tables read together as a **group**. It may add
**references**, rules the source keeps between them, such as "every invoice line names an existing
invoice". `deliver_together` then decides the files as one:

1. it checks each file as if it came alone;
2. it requires the same `produced_at` on every manifest;
3. it checks each reference, when the check can prove something (below);
4. it publishes every table in one transaction, or none.

**When a reference is checked.** Suppose the lines are exact at `p` where this delivery covers them,
and the invoices are complete as of `p` over the whole table. Because the lines are exact at `p`,
every line there existed at the source at `p`. The source keeps the rule, so each line's invoice
also existed at `p`. Because the invoices are complete as of `p`, that invoice is in your table. So
a line whose invoice is missing proves the files are not one snapshot, and every table is refused.

In any other case an honest delivery could fail the check, so dogana skips it and the receipt says
why:

- Lines delivered as changes without tombstones are patched: a line the source deleted with its
  invoice can stay, naming an invoice that is gone.
- If invoices arrive as window files, a range may have been proven yesterday. An invoice created
  today with yesterday's date is missing from it, and a line delivered today can name it.

The check catches only orphans, lines whose invoice is missing. Two separate reads stamped with the
same instant pass it whenever, by chance, no line names a missing invoice. So a reader relies on
`exact_as_of`, not on the check.

**Keeping one instant.** Publishing in one transaction means a reader never sees new lines beside
old invoices. Even so, a reader must read the rows and the ledgers together, from one committed
state of the store, and check `exact_as_of`.

To keep a common instant, every later delivery must leave each table with both dates equal to the
new instant. These files do it:

- a complete file;
- an append of keyless events;
- changes with tombstones whose `since` is no later than the deletions date;
- a feed with tombstones, when both dates were already equal.

A window file does it only inside its window, so a reader that queries only those windows passes
them to `Group.claims`, which then judges only those ranges. Changes without tombstones leave a
table patched, and the group without an instant. For a table with no changes, the sender sends an
empty file of its tombstone changes shape, stamped with the new instant. `plan_together` returns
one read per table, all at one shared instant.

## What it is not

dogana does not capture changes from a database log, schedule jobs, extract data, test data quality
or process streams. It also differs from tools that keep history: SQLMesh records which intervals
a model processed, Iceberg records versions of a table, dbt snapshots record each row's history.
None of them records what the source's files proved.

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
| Sender | Whoever exports files from the source and writes their manifests. |
| Receiver | Whoever runs dogana and decides whether to accept each file. |
| Reader | Whoever queries the loaded rows and needs to know how far to trust them. |
| Complete as of | Every row the source had at that instant is in the table. |
| Deletions as of | No row the source deleted before that instant is still in the table. |
| Patched | Complete as of a later instant than deletions: rows deleted in between may remain. |
| Window column | A column whose value never changes on a row, usually a date such as `order_date`. |
| Region | A range of the window column, with its two dates. |
| Unknown | The state of a region where nothing has been proven yet. It is not the same as empty. |
| Ledger | The list of a table's regions with their dates. |
| Shape | One kind of file the receiver accepts, such as a full dump or the rows changed since the last read. |
| Checkpoint | How a shape of changes measures where a read starts: a time, a token or a log position. |
| Complete file | A file that holds every current row of what it covers, such as a full dump or a window file. |
| Tombstone | A row in a file that says its key was deleted. Its deleted column is true. |
| Contract | The receiver's description of one table: its key, its columns and its shapes. |
| Manifest | The sender's declaration about one file: its shape, instant, row count and hash. |
| Read boundary | `produced_at`, the instant the file's data was read from the source. |
| Snapshot | A read that sees the whole source at one instant. |
| Delivery | One file with its manifest, handed to dogana. |
| Gate | The part of dogana that checks a manifest against the contract and the ledger. |
| Store | The code that checks the rows and writes them to your database. |
| Receipt | The record of the decision on one delivery, whether accepted or refused. |
| Replay | The same manifest sent again. dogana answers with the first receipt and changes nothing. |
| Claims | The state of each region at the moment a reader asks. |
| Expired | The state of a region whose complete as of date is older than the contract's freshness allows. |
| Exact at p | Every region has both dates equal to p: the table holds exactly the rows the source had at p. |
| Group | Tables read together, from one snapshot of the source. |
| Reference | A rule the source keeps between two tables of a group, such as "every invoice line names an existing invoice". |
