---
name: dogana
description: How to use dogana to load tables from exports with dates that say how far to trust the rows. Use when writing contracts, manifests, deliveries, claims, groups of tables read together, or a store, and when choosing a shape or a read time for an export.
---

# dogana

dogana tracks how far a table loaded from exported files matches its source. It checks each file
against the declaration that comes with it (the manifest) and the receiver's description of the
table (the contract), and keeps per table two dates: **complete as of** (every row the source had then is here) and
**deletions as of** (no row the source deleted before then is still here). The core is pure
Python with no dependencies and never parses a data file. Docs: https://fpiped.github.io/dogana/,
the specification is `docs/protocol.md`.

## The rules code cannot check

Every one of these passes every check when broken, and makes the dates lie.

1. **`produced_at` is the instant of the source the file shows**: the snapshot's instant, or the
   upper bound a changes query selects (`updated_at <= :until`). Never the scheduler's logical
   date, the run time, or when the file was written. A read that is not one snapshot, such as a
   paged API, shows no single instant.
2. **The window column never changes for a row**: an order, invoice or event date, never
   `updated_at`. "Rows updated in the last N days" is `Shape.changes()`, not a window.
3. **Declare only what the source really does.** A changes read that cannot see physical deletes is
   `deletions="none"`; schedule a periodic `Shape.full()` to heal them. A soft-delete flag is a
   deleted column only if setting it moves `updated_at`, nothing deletes physically, and the flag is
   on the table being loaded.
4. **Counts come from the export**, not from re-reading the file: `rows` live rows, `tombstones`
   deleted ones. Always set `content_hash`, the SHA-256 of the file's bytes; tombstone shapes need
   it.
5. **Tables read together come from one snapshot** (REPEATABLE READ, SERIALIZABLE, an exported
   snapshot, `AS OF` one SCN, SNAPSHOT isolation), every manifest stamped with its instant. READ
   COMMITTED and separate jobs are not one snapshot; giving them one `produced_at` fabricates it.

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
hash as `schema_hash` to refuse files with other columns. Deleted keys travel in the file as rows
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

- `deliver(store, contract, source, manifest, now)`, `now` the receiver's time, publishes rows,
  dates and receipt in one transaction or records a rejection; check `receipt.outcome`. The same
  manifest again is a replay: retry with the same manifest and the same bytes, never re-extract.
- Never load the consumer's table before acceptance; stage the candidate.
- `ledger.claims(now)` gives `state` (unknown, expired, patched, complete) and the two dates; pass
  `window=` for part of the table. A query with `NOT EXISTS` or an anti-join needs the deletions
  date of the negated table.
- `plan_delivery(ledger, Requirement(...))` names the cheapest read for the dates a reader needs;
  `next_extraction(ledger, shape, until)` the next regular read.
- Without a store, a loader calls `Gate.requirements`, answers the checks, `Gate.evaluate`, and
  commits rows, ledger and receipt atomically itself (see the reference's "Without a Store").

## Tables together

`Group((invoices, lines), (Reference("invoice_lines", ("invoice_id",), "invoices"),))` and
`deliver_together(store, group, {"invoices": (rows, manifest), ...}, now)`: every file is checked
alone, all must share one `produced_at`, references are checked when a failure would prove the files
are not one snapshot, and every table is published or none. A reader trusts a join only when
`group.claims(ledgers, now).exact_as_of` is not `None`, reading rows and ledgers under one
publication. Later deliveries must bring each table back to equal dates: complete files, or changes
with tombstones; a table without changes sends an empty changes file at the same instant.

## Stores

The package ships no store. `examples/duckdb` in the repository is an example on DuckDB, not
installed. A store implements the eight methods of `dogana.interfaces.Store` and must pass
`dogana.conformance.StoreConformance` (pytest); publishing rows and dates in separate commits does
not conform.
