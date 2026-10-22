# dogana

Customs for data integrations. A Python library that records what each delivery proves about a
table, so every reader knows where a missing row means a deleted row, and as of when.

The core has no dependencies and performs no I/O. It validates declarations, keeps the ledger and
decides what a store must do. Extraction stays with your extractor, rows with your database,
scheduling with your orchestrator.

## The problem

An ERP exports the last 30 days of invoices every morning and the whole table on Sunday. Monday's
file lacks an invoice dated last week: it was deleted, and the file proves it. The same morning an
invoice from March was also deleted at the ERP, but March is outside the window, so the file says
nothing about it. The row stays in your landing table until Sunday.

Every incremental pipeline lives with this. The usual answer is a weekly full refresh and a shrug in
between. What is missing is the record: which parts of the table are complete as of when, which may
still hold deleted rows, and which have never been delivered at all. Without it, "items with no
movement" is computed over rows that should be gone, and nobody can say so.

dogana keeps that record as two dates per region of a table:

- `complete_as_of`: no source row is missing in the region as of that read.
- `valid_as_of`: no deleted row remains in the region as of that read.

A complete window advances both inside the window and nothing outside it. Rows changed since a
timestamp, without a delete signal, advance only completeness. One invariant governs every
transition: **a delivery never raises a date above what it proves**.

## Try it

Python 3.11 or later. The reference store uses DuckDB.

```sh
uv pip install -e '.[dev,duckdb]'
```

The script below writes three CSV files with JSON sidecar manifests into a drop folder and ingests
them: Sunday's full export, Monday's 30-day window, and the next Sunday's full export. Run it in an
empty directory.

```python
import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dogana import Column, Contract, Manifest, Shape
from dogana.adapters.duckdb import DuckDBStore
from dogana.adapters.files import FileDrop

contract = Contract(
    "orders",
    key=("id",),
    shapes={
        "full": Shape("all", "complete", "absence", "none", cost=100),
        "daily": Shape("window", "complete", "absence", "none", cost=10, window_size=timedelta(days=30)),
    },
    window_column="day",
    schema=(Column("id", "int"), Column("day", "date"), Column("amount", "int")),
)

window = (date(2026, 9, 20), date(2026, 10, 19))
deliveries = [
    (18, "full", [(1, "2026-09-01", 100), (2, "2026-10-18", 200)], None),
    (19, "daily", [(3, "2026-10-19", 300)], window),
    (25, "full", [(3, "2026-10-19", 300)], None),
]

drop = Path("drop")
drop.mkdir()
history = []
with DuckDBStore("landing.duckdb") as store:
    for day, shape, rows, coverage in deliveries:
        path = drop / f"orders-{day}.csv"
        with path.open("w", newline="") as stream:
            csv.writer(stream).writerows([("id", "day", "amount"), *rows])
        manifest = Manifest(
            "orders",
            shape,
            datetime(2026, 10, day, tzinfo=timezone.utc),
            window=coverage,
            rows=len(rows),
            schema_hash=contract.schema_hash,
        )
        path.with_suffix(".csv.json").write_text(manifest.to_json())

        receipt = FileDrop(store, contract).ingest(path)
        assert receipt.accepted

        ledger = store.load_ledger(contract)
        claims = ledger.claims(manifest.produced_at)
        history.append((day, claims.complete_as_of.date(), claims.valid_as_of.date()))
        assert [row["id"] for row in store.read("orders")] == {18: [1, 2], 19: [1, 3], 25: [3]}[day]

assert history == [
    (18, date(2026, 10, 18), date(2026, 10, 18)),
    (19, date(2026, 10, 18), date(2026, 10, 18)),
    (25, date(2026, 10, 25), date(2026, 10, 25)),
]
```

Read the history line by line. Sunday 18: a full export, both dates are the 18th everywhere.
Monday 19: the window deleted order 2 (inside the window, absent from the file) and kept order 1
(outside the window, so the file proves nothing about it). Inside the window both dates are now the
19th, but the dates of the whole table stay at the 18th, because the weakest region decides.
Sunday 25: the full export removes order 1 and moves everything to the 25th.

Ask for the window alone and the answer changes:

```python
assert ledger.claims(manifest.produced_at, window=window).valid_as_of.date() == date(2026, 10, 25)
```

