# dogana

A Python library that records which source rows a delivery accounts for, and through which date
it accounts for deletions. Use it for full exports, rolling windows and change feeds that leave
old rows behind.

An ERP exports the last 30 days every morning and everything on Sunday. Monday's file proves that
an invoice missing inside those 30 days was deleted. It says nothing about a March invoice that
also disappeared. That row stays in the landing table until a full export removes it.

A **region** is an interval on one declared column, such as invoice date. The ledger, a record of
these regions, keeps two dates for each:

- `complete_as_of`: every source row live in the region at this boundary is present.
- `valid_as_of`: deletions are accounted for through this boundary; later deleted rows may remain.

Equal dates mean the region matches the source at that instant. A complete window advances both
inside its bounds. A keyed change feed without deletions advances completeness alone.

`Claims` reports these dates and a state. Call `ledger.claims(now=...)` with the required current
time: `unknown` means no evidence, `complete` means equal fresh dates, `patched` means newer
completeness than deletion knowledge, and `expired` means completeness exceeds the freshness limit.
The default limit is seven days. Set `freshness` to the feed cadence plus expected arrival slack.
State describes dated evidence, not a promise that today's source has not changed.

Whole-table dates are the earliest region dates, lower bounds on evidence. Different regions or
tables may describe different source instants. `Claims.combine` reports the weakest input dates;
it does not certify a joined result or create a common snapshot.

## Try it

Python 3.11 or later. No release is published yet. With uv and an active virtual environment, run
this installation command **from the source checkout** containing `pyproject.toml`:

```sh
uv pip install -e '.[duckdb]'
```

Then use that environment's Python to run the script below in any empty directory.

A `Contract` declares the table key, schema and allowed delivery shapes. A `Shape` describes
coverage, whether the read is complete, how deletions appear and its checkpoint, the previous read boundary or source cursor. Complete shapes
use missing rows as deletions and need no checkpoint. A `Manifest` declares one read's boundary,
window and row count. The gate checks it against the contract and candidate rows. A store publishes
accepted rows and the ledger together; a `Receipt` records the decision and checks.

This example delivers Sunday's full export, Monday's complete window, then the next Sunday's full
export. It uses fixed receiver times so the result is reproducible. Production callers pass their
actual receiver clock as `now`.

```python
import csv
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from dogana import Column, Contract, Manifest, Shape, deliver
from dogana.reference.duckdb import DuckDBStore

full = Shape(coverage="all", completeness="complete", deletions="absence", checkpoint="none", cost=100)
daily = Shape(
    coverage="window", completeness="complete", deletions="absence", checkpoint="none",
    cost=10, window_size=timedelta(days=30),
)
contract = Contract(
    "orders", key=("id",), shapes={"full": full, "daily": daily}, window_column="day",
    schema=(Column("id", "int"), Column("day", "date"), Column("amount", "int")),
)
window = (date(2026, 9, 20), date(2026, 10, 19))
deliveries = [
    (18, "full", [(1, "2026-09-01", 100), (2, "2026-10-18", 200)], None),
    (19, "daily", [(3, "2026-10-19", 300)], window),
    (25, "full", [(3, "2026-10-19", 300)], None),
]
Path("drop").mkdir()
history = []
with DuckDBStore("landing.duckdb") as store:
    for day, shape, rows, coverage in deliveries:
        path = Path("drop") / f"orders-{day}.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerows([("id", "day", "amount"), *rows])
        boundary = datetime(2026, 10, day, tzinfo=UTC)
        manifest = Manifest(
            "orders", shape, boundary, window=coverage, rows=len(rows),
            schema_hash=contract.schema_hash, content_hash=sha256(path.read_bytes()).hexdigest(),
        )
        receipt = deliver(store, contract, path, manifest, now=boundary)
        assert receipt.accepted and not receipt.replayed
        ledger = store.load_ledger(contract)
        claims = ledger.claims(now=boundary)
        history.append((day, claims.complete_as_of.day, claims.valid_as_of.day))
        ids = sorted(row["id"] for row in store.read("orders", receipt.publication_id))
        assert ids == {18: [1, 2], 19: [1, 3], 25: [3]}[day]
    replay = deliver(store, contract, path, manifest, now=boundary + timedelta(hours=1))
    assert replay.replayed and replay.identity == receipt.identity
    assert replay.received_at == receipt.received_at

assert history == [(18, 18, 18), (19, 18, 18), (25, 25, 25)]
assert ledger.claims(now=boundary, window=window).valid_as_of.day == 25
assert ledger.claims(now=boundary + timedelta(days=8)).state.value == "expired"
print("Success: the window kept the older row; the full export removed it; replay changed nothing.")
```

