# Quick start

Python 3.11 or later. Install with `pip install "dogana[duckdb]"`: the core has no dependencies, and
the `duckdb` extra adds the reference store this quick start uses. From a source checkout, with uv
and an active virtual environment:

```sh
uv pip install -e '.[duckdb]'
```

Run this script in an empty directory. Fixed source and receiver times make the story reproducible;
production callers supply the actual receiver clock as `now`.

The ordered `columns` mapping is the data contract. Its hash covers compact ASCII JSON pairs of
column names and logical types. The delivery contract keeps that hash and only the types its key
and window need. `FileDrop` reads each manifest from its sidecar and uses `columns` to parse CSV.
The `movements` deliveries use row mappings with inferred types and omit the optional schema check.

```python
import csv
import json
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from dogana import Contract, Shape, deliver
from dogana.reference.duckdb import DuckDBStore
from dogana.reference.files import FileDrop

columns = {"id": "int", "day": "date", "amount": "int"}
schema_hash = sha256(
    json.dumps(list(columns.items()), ensure_ascii=True, separators=(",", ":")).encode("ascii")
).hexdigest()
contract = Contract(
    "orders",
    key=("id",),
    shapes={"full": Shape.full(cost=100), "daily": Shape.window(size=timedelta(days=30), cost=10)},
    window_column="day",
    types={"id": "int", "day": "date"},
    schema_hash=schema_hash,
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
        f"   deletions as of {claims.deletions_as_of.date()}   {claims.state.value}"
    )


Path("drop").mkdir()
story = []
with DuckDBStore("landing.duckdb") as store:
    movements = Contract(
        "movements",
        ("id",),
        {"full": Shape.full(), "changes": Shape.changes()},
        types={"id": "int"},
    )
    for day, shape, since in [(12, "full", None), (19, "changes", datetime(2026, 10, 12, tzinfo=UTC))]:
        read_at = datetime(2026, 10, day, tzinfo=UTC)
        batch = movements.manifest(shape, read_at, since=since, rows=1)
        assert deliver(store, movements, [{"id": day, "amount": 100}], batch, now=read_at).accepted
    for label, day, shape, rows, coverage, declared_count in deliveries:
        path = Path("drop") / f"orders-{day}.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerows([tuple(columns), *rows])
        boundary = datetime(2026, 10, day, tzinfo=UTC)
        manifest = contract.manifest(
            shape,
            boundary,
            window=coverage,
            rows=declared_count,
            content_hash=sha256(path.read_bytes()).hexdigest(),
        )
        path.with_suffix(path.suffix + ".json").write_text(manifest.to_json(), encoding="utf-8")
        receipt = FileDrop(store, contract, columns=columns).ingest(path, now=boundary)
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
    replay = FileDrop(store, contract, columns=columns).ingest(path, now=boundary + timedelta(hours=1))
    assert replay.identity == receipt.identity and replay.received_at == receipt.received_at
    print(f"Same file again: {replay.outcome.value}, rows [3]")
    ledger = store.load_ledger(contract)
    claims = ledger.claims(now=boundary)
    print(f"Whole table: complete {claims.complete_as_of.date()}, deletions {claims.deletions_as_of.date()}")
    assert ledger.claims(now=boundary + timedelta(days=8)).state.value == "expired"
```

```text
orders     2026-09-20 to 2026-10-19   complete as of 2026-10-19   deletions as of 2026-10-19   complete
orders     before 2026-09-20          complete as of 2026-10-18   deletions as of 2026-10-18   complete
movements  whole table                complete as of 2026-10-19   deletions as of 2026-10-12   patched
Sunday full: accepted, rows [1, 2]
Monday window: accepted, rows [1, 3]
Truncated full: rejected, rows [1, 3]
Next Sunday full: accepted, rows [3]
Same file again: replayed, rows [3]
Whole table: complete 2026-10-25, deletions 2026-10-25
```

The first three lines are the regions on Monday, the ones the README shows. Monday removes order 2 inside the window and retains order 1 outside it. The next full export
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
