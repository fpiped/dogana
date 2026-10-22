# dogana

**Know which rows you can trust.**

[![CI](https://github.com/fpiped/dogana/actions/workflows/ci.yml/badge.svg)](https://github.com/fpiped/dogana/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab)](pyproject.toml)
[![Licence Apache-2.0](https://img.shields.io/badge/licence-Apache--2.0-2f6f44)](LICENSE)
[![Typed](https://img.shields.io/badge/typing-strict-5a5a5a)](src/dogana/py.typed)

A ledger of what each data delivery proves about a table: two dates per region, a gate that refuses
contradictions, receipts you can audit. Pure Python, no dependencies in the core.

Most tables in a warehouse are copies of data someone else owns, delivered in pieces: a full dump now and then, a rolling window every day, "rows changed since yesterday" from an API, a change feed that names what it touched. Each delivery proves something different about the table, and every pipeline forgets it the moment the rows are loaded. So rows deleted at the source live on as ghosts until the next full dump, a truncated file quietly empties a table, and a report that counts what is absent is computed over rows that no longer exist. When someone asks "can I trust this table today", the honest answer is a shrug.

dogana replaces the shrug with a record. Every delivery states what it is and as of when. A gate refuses the ones that contradict themselves: a wrong count, a row outside its window, a column gone. A ledger keeps, for each part of a table, two dates: complete as of (nothing is missing) and valid as of (nothing deleted is still here). A reader gets those dates and a state, unknown, complete, patched or expired, for a table, a region, or the whole set of tables a report reads:

```text
orders     2026-09-20 to 2026-10-19   complete as of 2026-10-19   valid as of 2026-10-19   complete
orders     before 2026-09-20          complete as of 2026-10-18   valid as of 2026-10-18   complete
movements  whole table                complete as of 2026-10-19   valid as of 2026-10-12   patched
```


Patched means: rows deleted at the source after October 12 may still be here. That sentence, per table and per region, is what the library exists to produce. One rule governs everything it records: a delivery never raises a date above what it proves.

It runs inside whatever already writes your tables: a cron script, an Airflow task, a dlt pipeline. No server, no daemon, no dependencies in the core. It never reads a source, never schedules, never transforms. The rows stay in your database and the ledger moves with them in one transaction. A DuckDB reference store makes the example below run in a minute; a conformance suite lets you prove a store for your own warehouse.

For teams that receive data in deliveries: file drops, API extracts, exports from systems they do not operate. If every source you have arrives through log-based change data capture with soft deletes, you already hold the answer and do not need this.

## Try it

Python 3.11 or later. No release is on PyPI yet; install from GitHub with
`pip install "dogana[duckdb] @ git+https://github.com/fpiped/dogana"`, or, with uv and an active
virtual environment, from the source checkout containing `pyproject.toml`:

```sh
uv pip install -e '.[duckdb]'
```

Run this script in an empty directory. Fixed source and receiver times make the story reproducible;
production callers supply the actual receiver clock as `now`.

```python
import csv
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from dogana import Column, Contract, Shape, deliver
from dogana.reference.duckdb import DuckDBStore

contract = Contract(
    "orders",
    key=("id",),
    shapes={"full": Shape.full(cost=100), "daily": Shape.window(size=timedelta(days=30), cost=10)},
    window_column="day",
    schema=(Column("id", "int"), Column("day", "date"), Column("amount", "int")),
)
window = contract.shape("daily").window_ending_at(datetime(2026, 10, 19, tzinfo=UTC), "date")
deliveries = [
    ("Sunday full", 18, "full", [(1, "2026-09-01", 100), (2, "2026-10-18", 200)], None, 2),
    ("Monday window", 19, "daily", [(3, "2026-10-19", 300)], window, 1),
    ("Truncated full", 20, "full", [], None, 2),
    ("Next Sunday full", 25, "full", [(3, "2026-10-19", 300)], None, 1),
]


def show(table, span, claims):
    print(
        f"{table:<9}  {span:<26} complete as of {claims.complete_as_of.date()}"
        f"   valid as of {claims.valid_as_of.date()}   {claims.state.value}"
    )


Path("drop").mkdir()
story = []
with DuckDBStore("landing.duckdb") as store:
    movements = Contract(
        "movements",
        ("id",),
        {"full": Shape.full(), "changes": Shape.changes()},
        schema=(Column("id", "int"), Column("amount", "int")),
    )
    for day, shape, since in [(12, "full", None), (19, "changes", datetime(2026, 10, 12, tzinfo=UTC))]:
        read_at = datetime(2026, 10, day, tzinfo=UTC)
        batch = movements.manifest(shape, read_at, since=since, rows=1)
        assert deliver(store, movements, [{"id": day, "amount": 100}], batch, now=read_at).accepted
    for label, day, shape, rows, coverage, declared_count in deliveries:
        path = Path("drop") / f"orders-{day}.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerows([("id", "day", "amount"), *rows])
        boundary = datetime(2026, 10, day, tzinfo=UTC)
        manifest = contract.manifest(
            shape,
            boundary,
            window=coverage,
            rows=declared_count,
            content_hash=sha256(path.read_bytes()).hexdigest(),
        )
        receipt = deliver(store, contract, path, manifest, now=boundary)
        ids = sorted(row["id"] for row in store.read("orders"))
        story.append(f"{label}: {receipt.outcome.value}, rows {ids}")
        if day == 19:
            monday = store.load_ledger(contract)
            show("orders", "2026-09-20 to 2026-10-19", monday.claims(boundary, window=window))
            show("orders", "before 2026-09-20", monday.claims(boundary, window=(date.min, date(2026, 9, 19))))
            show("movements", "whole table", store.load_ledger(movements).claims(boundary))
        if not receipt.accepted:
            assert any(check.name == "row_count" and not check.passed for check in receipt.checks)
        assert ids == {18: [1, 2], 19: [1, 3], 20: [1, 3], 25: [3]}[day]
    print("\n".join(story))
    replay = deliver(store, contract, path, manifest, now=boundary + timedelta(hours=1))
    assert replay.identity == receipt.identity and replay.received_at == receipt.received_at
    print(f"Same file again: {replay.outcome.value}, rows [3]")
    ledger = store.load_ledger(contract)
    claims = ledger.claims(now=boundary)
    print(f"Whole table: complete {claims.complete_as_of.date()}, valid {claims.valid_as_of.date()}")
    assert ledger.claims(now=boundary + timedelta(days=8)).state.value == "expired"
```

```text
Sunday full: accepted, rows [1, 2]
Monday window: accepted, rows [1, 3]
Truncated full: rejected, rows [1, 3]
Next Sunday full: accepted, rows [3]
Same file again: replayed, rows [3]
Whole table: complete 2026-10-25, valid 2026-10-25
```

Monday removes order 2 inside the window and retains order 1 outside it. The next full export
removes order 1. The refused file changes no rows or dates.

The default shrink limit also refuses removal of more than half the rows in complete coverage.
`max_shrink_fraction=None` disables that guard. Neither counting received rows nor a shrink limit
proves source completeness: a truncated file with a self-consistent count can still pass.
A corrected file needs a new `content_hash` and a later truthful source boundary.

## Request the next read

The planner chooses the cheapest admitted shape that can establish the required dates.
Here a window suffices:

```python
from dogana import Requirement, next_extraction, plan_delivery

until = datetime(2026, 10, 26, tzinfo=UTC)
needed = (date(2026, 10, 1), date(2026, 10, 26))
request = plan_delivery(ledger, Requirement("orders", until, until, window=needed))
assert request is not None and request.shape == "daily" and request.window == needed
rolling = next_extraction(ledger, "daily", until)
assert rolling.interval == (date(2026, 9, 27), date(2026, 10, 26))
print("Next read: complete daily window, October 1 through October 26.")
```

```text
Next read: complete daily window, October 1 through October 26.
```

Your extractor fulfills the request and passes its result through `deliver`. A scheduler's logical
date is not the source read boundary. Keyed timestamp reads can use `lookback` for bounded lag;
`cursor_column` enables row checks against the declared interval.

## Concepts

| Concept | Meaning |
|---|---|
| Contract | Receiver declaration of table, key, schema, admitted shapes and policy. |
| Shape | Coverage, completeness, deletion signal and checkpoint; helpers include `full`, `window`, `changes`, `feed` and `append`. |
| Manifest | One source read's shape, boundary, coverage, count and optional content digest. |
| Gate | Fixed checks that turn a manifest and row outcomes into a publication decision. |
| Ledger | Evidence for the table, rebuilt from accepted receipts. |
| Region | An interval on one declared window column, including explicitly unknown space. |
| The two dates | `complete_as_of`: no live source row missing; `valid_as_of`: deletions accounted for through that boundary. |
| State | Unknown, complete, patched or expired, evaluated at the reader's `now`; default freshness is seven days. |
| Claims | Regional evidence and weakest input dates; neither a common snapshot nor a guarantee about a joined result. |
| Receipt | Accepted, rejected or replayed outcome, checks, receiver time and resulting evidence. |
| Store | Holds and checks candidates, then publishes rows and evidence atomically against the expected head. |

## Source patterns

| Source | Shape | Evidence |
|---|---|---|
| Nightly full export | `Shape.full()` | Both dates advance everywhere. |
| Daily window, weekly full | `Shape.window(size=...)` and `Shape.full()` | Both dates advance inside delivered coverage. |
| Changes since `updated_at` | `Shape.changes()` | Completeness advances; deletion knowledge waits. |
| One export per plant | Complete singleton windows | Each plant has dates; undelivered space stays unknown. |
| Cursor feed with tombstones | `Shape.feed()` | Continuous changes and deleted keys preserve an exact base. |
| Immutable keyless history | `Shape.append()` with `start_at` | Contiguous intervals advance both dates. |
| A change feed consumed in offset batches | `Shape.feed()` after a snapshot | Each committed range preserves an exact base. |

Window membership must be immutable at the source. A generation is an opaque table-wide cursor;
a tombstone names a deleted key. The [shape decision table](docs/protocol.md#choosing-a-shape)
details what each pattern cannot prove.

## What it is not

- SQLMesh tracks processed model intervals; dogana records incoming source evidence and deletion coverage.
- Iceberg snapshots identify committed table versions; dogana describes source evidence within a version.
- dbt snapshots retain row history; dogana decides when incoming absence justifies deletion.
- A watermark table records a cursor; dogana also records coverage, deletion knowledge and refused files.
- CDC transports changes; dogana accounts for the evidence a delivered batch establishes.
- Not a stream processor: it accounts for bounded deliveries, and a micro-batch committed from a stream is one of them. No watermarks, no event-time windows.

## Reading and extending

For "items with no movement", retained deleted movements hide true answers. Negation reverses
which input guarantee matters; exact aggregates and mixed-date joins need more than minimum dates.
The [protocol](docs/protocol.md) defines the semantics and [wire schemas](docs/wire/) describe JSON.

Store authors can run `dogana.conformance.StoreConformance`; stamped recovery is optional.
See [reference implementations](docs/reference.md), [integrations](docs/integrations.md) and
[architecture](docs/architecture.md). Quality tools run business rules outside the fixed gate.

## Documentation

| Read | For |
|---|---|
| [Protocol](docs/protocol.md) | Every term, transition, check and obligation; the canonical text. |
| [Architecture](docs/architecture.md) | How the code is shaped and why, in ten sentences and their reasons. |
| [Integrations](docs/integrations.md) | dlt, dbt, SQLMesh, Iceberg and DuckLake, Spark, orchestrators, quality tools, micro-batches. |
| [Reference implementations](docs/reference.md) | The DuckDB store, the file drop and the no-store path. |
| [Wire schemas](docs/wire/) | JSON Schema for every value, with identity test vectors in [examples](docs/examples/). |
| [Changelog](CHANGELOG.md) and [Contributing](CONTRIBUTING.md) | What changed and how to work on it. |

## Grounding and status

The dates adapt table completeness and validity to regions and source history: missing live rows
and retained deleted rows are different failures. Protocol version 1 is unreleased. Compatible
policy changes preserve history; key and window bindings cannot change. DuckDB and file drop are
executable reference implementations, not production warehouse support.

## Licence

[Apache-2.0](LICENSE).
