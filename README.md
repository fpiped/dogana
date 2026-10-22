<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="docs/assets/wordmark-dark.svg">
<img src="docs/assets/wordmark-light.svg" alt="dogana: know which rows you can trust" width="420">
</picture>
</p>

<p align="center">
<a href="https://github.com/fpiped/dogana/actions/workflows/ci.yml"><img src="https://github.com/fpiped/dogana/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
<a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.11%2B-3776ab" alt="Python 3.11+"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/licence-Apache--2.0-2f6f44" alt="Licence Apache-2.0"></a>
<a href="src/dogana/py.typed"><img src="https://img.shields.io/badge/typing-strict-5a5a5a" alt="Typed"></a>
</p>

# Know which rows you can trust

**dogana** is a ledger of what each data delivery proves about a table. A delivery arrives with a
**manifest** that says what it is and as of when: a full export, a rolling window, rows changed since
a timestamp, a change feed with deletions. A **gate** checks the rows against the manifest and refuses
a contradiction. A **ledger** keeps, for each region of the table, two dates: **complete as of**,
nothing is missing, and **valid as of**, nothing deleted is still here. One rule governs it: a
delivery never raises a date above what it proves.

<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="docs/assets/flow-dark.svg">
<img src="docs/assets/flow-light.svg" alt="delivery, gate, ledger, claims" width="760">
</picture>
</p>

## How it works

dogana is a protocol, with this library as its reference. Two documents carry it.

**The contract** is the receiver's declaration, written once per table: the key, the window column,
their types and the named shapes a delivery may take, each saying what such a delivery proves.
The data contract owns the columns; the delivery contract references their hash as an optional
opaque string. The core neither carries nor computes the full schema.

```python
from datetime import timedelta

from dogana import Contract, Shape

orders = Contract(
    "orders",
    key=("id",),
    shapes={"full": Shape.full(), "daily": Shape.window(size=timedelta(days=30))},
    window_column="day",
    types={"id": "int", "day": "date"},
    schema_hash="orders-columns-v1",
)
```

`types` must name exactly the key, window and optional cursor columns, with no extras.
Here `orders-columns-v1` stands for a hash supplied by the data contract; the
[quick start](docs/quickstart.md) computes one using the reference store's recipe.

**The manifest** travels with each delivery and declares that one read: which shape, as of which
source boundary, which window, how many rows, a hash of the content. On the wire it is plain JSON:

```json
{"kind": "Manifest", "version": 1, "table": "orders", "shape": "daily",
 "produced_at": {"$datetime": "2026-10-19T00:00:00+00:00"},
 "window": [{"$date": "2026-09-20"}, {"$date": "2026-10-19"}],
 "rows": 1, "content_hash": "3b7f...", "since": null, "generation": null,
 "schema_hash": "orders-columns-v1", "keys": [], "tombstones": []}
```

Who writes it depends on who controls the read. A sender that controls it drops the JSON next to
the file (`orders-19.csv.json`). A sender that cannot write one gets a manifest derived by the
receiver from a filename pattern the receiver declares once (`orders-20261019-daily.csv`). An
extractor you run yourself builds it from the read it just made. The protocol does not care who
wrote it, only that the gate checks the rows against it.

**The gate** compares the manifest with the contract (is this shape admitted, does the window fit
the column, is the boundary later than what the ledger already holds) and the rows with the
manifest (the count, the column hash when the contract supplies one, every row inside the window,
keys unique). A contradiction is refused as a whole and nothing is written.

**The ledger** folds each accepted manifest. A complete delivery sets both dates in its coverage.
Rows changed since a timestamp move completeness only. Tombstones move validity back to their
checkpoint. A delivery never raises a date above what it proves.

**The receipt** records the decision, every check, the regions before and after, and the
receiver's clock. The same manifest again returns the stored receipt. The receipts in order rebuild
the ledger, so the ledger is derived state and the receipts are the record.

The [protocol](docs/protocol.md) is the authority for all of this; the code follows it.

## The answer it gives

Most warehouse tables are copies of data someone else owns, delivered in pieces, and every pipeline
forgets what each piece proved the moment the rows are loaded. Deleted rows live on as ghosts until
the next full export; a truncated file quietly empties a table; a report that counts what is absent
runs over rows that no longer exist. dogana turns "can I trust this table today" into a line per
region:

