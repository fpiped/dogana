# dogana

Customs for data integrations. A protocol and Python reference library that records what each delivery
proves about a table, so readers know where a missing row means a deleted row, and as of when.

## The problem

An ERP exports the last 30 days daily and the whole table on Sunday. Monday's file omits an invoice
inside that window: it was deleted. An older invoice is also deleted at the ERP, but falls outside the
window. It must remain in the landing table until the next full export. A single "last loaded" timestamp
cannot distinguish those regions.

The ledger keeps two dates per region: **complete_as_of**, no source rows missing as of that read;
`valid_as_of`, no deleted rows retained as of that read. A complete window advances both dates inside
it. Rows changed since a timestamp without a delete signal advance only completeness. The second date
stays at Sunday's full export. A delivery never raises either date above what it proves.

## The concepts

1. A contract names one table, its key, schema and admitted delivery shapes.
2. A shape declares coverage, completeness, deletion signal and checkpoint.
3. A manifest names the shape and claims a source read boundary, window, count and schema hash.
4. The gate checks the declaration and requires the adapter's row checks before accepting it.
5. The ledger records completeness and validity dates in disjoint regions, including unknown gaps.
6. A region is unknown, complete, patched (deletions may remain), or expired by its freshness limit.
7. Claims expose the weakest dates and every region's state; a report combines claims across tables.
8. A receipt records acceptance or rejection, checks, transition, claims and publication identity.

## Run a file drop

Python 3.11 or later. From a checkout, with a virtual environment active:

```sh
uv pip install -e '.[dev,duckdb]'
```

Run the following Python in an empty working directory. It creates a drop folder, three CSV files with
JSON sidecars, and a DuckDB landing database. The files simulate Sunday, Monday and the next Sunday.

```python
import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dogana import Column, Contract, Manifest, Shape
from dogana.adapters.duckdb import DuckDBStore
from dogana.adapters.files import FileDrop

contract = Contract(
    "orders",
    ("id",),
    {
        "full": Shape("all", "complete", "absence", "none", cost=100),
        "daily": Shape("window", "complete", "absence", "none", cost=10, window_size=timedelta(days=30)),
    },
    window_column="day",
    schema=(Column("id", "int"), Column("day", "date"), Column("amount", "int")),
)
drop = Path("drop")
drop.mkdir()
window = (date(2026, 9, 20), date(2026, 10, 19))
deliveries = [
    (18, "full", [(1, "2026-09-01", 100), (2, "2026-10-18", 200)], None),
    (19, "daily", [(3, "2026-10-19", 300)], window),
    (25, "full", [(3, "2026-10-19", 300)], None),
]
history = []
with DuckDBStore("landing.duckdb") as store:
    for day, shape, rows, coverage in deliveries:
        path = drop / f"orders-{day}.csv"
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerows([("id", "day", "amount"), *rows])
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
        ids = [row["id"] for row in store.read("orders")]
        assert ids == {18: [1, 2], 19: [1, 3], 25: [3]}[day]
        if day == 19:
            assert ledger.claims(manifest.produced_at, window=window).valid_as_of.date() == date(2026, 10, 19)

assert history == [
    (18, date(2026, 10, 18), date(2026, 10, 18)),
    (19, date(2026, 10, 18), date(2026, 10, 18)),
    (25, date(2026, 10, 25), date(2026, 10, 25)),
]
```

Monday removes invoice 2 inside the window and retains invoice 1 outside it. Both window dates become
October 19; both whole-table dates remain October 18. The next Sunday removes invoice 1 and advances
both whole-table dates to October 25. All three receipts and publications remain readable. An invalid
file leaves published rows and ledger unchanged and persists a rejected receipt.

CSV and Parquet are supported. Without a sidecar, supply a `FilePattern` with named `shape`,
`produced_at`, `rows` and optional window/checkpoint groups. There is no folder watcher. The caller
chooses arrival order. Exact manifest replay returns the original receipt. Older overlapping deliveries
are refused; cross-partition key moves require a full export. The [protocol](docs/protocol.md) defines
endpoint rules, shrink tolerance, generation recovery and [JSON examples](docs/examples/contract.json).

## Push and pull

