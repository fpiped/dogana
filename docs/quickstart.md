# Quick start

You need Python 3.11 or later. dogana itself never reads a data file, so this page uses the
example store from the repository: it keeps the tables in a DuckDB database and picks up files from
a folder. From a checkout of the repository (`git clone https://github.com/fpiped/dogana`), with
uv and an active virtual environment (`uv venv`, then `source .venv/bin/activate`):

```sh
uv pip install -e . -e examples/duckdb
```

The blocks form one script: run them in order, in an empty directory. Steps 1 to 6 follow a table
of orders through a week of files. Steps 7 and 8 add deletions and a second table.
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

`columns` lists every column with its type. `schema_hash` is the SHA-256 of that list as compact
JSON, the protocol's recipe. The example store computes the same hash for each file, so a file with
other columns is rejected. `id` is the key. `day`, the order date, never changes on an order, so it
is the window column: dogana keeps the two dates separately for each range of days. `types` lists
only the columns dogana itself reads, here the key and the window column.

The contract accepts two shapes, or kinds of file: `full`, a full export, and `daily`, every order
dated in the 30 days up to the read. Costs are weights the planner compares in step 6: a full
export costs ten times a daily file.

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
the file as `sunday.csv.json`. It declares the shape, the instant the source was read, the number of
rows and the SHA-256 of the file's bytes. Senders in other languages write it with
`dogana manifest`.

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

The 30 days ending October 19 start on September 20, both ends included. The file has every order
dated in those days. Order 2, dated October 18, is missing from it, so it was deleted at the source
and dogana removes it. Order 1, dated September 1, is outside the window and stays. The window is
now proven as of Monday, October 19, and the rest of the table as of Sunday, October 18.
`ledger.claims(now, window=...)` returns what the ledger proves about a range of days at the
instant `now`: the two dates and a state.

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
changes. A truncated file whose manifest declared a matching count would pass: the count must come
from the export, computed in the same read, never from counting the file.

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

The full export removes order 1, which is no longer in it. Sent again, the same manifest gets back
its first receipt, and nothing is written twice. Claims are evaluated at the instant you ask. The
contract's freshness, seven days by default, is how old the complete as of date may be. Eight days
after October 25 it is older than that, so the table is expired until a new file arrives.

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

`plan_delivery`, the planner, starts from what a reader needs: both dates at October 26, for orders
dated October 1 to 26. The cheapest file that proves it is a window file for exactly those days, at
cost 10, not a full export at cost 100. `next_extraction` follows the schedule instead: the next
file of a shape at its usual size, the 30 days ending October 26. Neither function reads the source.
Your extraction job runs the read and declares in the manifest the instant it read the source
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

`deliver` hands the store rows, here Python dictionaries instead of a file, with their manifest.
A feed is a series of change files, each starting where the previous one ended. Its position is a
generation, a token the source issues with each read: Sunday's export carries `g1`, and Monday's
file declares `since="g1"` and its new generation `g2`, so no change between them is missing. A row
whose `deleted` is true is a tombstone: it deletes its key. The manifest counts those rows
(`tombstones=1`) but does not list them. Item 1 is gone and item 2 is updated. Both dates move to
Monday, because the feed starts where Sunday's export ended and reports its deletions.

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

A group names tables read together. A reference states a rule the source keeps between them:
every `invoice_lines.invoice_id` names an `invoices` key. Invoices and their lines are read in one
snapshot, so both manifests carry the same instant, and `deliver_together` decides them as one.

On Monday both are published in one step. Both are exact as of Monday, meaning both dates equal
Monday everywhere, so a join between them gives the source's answer. On Tuesday, line 11 names
invoice 2, but the invoices file has no invoice 2. The source keeps every line's invoice, so one
snapshot of it can never hold a line without its invoice: the two files are not one snapshot. Both
are rejected, and the tables stay as of Monday.