Monday removes order 2 inside the window and retains order 1 outside it. The aggregate dates stay
at October 18 because that is the oldest regional evidence. Sunday 25 removes order 1 and advances
both dates everywhere.

The default `max_shrink_fraction=0.5` rejects complete deliveries removing more than half the rows
in their coverage. For an intentional larger deletion, change that policy explicitly; `None`
disables it. The guard is a per-delivery limit, not proof that an export is complete.

The manifest is testimony. A truncated file with a self-consistent count can still pass; counting
rows at the receiver does not discover missing source rows. `content_hash` distinguishes corrected
payloads. A different digest at an already accepted source boundary is a new, stale delivery and
is refused, so corrections need a new truthful read boundary. Replays preserve the original receipt
and set `replayed=True`. Receipts also carry receiver `received_at` and optional `source` details.

## Request the next read

A `Requirement` states minimum evidence dates and optional coverage. The planner chooses an admitted
read by relative `cost`, breaking ties by shape name. Here the cheaper window suffices:

```python
from dogana import Requirement, next_extraction, plan_delivery

until = datetime(2026, 10, 26, tzinfo=UTC)
needed = (date(2026, 10, 1), date(2026, 10, 26))
request = plan_delivery(ledger, Requirement("orders", until, until, window=needed))
assert request is not None and request.shape == "daily" and request.window == needed
rolling = next_extraction(ledger, "daily", until)
assert rolling.interval == (date(2026, 9, 27), date(2026, 10, 26))
print("Success: the planner selected a complete window.")
```

Your extractor fulfills the request; its result passes through the same `deliver` function.
A scheduler's logical date is not the source read boundary. Keyed timestamp shapes may declare
`lookback` to overlap reads for late commits; this mitigates bounded lag without proving completeness.
An optional contract `cursor_column` lets the store check row timestamps against the interval.

## Source patterns

A checkpoint is the previous read boundary: a timestamp or an opaque source cursor called a
generation. A tombstone explicitly names a deleted key. Generations are table-wide.

| Source | Declaration | Evidence |
|---|---|---|
| Nightly full export | Complete, all, absence | Both dates advance everywhere. |
| Daily window and weekly full | Complete, window and all, absence | Both advance inside each delivery's coverage. |
| Rows changed since `updated_at` | Changed-only, all, none, timestamp | Completeness advances; deletion knowledge waits. |
| One export per plant | Complete singleton windows | Each plant has its own dates; whole-table claims remain unknown without a full read. |
| Feed with tombstones | Changed-only, keys, tombstone, generation | A continuous exact base remains exact; a broken cursor needs a full reset. |
| Keyless append-only log | Changed-only, all, none, timestamp; `start_at` | The source declares retained events immutable and never deleted, so contiguous appends advance both dates. |

## Reading and extending

For "items with no movement", a retained deleted movement hides an item. With exact items and
patched movements at the same boundary, this anti-join can omit true answers. Negation reverses
which input guarantee matters. Exact aggregates and joins across different dates need more than
the minimum reported date. The [protocol](docs/protocol.md) defines these limits and every transition.

The core uses only the standard library. It emits plans and drives store primitives; reference
packages perform I/O. An existing loader can use the [six-call path](docs/reference.md#without-a-store)
and provide its own atomic publication. Store authors can run `dogana.conformance.StoreConformance`.
See [reference implementations](docs/reference.md), [integrations](docs/integrations.md) and
[architecture](docs/architecture.md).

## What it is not

- SQLMesh tracks processed model intervals; dogana records what incoming source deliveries establish about rows and deletions.
- Iceberg snapshots identify committed table versions; dogana describes source evidence within each version.
- dbt snapshots track row history; dogana records whether the incoming coverage can justify absence as deletion.
- A watermark table stores a last-seen cursor; dogana also tracks coverage, deletion knowledge and refused deliveries.
- A pipeline, scheduler or quality monitor: extraction, timing and business-value rules remain with their owners.

If log-based change data capture already preserves every insert, update and deletion, this ledger
may add little. There is no daemon or command-line interface. DuckDB and file drop are reference
implementations, not a catalog of production warehouse connectors.

## Grounding and status

The two dates adapt table completeness and validity to source history, per region and per read
boundary. They distinguish missing live rows from retained deleted rows. Protocol version 1 is
unreleased; compatible policy changes preserve history, while key and window bindings cannot change.

## Licence

[Apache-2.0](LICENSE).
