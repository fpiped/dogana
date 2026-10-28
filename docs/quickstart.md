# Quick start

Python 3.11 or later. dogana never reads a data file, so this page publishes through the
repository's example store, a DuckDB database and a file drop outside the package. From a checkout,
with uv and an active virtual environment:

```sh
uv pip install -e . -e examples/duckdb
```

The blocks form one script: run them in order, in an empty directory. Steps 1 to 6 follow a table
of orders through a week of files; 7 and 8 add deletions and a second table.
[Concepts](concepts.md) explains every term.

## 1. Write the contract

```python
import csv
import json
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from dogana import Contract, Requirement, Shape, deliver, next_extraction, plan_delivery
from example_store import DuckDBStore, FileDrop

columns = {"id": "int", "day": "date", "amount": "int"}
schema_hash = sha256(
    json.dumps(list(columns.items()), ensure_ascii=True, separators=(",", ":")).encode("ascii")
).hexdigest()
orders = Contract(
    "orders",
    key=("id",),
    shapes={"full": Shape.full(cost=100), "daily": Shape.window(size=timedelta(days=30), cost=10)},
    window_column="day",
    types={"id": "int", "day": "date"},
    schema_hash=schema_hash,
)
```

`columns` lists every column with its type; the contract keeps its SHA-256, so a file with other
columns is rejected. `id` identifies an order; `day`, its order date, never changes, so the dates
are kept per range of days. The receiver accepts a full export, ten times as expensive to ask for,
and every order dated in the last 30 days.

## 2. Send and receive a file

```python
Path("drop").mkdir()
store = DuckDBStore("landing.duckdb")
drop = FileDrop(store, orders, columns=columns)


def send(name, shape, day, rows, *, window=None, declared=None):
    """Write a CSV and its manifest as a sender would, then let the receiver ingest it."""
    path = Path("drop") / f"{name}.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        csv.writer(stream).writerows([tuple(columns), *rows])
    read_at = datetime(2026, 10, day, tzinfo=UTC)
    manifest = orders.manifest(
        shape,
        read_at,
        window=window,
        rows=len(rows) if declared is None else declared,
        content_hash=sha256(path.read_bytes()).hexdigest(),
    )
    path.with_suffix(".csv.json").write_text(manifest.to_json(), encoding="utf-8")
    receipt = drop.ingest(path, now=read_at)
    print(f"{name}: {receipt.outcome.value}, orders {sorted(row['id'] for row in store.read('orders'))}")
    return receipt


send("sunday", "full", 18, [(1, "2026-09-01", 100), (2, "2026-10-18", 200)])
```

```text
sunday: accepted, orders [1, 2]
```

The store, a DuckDB file, holds the rows, the dates and every decision. The manifest travels beside
the file as `sunday.csv.json`: its shape, the instant the source was read, its rows and the SHA-256
of its bytes. Senders in other languages write it with `dogana manifest`.

## 3. A window file

```python
window = orders.shape("daily").window_ending_at(datetime(2026, 10, 19, tzinfo=UTC), "date")
monday = send("monday", "daily", 19, [(3, "2026-10-19", 300)], window=window)


def show(label, claims):
    print(
        f"{label:<17} complete as of {claims.complete_as_of.date()}, "
        f"deletions as of {claims.deletions_as_of.date()}, {claims.state.value}"
    )


ledger = store.load_ledger(orders)
show("Sep 20 to Oct 19", ledger.claims(monday.received_at, window=window))
show("before Sep 20", ledger.claims(monday.received_at, window=(date.min, date(2026, 9, 19))))
```

```text
monday: accepted, orders [1, 3]
Sep 20 to Oct 19  complete as of 2026-10-19, deletions as of 2026-10-19, complete
before Sep 20     complete as of 2026-10-18, deletions as of 2026-10-18, complete
```

Thirty days ending October 19 start on September 20, both ends included. Inside them the file is
complete, so order 2, dated October 18 and missing, was deleted at the source; order 1, dated
September 1, is outside and stays. The range is proven as of Monday, the rest as of Sunday.

## 4. A truncated file

```python
tuesday = send("tuesday", "full", 20, [], declared=2)
print([check.name for check in tuesday.checks if not check.passed][0])
```

```text
tuesday: rejected, orders [1, 3]
row_count
```

The manifest declares two rows and the file has none, so the whole file is rejected and nothing
changes. A truncated file whose count matches passes the count; the shrink limit then rejects a
complete file that removes more than half the rows it covers.

## 5. The next full export, and the same file twice

```python
send("next-sunday", "full", 25, [(3, "2026-10-19", 300)])
again = send("next-sunday", "full", 25, [(3, "2026-10-19", 300)])
ledger = store.load_ledger(orders)
claims = ledger.claims(again.received_at)
print(f"whole table: complete as of {claims.complete_as_of.date()}, {claims.state.value}")
print(f"eight days later: {ledger.claims(again.received_at + timedelta(days=8)).state.value}")
```