Every receipt and every publication stays readable. A file that contradicts its manifest (rows
outside the window, a wrong count, a column gone) is refused, leaves rows and ledger untouched, and
still gets a receipt that says why.

## Pull mode

When you control the read, the ledger runs backwards. Ask what a reader needs and get the cheapest
admitted read that proves it, or the next interval for a rolling shape:

```python
from dogana import Requirement, next_extraction, plan_delivery

until = datetime(2026, 10, 26, tzinfo=timezone.utc)
needed = (date(2026, 10, 1), date(2026, 10, 26))
request = plan_delivery(ledger, Requirement("orders", until, until, window=needed))
assert request.shape == "daily" and request.window == needed

rolling = next_extraction(ledger, "daily", until)
assert rolling.interval == (date(2026, 9, 27), date(2026, 10, 26))
```

The request goes to your extractor. What comes back passes through the same gate as a pushed file,
so push and pull produce the same ledger for the same deliveries.

## The concepts, in the order you meet them

| Term | What it is |
|---|---|
| Contract | One table: key, schema, admitted delivery shapes, optional window column. |
| Shape | How a delivery was produced: coverage (all, window, keys), completeness (complete, changed only), deletion signal (absence, tombstone, none), checkpoint (generation, timestamp, none). Fourteen combinations are valid. |
| Manifest | What one delivery claims: its shape, the source read boundary, window, count, schema hash. Written by the sender as a sidecar, derived from a file name, or produced by your own extraction. |
| Gate | Checks the claim against the contract and the rows against the claim. Accept or reject, never partially. |
| Ledger | Disjoint regions of the key space, each with the two dates. Never delivered regions stay visible as unknown. |
| State | Per region: unknown, complete, patched (deleted rows may remain), expired (older than the freshness limit). |
| Claims | What a reader may rely on, for a table or a set of tables: the weakest dates and the state of every region. |
| Receipt | The audit record: manifest, checks, transition, resulting claims, publication identity. The ledger can be rebuilt from receipts. |

The [protocol](docs/protocol.md) defines every term and transition. It is the authority; the code
follows it.

## Common source patterns

| Source | Shapes | What the ledger says |
|---|---|---|
| Nightly full export | all, complete, absence | Both dates advance everywhere. |
| Daily window plus weekly full | window and all, complete, absence | Both advance only inside the window; the weekly full resets the rest. |
| Rows changed since updated_at, weekly full | all, changed only, none, timestamp | Completeness advances daily; validity waits for Sunday. |
| One export per plant | window with equal endpoints, complete, absence | Each plant has its own dates; undelivered plants are unknown. |
| Change feed with tombstones | keys, changed only, tombstone, generation | Both advance from a continuous base; a broken chain is refused until a full export. |
| Append-only event log, no key | all, changed only, none, timestamp | Contiguous intervals from a declared start. |

## What it is not

- Not a pipeline. It never reads a source. dlt, Airbyte, Spark or a shell script do that.
- Not a scheduler. Dagster or Airflow decide when; dogana says what is due and why.
- Not a quality monitor. Soda or Great Expectations test row values; dogana tracks what a delivery proved.
- Not a store. Rows live in DuckDB, Postgres, Iceberg or DuckLake. The store executes plans the core emits as values.

The reference adapters (DuckDB store, file drop) exist to prove the interfaces, not to be the
product. [Integrations](docs/integrations.md) explains how dogana sits next to dlt, dbt, Iceberg,
DuckLake, Dagster and the observability tools. [Architecture](docs/architecture.md) explains the
design and what it borrows from projects that got these questions right.

## Grounding

Razniewski and Nutt formalise partial databases with table completeness statements: the available
table holds every ideal row satisfying a condition. Motro splits integrity into completeness (no
missing rows) and validity (no rows that should be gone). A row deleted at the source but still in
your table is a validity failure, not a completeness one. That is why there are two dates and not
one.

## Status

Protocol version 1, reference implementation, no release yet. 148 tests cover the transitions, the
gate, JSON round trips, pull planning, and a week of files for three tables through DuckDB. The
README code blocks run in the test suite. Next: a dlt extractor adapter, a DuckLake store, contract
evolution.

## Licence

[Apache-2.0](LICENSE).