Push calls `FileDrop.ingest(path)` or `store.deliver(contract, path, manifest)`. Pull asks the ledger for
an extraction request, passes it to an `Extractor.extract(contract, request)`, and sends the returned
manifest and row source through the same store entry point. The library does not run an extractor.

Continue the example to plan the cheapest read proving deletions for a region on October 26:

```python
from dogana import Requirement, next_extraction, plan_delivery

until = datetime(2026, 10, 26, tzinfo=timezone.utc)
needed = (date(2026, 10, 1), date(2026, 10, 26))
request = plan_delivery(ledger, Requirement("orders", until, until, window=needed))
assert request.shape == "daily"
assert request.window == needed
rolling = next_extraction(ledger, "daily", until)
assert rolling.interval == (date(2026, 9, 27), date(2026, 10, 26))
assert Contract.from_json(contract.to_json()) == contract
```

Costs are declared estimates. A changed-since request starts at the weakest covered completeness date;
a generation feed continues the held token. A broken chain requires a complete base. Tombstones cannot
repair deletions missed during an earlier patched interval.

## Common source patterns

| Source | Shapes | Result |
|---|---|---|
| Nightly full export | all, complete, absence, none | Both dates advance everywhere. |
| Daily 30-day window plus weekly full | window and all, complete, absence, none | Both advance only in covered regions. |
| Weekly full plus changed since updated_at | all, changed_only, none, timestamp | Completeness advances; validity waits for full. |
| Per-plant or company-code export | window with equal endpoints, complete, absence, none | Independent regional dates; undelivered regions unknown. |
| Change feed with tombstones | keys, changed_only, tombstone, generation | Both advance from a clean, continuous base. |
| Append-only log without a key | all, changed_only, none, timestamp | Contiguous appends from a declared history start. |

## Boundaries and integration points

The core has no runtime dependencies and performs no I/O. It declares operations as values and leaves
rows to a store. Extraction, scheduling and business quality rules stay with their existing tools.
DuckDB and file drop are the implemented adapters; the following are integration boundaries, not
packaged connectors:

- [dlt](https://dlthub.com/docs/general-usage/incremental/cursor) extracts and loads; dogana owns delivery state, supplies intervals through `end_value` or `TimeIntervalContext`, and selects the [merge](https://dlthub.com/docs/general-usage/merge-loading) via its apply plan.
- [Airbyte](https://docs.airbyte.com/platform/using-airbyte/core-concepts/sync-modes) reads sources and writes destinations; an adapter declares the coverage and delete guarantees of the configured sync.
- [Iceberg](https://iceberg.apache.org/docs/latest/api/) owns table snapshots; a store adapter must coordinate evidence with its atomic publication.
- [DuckLake](https://ducklake.select/docs) owns rows and catalog snapshots; its store adapter remains to be implemented.
- [Dagster](https://dagster.io/docs/guides/automate/schedules) schedules extraction and publication; dogana supplies the required shape and interval.
- [Soda](https://docs.soda.io/) checks business data expectations; dogana accounts for delivery coverage and deletion knowledge.

A dlt adapter must translate endpoint conventions explicitly: dlt defaults to an inclusive start and
exclusive end; complete dogana windows include both endpoints. A primary-key merge alone cannot delete
absent rows from an entire window, especially an empty one. Publication must include bounded deletion.

## Grounding, status and roadmap

[Razniewski and Nutt's table completeness statements](https://www.vldb.org/pvldb/vol4/p749-razniewski.pdf)
express which parts of a relation contain every source row.
[Motro's integrity model](https://citeseerx.ist.psu.edu/document?doi=f167154daba7fd9234fe1375af3bee7e03c5444e&repid=rep1&type=pdf)
separates completeness from validity. dogana applies that distinction to delivery evidence with two
dates. Combining table claims is conservative; this library does not prove completeness of arbitrary SQL.

Experimental reference implementation, protocol version 1. Tests cover overlapping regions, failed
checks, feed recovery, pull planning and transactional file delivery across three tables. README code
blocks run in tests. The DuckDB adapter copies historical publications and assumes serialized delivery;
it is not a scalable retention system. Next: source adapters, table-format publication and retention
policies, and contract evolution. Stable window values and honest source read boundaries are explicit
source guarantees.

## Licence

[Apache-2.0](LICENSE).