```text
orders     2026-09-20 to 2026-10-19   complete as of 2026-10-19   valid as of 2026-10-19   complete
orders     before 2026-09-20          complete as of 2026-10-18   valid as of 2026-10-18   complete
movements  whole table                complete as of 2026-10-19   valid as of 2026-10-12   patched
```

**Patched** means: rows deleted at the source after October 12 may still be here. That sentence, per
table and per region, is what the library exists to produce.

## In code

The lines above come from two contracts and four deliveries, accounted for by the core alone:

```python
from datetime import UTC, date, datetime, timedelta

from dogana import Contract, Ledger, Shape

at = lambda day: datetime(2026, 10, day, tzinfo=UTC)  # noqa: E731
orders = Contract(
    "orders",
    key=("id",),
    shapes={"full": Shape.full(), "daily": Shape.window(size=timedelta(days=30))},
    window_column="day",
    types={"id": "int", "day": "date"},
)
movements = Contract(
    "movements",
    key=("id",),
    shapes={"full": Shape.full(), "changes": Shape.changes()},
    types={"id": "int"},
)
window = orders.shape("daily").window_ending_at(at(19), "date")
orders_ledger = (
    Ledger(orders)
    .apply(orders.manifest("full", at(18)))
    .apply(orders.manifest("daily", at(19), window=window))
)
movements_ledger = (
    Ledger(movements)
    .apply(movements.manifest("full", at(12)))
    .apply(movements.manifest("changes", at(19), since=at(12)))
)


def show(table, span, claims):
    print(
        f"{table:<9}  {span:<26} complete as of {claims.complete_as_of.date()}"
        f"   valid as of {claims.valid_as_of.date()}   {claims.state.value}"
    )


show("orders", "2026-09-20 to 2026-10-19", orders_ledger.claims(at(19), window=window))
show("orders", "before 2026-09-20", orders_ledger.claims(at(19), window=(date.min, date(2026, 9, 19))))
show("movements", "whole table", movements_ledger.claims(at(19)))
```

The [quick start](docs/quickstart.md) runs the whole story through the DuckDB reference store: a
full export, a window, a truncated file refused, the same file again as a replay, and the next read
the planner asks for.

## Where it runs

Inside whatever already writes your tables: a cron script, an Airflow task, a dlt pipeline. No
server, no daemon, no dependencies in the core. It never reads a source, never schedules, never
transforms. The rows stay in your database and the ledger moves with them in one transaction. A
DuckDB store and a file drop are included as reference implementations; a conformance suite lets
you prove a store for your own warehouse.

After the first release, install with `pip install dogana`.
The core has no dependencies. The `duckdb` extra adds the reference store the quick start uses; nothing in the core imports it.
For the quick start, use `pip install 'dogana[duckdb]'`. No release is on PyPI yet; for now use
`pip install "dogana[duckdb] @ git+https://github.com/fpiped/dogana"` or the
[checkout instructions](docs/quickstart.md).

## Who it is for

Teams that receive data in deliveries: file drops, API extracts, exports from systems they do not
operate, change feeds consumed in batches. Not for teams whose every source arrives through
log-based change data capture with soft deletes: they already hold the answer. Not a pipeline, not
a scheduler, not a quality monitor, not a store, not a stream processor; the
[concepts page](docs/concepts.md) says what it is not, next to what it is.

## Documentation

| Read | For |
|---|---|
| [Quick start](docs/quickstart.md) | The full story in code, with its output, and the next read the planner asks for. |
| [Concepts](docs/concepts.md) | Every term in one table, the source patterns, what it is not, the grounding. |
| [Protocol](docs/protocol.md) | Every transition, check and obligation; the canonical text. |
| [Architecture](docs/architecture.md) | How the code is shaped and why. |
| [Integrations](docs/integrations.md) | dlt, dbt, SQLMesh, Iceberg and DuckLake, Spark, orchestrators, quality tools, micro-batches. |
| [Reference implementations](docs/reference.md) | The DuckDB store, the file drop and the no-store path. |
| [Wire schemas](docs/wire/) | JSON Schema for every value, with identity test vectors in [examples](docs/examples/). |
| [Changelog](CHANGELOG.md) and [Contributing](CONTRIBUTING.md) | What changed and how to work on it. |

## Licence

[Apache-2.0](LICENSE), with a [NOTICE](NOTICE) naming the copyright.
