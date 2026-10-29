---
name: dogana
description: How to use dogana to load tables from reads with dates that say how far to trust the rows. Use when writing contracts, manifests, deliveries, claims, groups of tables read together, or a store, and when choosing a shape or a read time.
---

# dogana

dogana tracks how far a table loaded from another system matches that system. Whoever reads the
source declares the read in a manifest; whoever writes the table checks the delivery against that
declaration and the receiver's description of the table (the contract), and dogana decides. It keeps per table two dates: **complete as of** (every row the source had
then is here) and **deletions as of** (no row the source deleted before then is still here). The
core is pure Python with no dependencies and never parses a data file. Docs: https://fpiped.github.io/dogana/,
the specification is `docs/protocol.md`.

## The rules code cannot check

Every one of these passes every check when broken, and makes the dates lie.

1. **`produced_at` is the instant of the source the read shows**: the snapshot's instant, or the
   upper bound a changes query selects (`updated_at <= :until`). Never the scheduler's logical
   date, the run time, or when the delivery was prepared. A read that is not one snapshot, such as a
   paged API, shows no single instant.
2. **The window column never changes for a row**: an order, invoice or event date, never
   `updated_at`. "Rows updated in the last N days" is `Shape.changes()`, not a window.
3. **Declare only what the source does.** A changes read that cannot see physical deletes is
   `deletions="none"`; schedule a periodic `Shape.full()` to heal them. A soft-delete flag is a
   deleted column only if setting it moves the cursor column (usually `updated_at`), nothing deletes
   physically, and the flag is on the table being loaded. Once a table has history, the deleted
   column is fixed like the key: changing it means a new full delivery into a new table.
4. **Counts come from the source read**, not from recounting the delivered rows: `rows` live rows, `tombstones`
   deleted ones. Always set `content_hash`, the SHA-256 of the file's bytes; tombstone shapes need
   it.
5. **Tables read together come from one snapshot** (REPEATABLE READ, SERIALIZABLE, an exported
   snapshot, `AS OF` one SCN, SNAPSHOT isolation), declared in one manifest stamped with its
   instant. READ COMMITTED and separate jobs are not one snapshot; one manifest for them fabricates
   it.

## Contract and manifest

```python
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

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
manifest = orders.manifest(
    "changes",
    datetime(2026, 10, 26, 2, tzinfo=UTC),
    since=datetime(2026, 10, 25, 2, tzinfo=UTC),
    rows=120,
    tombstones=3,
    content_hash=sha256(Path("orders-changes.csv").read_bytes()).hexdigest(),
)
```

`types` lists exactly the key, window, cursor and deleted columns. Pass the data contract's column
hash as `schema_hash` to refuse deliveries with other columns. Deleted keys travel in the read as rows
whose deleted column is true; the manifest only counts them. From a shell or another language:
`dogana manifest FILE --contract contract.json --shape changes --produced-at TIME --rows N`, and
`dogana check FILE --contract contract.json` before handing the file over.

| Source behavior | Shape |
|---|---|
| Full export | `Shape.full()` |
| Every row dated in the last N days, by a fixed date | `Shape.window(size=timedelta(days=N))` |
| Rows changed since the last read, no deletes | `Shape.changes()` |
| The same, with deleted keys | `Shape.changes(deletions="tombstone")` |
| Change feed with a sync token | `Shape.feed()` |
| CDC by log position, after a full snapshot | `Shape.feed(checkpoint="sequence")` |
| Immutable events without a key | `Shape.append()` with the contract's `start_at` |

## Deliver and read

- dogana never reads rows: the loader does, in this order. Look the read's identity and the table
  up among the receipts (an accepted one is a replay: answer with it, read nothing). `Gate.preflight(ledger,
  manifest, now=now)`; on a failure, `Gate.reject`. Check the staged rows against the manifest
  (counts, keys, window, column hash, `content_hash`) and pass the results as `Check` values to
  `Gate.evaluate(ledger, manifest, outcomes, now=now)`. On acceptance execute
  `receipt.transition.plan` and save `ledger.follow(receipt)` and the receipt in the same
  transaction as the rows; on rejection save the receipt alone.
- Never load the consumer's table before acceptance; stage the candidate. Retry with the same
  manifest and the same bytes, never re-extract.
- `ledger.claims(now, limit=...)` gives `state` (unknown, expired, patched, complete) and the two
  dates. `limit` is required: how long ago `complete_as_of` may be before the region is expired, as
  a `timedelta`, or `timedelta.max` to expire nothing. Pass `window=` for part of the table. A query
  with `NOT EXISTS` or an anti-join needs the deletions date of the negated table.
- `plan_delivery(ledger, Requirement(...))` names the cheapest read for the dates a reader needs;
  `next_extraction(ledger, shape, until)` the next regular read.

## Tables together

One snapshot of several tables is one manifest with a part per table:
`Manifest(produced_at, {"invoices": Part(shape="full", rows=10), "invoice_lines": Part(shape="full",
rows=40)}, snapshot="txn-42")`. `Group((invoices, lines)).apply(ledgers, manifest, now=now,
outcomes=checks)`: every table is decided alone, the read must declare every table of the group, and
every table is accepted or every table is rejected; the loader then commits every table's rows and
ledger together, or nothing. A reader trusts a join only when
`group.claims(ledgers, now, limit=timedelta.max).exact_as_of` is not `None`, reading rows and
ledgers under one publication. Later deliveries must bring each table back to equal dates: complete
reads, or changes with tombstones; a table without changes sends an empty read of its tombstone
changes shape at the same instant.

## The loader

The package ships no loader: it depends on your database. What a loader owes, from the protocol's
"The receiver". Rows, ledger and receipt change in one atomic step. A decision made against a ledger
that moved (`receipt.base_publication_id` differs from the published head) is made again. Every
receipt is kept, and the ledger equals `Ledger.from_receipts` of the accepted ones. Readers read
rows and ledger from one committed state. The quick start is a complete loader on SQLite.