```text
next-sunday: accepted, orders [3]
next-sunday: replayed, orders [3]
whole table: complete as of 2026-10-25, complete
eight days later: expired
```

The full export removes order 1. Delivered again, the same manifest returns the original receipt
and publishes nothing. Claims are read at the reader's time: past the contract's freshness, seven
days by default, the table is expired until a new file arrives.

## 6. Ask for the next read

```python
until = datetime(2026, 10, 26, tzinfo=UTC)
needed = (date(2026, 10, 1), date(2026, 10, 26))
request = plan_delivery(
    ledger, Requirement("orders", complete_as_of=until, deletions_as_of=until, window=needed)
)
print(
    f"to prove {needed[0]} to {needed[1]}: a {request.shape} file for {request.window[0]} to {request.window[1]}"
)
rolling = next_extraction(ledger, "daily", until)
print(f"the next daily file: {rolling.window[0]} to {rolling.window[1]}")
```

```text
to prove 2026-10-01 to 2026-10-26: a daily file for 2026-10-01 to 2026-10-26
the next daily file: 2026-09-27 to 2026-10-26
```

The planner works backwards from what a reader needs: for both dates on October 26 over orders
dated October 1 to 26, the cheapest proof is a window file for those days, at cost 10, not a full
export at 100. `next_extraction` gives the next regular file of a shape: the 30 days ending
October 26. Your job runs the read and declares the instant the source was read
([which instant](concepts.md#which-instant-to-declare)).

## 7. Deliver deletions

```python
stock = Contract(
    "stock",
    ("id",),
    {"full": Shape.full(), "feed": Shape.feed()},
    types={"id": "int", "deleted": "bool"},
    deleted_column="deleted",
)
sunday, monday = datetime(2026, 10, 25, tzinfo=UTC), datetime(2026, 10, 26, tzinfo=UTC)
full = stock.manifest("full", sunday, rows=2, generation="g1")
assert deliver(store, stock, [{"id": 1, "qty": 5}, {"id": 2, "qty": 7}], full, now=sunday).accepted
changes = [{"id": 2, "qty": 9, "deleted": False}, {"id": 1, "qty": None, "deleted": True}]
feed = stock.manifest("feed", monday, since="g1", generation="g2", rows=1, tombstones=1)
receipt = deliver(store, stock, changes, feed, now=monday)
claims = store.load_ledger(stock).claims(receipt.received_at)
print(f"feed: {receipt.outcome.value}, stock {store.read('stock')}")
print(f"stock: {claims.state.value}, both dates {claims.complete_as_of:%A}")
```

```text
feed: accepted, stock [{'id': 2, 'qty': 9}]
stock: complete, both dates Monday
```

A feed is a series of change files, each starting where the previous one ended: Sunday's export
is tagged `g1`, and Monday's feed runs from `g1` to `g2`, so no change between them is missing. A
row whose `deleted` is true deletes its key, and the manifest only counts those rows
(`tombstones=1`). Item 1 is gone, item 2 is updated, and both dates move to Monday.

## 8. Deliver related tables together

```python
from dogana import Group, Reference, deliver_together

invoices = Contract("invoices", ("id",), {"full": Shape.full()}, types={"id": "int"})
lines = Contract("invoice_lines", ("id",), {"full": Shape.full()}, types={"id": "int"})
group = Group((invoices, lines), (Reference("invoice_lines", ("invoice_id",), "invoices"),))


def together(day, heads, items):
    """Both tables read from one consistent snapshot at `day`, then delivered as one."""
    read_at = datetime(2026, 10, day, tzinfo=UTC)
    deliveries = {
        "invoices": (heads, invoices.manifest("full", read_at, rows=len(heads))),
        "invoice_lines": (items, lines.manifest("full", read_at, rows=len(items))),
    }
    receipt = deliver_together(store, group, deliveries, now=read_at)["invoices"]
    rows, ledgers = store.read_together(group)
    instant = group.claims(ledgers, read_at).exact_as_of
    print(
        f"{receipt.outcome.value}: lines {[r['id'] for r in rows['invoice_lines']]}, exact as of {instant:%A}"
    )
    return receipt


together(26, [{"id": 1, "total": 50}], [{"id": 10, "invoice_id": 1, "amount": 50}])
refused = together(
    27,
    [{"id": 1, "total": 50}],
    [{"id": 10, "invoice_id": 1, "amount": 50}, {"id": 11, "invoice_id": 2, "amount": 30}],
)
print(next(check.detail for check in refused.checks if not check.passed))
store.close()
```

```text
accepted: lines [10], exact as of Monday
rejected: lines [10], exact as of Monday
invoice_lines(invoice_id) references invoices: rows without a parent row in invoices: 1, naming invoice_id 2
```

Invoices and their lines are read from one snapshot, so both files carry the same read time, and
are delivered as one. On Monday both are published in one step and are the source as of Monday: a
join answers as the source would have. Tuesday's line 11 names invoice 2, in neither file, so the
files cannot be one instant of a source that keeps every line's invoice: both are rejected, and the
tables stay exactly as of Monday.
