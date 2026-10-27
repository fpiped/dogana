# Quick start

Python 3.11 or later. Install with `pip install "dogana[duckdb]"`: the core has no dependencies, and
the `duckdb` extra adds the reference store this quick start uses. From a source checkout, with uv
and an active virtual environment:

```sh
uv pip install -e '.[duckdb]'
```

The blocks below form one script: run them in order, in an empty directory. They follow a table
of orders through one week of files. [Concepts](concepts.md) explains every term used here.

## 1. Write the contract

```python
import csv
import json
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from dogana import Contract, Requirement, Shape, deliver, next_extraction, plan_delivery
from dogana.reference.duckdb import DuckDBStore
from dogana.reference.files import FileDrop

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

`columns` lists every column of the table with its type. The contract keeps a fingerprint of that
list (its SHA-256 hash), so a file with other columns is rejected. Each order is identified by
`id`; `day` is its order date, which never changes, so the dates can be kept per range of days.
The receiver accepts two kinds of file: a full export, and every order dated in the last 30 days.
The costs say a full export is ten times as expensive to ask for.

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

The store is a DuckDB file that holds the rows, the dates and every decision. The manifest travels
beside the file as `sunday.csv.json`: its shape, the instant the source was read, how many rows,
and the SHA-256 of the file's bytes. Senders in other languages write it with `dogana manifest`.

## 3. A window file

```python
window = orders.shape("daily").window_ending_at(datetime(2026, 10, 19, tzinfo=UTC), "date")
monday = send("monday", "daily", 19, [(3, "2026-10-19", 300)], window=window)


def show(label, claims):
    print(
        f"{label:<17} complete as of {claims.complete_as_of.date()}, "
        f"deletions as of {claims.deletions_as_of.date()}, {claims.state.value}"
    )


show("Sep 20 to Oct 19", monday.ledger.claims(monday.received_at, window=window))
show("before Sep 20", monday.ledger.claims(monday.received_at, window=(date.min, date(2026, 9, 19))))
```

```text
monday: accepted, orders [1, 3]
Sep 20 to Oct 19  complete as of 2026-10-19, deletions as of 2026-10-19, complete
before Sep 20     complete as of 2026-10-18, deletions as of 2026-10-18, complete
```

Thirty days ending October 19 start on September 20, both ends included. Inside that range the file
is complete, so order 2, dated October 18 and missing from it, was deleted at the source. Order 1,
dated September 1, is outside the range and stays. The range is proven as of Monday, the rest of
the table still as of Sunday.

## 4. A truncated file

```python
tuesday = send("tuesday", "full", 20, [], declared=2)
print([check.name for check in tuesday.checks if not check.passed][0])
```

```text
tuesday: rejected, orders [1, 3]
row_count
```

The manifest declares two rows and the file has none, so the row count fails and the whole file is
rejected: no row and no date changes. A file whose count matches but which is still truncated can
pass the count; the contract's shrink limit then rejects a complete file that removes more than half
the rows it covers.

## 5. The next full export, and the same file twice

```python
send("next-sunday", "full", 25, [(3, "2026-10-19", 300)])
again = send("next-sunday", "full", 25, [(3, "2026-10-19", 300)])
claims = again.ledger.claims(again.received_at)
print(f"whole table: complete as of {claims.complete_as_of.date()}, {claims.state.value}")
print(f"eight days later: {again.ledger.claims(again.received_at + timedelta(days=8)).state.value}")
```

```text
next-sunday: accepted, orders [3]
next-sunday: replayed, orders [3]
whole table: complete as of 2026-10-25, complete
eight days later: expired
```

The full export removes order 1. Delivered again, the same manifest returns the original receipt
and publishes nothing. Claims are read at the reader's time: after the contract's freshness, seven
days by default, the table is expired until a new file arrives.

## 6. Ask for the next read

```python
until = datetime(2026, 10, 26, tzinfo=UTC)
needed = (date(2026, 10, 1), date(2026, 10, 26))
request = plan_delivery(
    again.ledger, Requirement("orders", complete_as_of=until, deletions_as_of=until, window=needed)
)
print(
    f"to prove {needed[0]} to {needed[1]}: a {request.shape} file for {request.window[0]} to {request.window[1]}"
)
rolling = next_extraction(again.ledger, "daily", until)
print(f"the next daily file: {rolling.window[0]} to {rolling.window[1]}")
```

```text
to prove 2026-10-01 to 2026-10-26: a daily file for 2026-10-01 to 2026-10-26
the next daily file: 2026-09-27 to 2026-10-26
```

The planner works backwards from what a reader needs. Asked for both dates on October 26 for orders
dated October 1 to 26, it chooses the cheapest kind of file that proves them: a window file for just
those days, at cost 10, rather than a full export at cost 100. `next_extraction` instead gives the
next regular file of a shape: the full 30 days ending October 26. Your job runs the read and delivers
it; the manifest's read time must be when the source was actually read, never a scheduler's date.

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
claims = receipt.claims
print(f"feed: {receipt.outcome.value}, stock {store.read('stock')}")
print(f"stock: {claims.state.value}, both dates {claims.complete_as_of:%A}")
```

```text
feed: accepted, stock [{'id': 2, 'qty': 9}]
stock: complete, both dates Monday
```

A feed is a series of change files, each starting where the previous one ended. The Sunday export is
tagged `g1`; Monday's feed says it starts at `g1` and ends at `g2`, so no change between them is
missing. It tells its deletions in its own rows: the contract names the boolean column `deleted`,
and a row whose `deleted` is true deletes its key. The manifest only counts those rows
(`tombstones=1`), so it stays small however many keys are deleted. Item 1 is gone, item 2 is
updated, and both dates move to Monday.

## 8. Deliver related tables together

```python
from dogana import Group, Reference, deliver_together

invoices = Contract("invoices", ("id",), {"full": Shape.full()}, types={"id": "int"})
lines = Contract("invoice_lines", ("id",), {"full": Shape.full()}, types={"id": "int"})
group = Group((invoices, lines), (Reference("invoice_lines", ("invoice_id",), "invoices"),))


def together(day, heads, items):
    """Both tables read in one source transaction at `day`, then delivered as one."""
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
invoice_lines(invoice_id) references invoices: rows without a parent row in invoices: 1
```

Invoices and their lines are read in one source transaction, so both files carry the same read
time, and they are delivered as one. On Monday both tables are published in one step, and both are
the source as of Monday: a join between them answers as the source would have. Tuesday's line 11
names invoice 2, which is in neither file, so the two files cannot be one instant of the source:
both are rejected, and the tables stay exactly as of Monday.
