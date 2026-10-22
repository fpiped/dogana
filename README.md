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

Between full refreshes, a reader needs to know which regions may retain deleted rows and which
have never arrived. Otherwise "items with no movement", which asks for items absent from the
movements table, silently drops the items whose last movement is a ghost row.

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
from dogana.reference.duckdb import DuckDBStore
from dogana.reference.files import FileDrop

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

Sunday 18 establishes both dates everywhere. Monday's window deletes order 2 but keeps order 1
outside it. The whole table reports the 18th: the table dates are the earliest region dates, a lower bound
that every region satisfies. Sunday 25 removes
order 1 and moves both dates everywhere to the 25th.

Ask for the window alone and the answer changes:

```python
assert ledger.claims(manifest.produced_at, window=window).valid_as_of.date() == date(2026, 10, 25)
```

Every receipt stays readable. A file that contradicts its manifest (rows
outside the window, a wrong count, a column gone) is refused, leaves rows and ledger untouched, and
still gets a receipt that says why.

## Without a store

An existing loader can use the core alone. Here the published rows, stamp, ledger and receipts
share one in-memory state; replacing that state publishes them together.

```python
from dogana import Check, Gate, Ledger, Operation

memory_contract = Contract(
    "items",
    ("id",),
    {"full": Shape("all", "complete", "absence", "none")},
    schema=(Column("id", "int"),),
)
memory_state = ([], Ledger(memory_contract), (), None)
candidate_rows = [{"id": 1}, {"id": 2}]
memory_manifest = Manifest(
    "items",
    "full",
    datetime(2026, 10, 18, tzinfo=timezone.utc),
    rows=2,
    schema_hash=memory_contract.schema_hash,
)

published_rows, memory_ledger, receipts, stamp = memory_state
assert stamp == memory_ledger.publication_id
memory_manifest.check(memory_contract)
requirements = Gate.requirements(memory_contract, memory_manifest)
results = {
    "row_count": len(candidate_rows) == memory_manifest.rows,
    "schema_hash": memory_manifest.schema_hash == memory_contract.schema_hash
    and all(tuple(row) == ("id",) and type(row["id"]) is int for row in candidate_rows),
    "key_integrity": len({row["id"] for row in candidate_rows}) == len(candidate_rows)
    and all(row["id"] is not None for row in candidate_rows),
}
checks = tuple(Check(r.name, results[r.name]) for r in requirements)
memory_receipt = Gate.evaluate(memory_ledger, memory_manifest, checks)
if memory_receipt.accepted:
    for action in memory_receipt.transition.plan.actions:
        assert action.operation is Operation.REPLACE_ALL
        published_rows = list(candidate_rows)
memory_state = (
    published_rows,
    memory_receipt.ledger,
    receipts + (memory_receipt,),
    memory_receipt.publication_id,
)
assert memory_receipt.accepted and memory_state[0] == candidate_rows
assert Ledger.from_receipts(memory_contract, memory_state[2]) == memory_state[1]
```

A production loader meets four obligations:

- Checks before rows.
- Rows and ledger move together, including stamp and receipt.
- Receipts are kept, including rejections.
- Replay returns the stored receipt.

Run `dogana.conformance.StoreConformance` against your store; [reference](docs/reference.md) shows
the factory and fault hooks. The example above handles a full replacement only.

## The concepts, in the order you meet them

| Term | What it is |
|---|---|
| Contract | One table: key, schema, admitted delivery shapes, optional window column. |
| Shape | How a delivery was produced: coverage (all, window, keys), completeness (complete, changed only), deletion signal (absence, tombstone, none), checkpoint (generation, timestamp, none). Twelve combinations are valid. |
| Manifest | What one delivery claims: its shape, the source read boundary, window, count, schema hash. Written by the sender as a sidecar, derived from a file name, or produced by your own extraction. |
| Gate | Checks the claim against the contract and the rows against the claim. Accept or reject, never partially. |
| Ledger | Disjoint regions of the window column's domain, each with the two dates. Never delivered regions stay visible as unknown. |
| State | Per region: unknown, complete, patched (deleted rows may remain), expired (older than the freshness limit). |
| Claims | What a reader may rely on, for a table or a set of tables: the earliest dates and the state of every region. A query inherits them when it is monotone; under negation the two dates swap roles for the negated input. |
| Receipt | The audit record: manifest, checks, transition, resulting claims, publication identity. The ledger can be rebuilt from receipts. |

The [protocol](docs/protocol.md) defines every term and transition. It is the authority; the code
follows it.

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

The DuckDB store and file drop are reference implementations.
[Integrations](docs/integrations.md) explains how dogana sits next to dlt, dbt, Iceberg, DuckLake,
Dagster and the observability tools. [Architecture](docs/architecture.md) explains the
design and what it borrows from projects that got these questions right.

## Who should not use it

If every source uses log-based CDC with soft deletes, your replica already captures absence.
Both dates would stay equal; this ledger adds little.

## Grounding

Razniewski and Nutt formalise partial databases with table completeness statements: the available
table holds every ideal row satisfying a condition. Motro splits integrity into completeness (no
missing rows) and validity (no rows that should be gone). A row deleted at the source but still in
your table is a validity failure, not a completeness one. That is why there are two dates and not
one.

## Status

Protocol version 1, no release yet. 308 tests cover transitions, JSON, planning, runnable README
examples and store obligations. A conformance suite proves a store; DuckDB and file drop are
reference implementations. Contract evolution remains open.

## Licence

[Apache-2.0](LICENSE).
