# Quick start

You need Python 3.11 or later and nothing else: dogana never reads a row or a file, so this page
also writes the **loader**, the receiver's code that does, on SQLite from the standard library.
From a checkout of the repository (`git clone https://github.com/fpiped/dogana`), with uv and an
active virtual environment (`uv venv`, then `source .venv/bin/activate`):

<!-- illustration: checkout installation; the suite installs the package before running examples -->
```sh
uv pip install -e .
```

The blocks form one script: run them in order, in an empty directory. Steps 1 to 6 follow a table
of orders through a week of files. A query result or an API response is delivered the same way,
with the same manifest. Steps 7 and 8 add deletions and a second table.
[Concepts](concepts.md) explains every term.

## 1. Write the contract

```python
import csv
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from dogana import Check, Contract, Gate, Ledger, Operation, Outcome, Receipt, Shape

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
JSON, the protocol's recipe; the loader below computes the same hash from each file, so a file with
other columns is rejected. `id` is the key. `day`, the order date, never changes on an order, so it
is the window column: dogana keeps the two dates separately for each range of days. `types` lists
only the columns dogana itself reads, here the key and the window column.

The contract accepts two shapes, or kinds of file: `full`, a full export, and `daily`, every order
dated in the 30 days up to the read. Costs are weights the planner compares in step 6: a full
export costs ten times a daily file.

## 2. Write the loader

```python
db = sqlite3.connect("landing.sqlite")
db.executescript(
    "create table orders (id integer, day text, amount integer);"
    "create table stock (id integer, qty integer);"
    "create table dogana_ledgers (table_name text primary key, document text);"
    "create table dogana_receipts (identity text primary key, accepted integer, document text);"
)


def ledger_of(contract):
    row = db.execute("select document from dogana_ledgers where table_name = ?", (contract.table,)).fetchone()
    return Ledger.from_json(row[0]) if row else Ledger(contract)


def read_rows(path, contract):
    """The file's rows, typed by their header; rows flagged in the deleted column are tombstones."""
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    marker = contract.deleted_column
    live = [{k: v for k, v in r.items() if k != marker} for r in rows if r.get(marker, "false") == "false"]
    deleted = [r for r in rows if r.get(marker) == "true"]
    return live, deleted


def check(contract, manifest, live, deleted, path):
    """What the manifest declares, verified against the file: dogana records these, it never runs them."""
    names = [name for name in (live or deleted or [{}])[0] if name != contract.deleted_column]
    declared = sha256(
        json.dumps(
            [[n, columns.get(n, "int")] for n in names], ensure_ascii=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    keys = [r["id"] for r in live] + [r["id"] for r in deleted]
    inside = manifest.window is None or all(
        manifest.window[0] <= date.fromisoformat(r[contract.window_column]) <= manifest.window[1]
        for r in live
    )
    return (
        Check("row_count", len(live) == manifest.rows and len(deleted) == manifest.tombstones),
        Check("schema_hash", contract.schema_hash is None or declared == contract.schema_hash),
        Check("key_integrity", len(set(keys)) == len(keys)),
        Check("rows_in_window", inside),
        Check("content_hash", sha256(path.read_bytes()).hexdigest() == manifest.content_hash),
    )


def execute(plan, live, deleted):
    """The receipt's plan, as SQL on the published table."""
    table = plan.table
    for action in plan.actions:
        if action.operation is Operation.REPLACE_ALL:
            db.execute(f"delete from {table}")
        elif action.operation is Operation.REPLACE_WINDOW:
            low, high = (bound.isoformat() for bound in action.window)
            db.execute(f"delete from {table} where day between ? and ?", (low, high))
        elif action.operation is Operation.UPSERT:
            db.executemany(f"delete from {table} where id = ?", [(r["id"],) for r in live])
        elif action.operation is Operation.TOMBSTONE:
            db.executemany(f"delete from {table} where id = ?", [(r["id"],) for r in deleted])
    if live:
        names = list(live[0])
        db.executemany(
            f"insert into {table} ({', '.join(names)}) values ({', '.join('?' * len(names))})",
            [tuple(r[n] for n in names) for r in live],
        )


def deliver(contract, path, manifest, now):
    """Decide a file as the receiver does: replay, preflight, checks, then rows and ledger in one commit."""
    earlier = db.execute(
        "select document from dogana_receipts where identity = ? and accepted", (manifest.identity,)
    ).fetchone()
    if earlier:
        return replace(Receipt.from_json(earlier[0]), outcome=Outcome.REPLAYED)
    ledger, outcomes, live, deleted = ledger_of(contract), (), [], []
    if all(c.passed for c in Gate.preflight(ledger, manifest, now=now)):
        live, deleted = read_rows(path, contract)
        outcomes = check(contract, manifest, live, deleted, path)
    receipt = Gate.evaluate(ledger, manifest, outcomes, now=now, source=path.name)
    with db:
        if receipt.accepted:
            execute(receipt.transition.plan, live, deleted)
            db.execute(
                "insert or replace into dogana_ledgers values (?, ?)",
                (contract.table, ledger.follow(receipt).to_json()),
            )
        db.execute(
            "insert into dogana_receipts values (?, ?, ?)",
            (manifest.identity, receipt.accepted, receipt.to_json()),
        )
    return receipt
```

This is the whole integration. dogana decides (`Gate.preflight`, `Gate.evaluate`); the loader does
what only it can: it reads the file, checks it against the manifest, executes the plan the receipt
returns, and commits rows, ledger and receipt together. The `with db:` block is that commit: if
anything fails inside it, SQLite rolls everything back and the dates stay true.

## 3. Send and receive a file

```python
Path("drop").mkdir()


def send(name, contract, shape, day, rows, *, window=None, declared=None, **fields):
    """Write a CSV and its manifest as a sender would, then let the loader decide it."""
    path = Path("drop") / f"{name}.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(rows[0].keys() if rows else columns)
        writer.writerows(row.values() for row in rows)
    read_at = datetime(2026, 10, day, tzinfo=UTC)
    manifest = contract.manifest(
        shape,
        read_at,
        window=window,
        rows=len(rows) if declared is None else declared,
        content_hash=sha256(path.read_bytes()).hexdigest(),
        **fields,
    )
    path.with_suffix(".csv.json").write_text(manifest.to_json(), encoding="utf-8")
    receipt = deliver(contract, path, manifest, read_at)
    ids = sorted(row[0] for row in db.execute(f"select id from {contract.table}"))
    print(f"{name}: {receipt.outcome.value}, {contract.table} {ids}")
    return receipt


def order(key, day, amount):
    return {"id": key, "day": day, "amount": amount}


send("sunday", orders, "full", 18, [order(1, "2026-09-01", 100), order(2, "2026-10-18", 200)])
```

```text
sunday: accepted, orders [1, 2]
```

The manifest travels beside the file as `sunday.csv.json`. It declares the shape, the instant the
source was read, the number of rows and the SHA-256 of the file's bytes. Senders in other languages
write it with `dogana manifest`.

## 4. A window file

```python
window = orders.shape("daily").window_ending_at(datetime(2026, 10, 19, tzinfo=UTC), "date")
monday = send("monday", orders, "daily", 19, [order(3, "2026-10-19", 300)], window=window)


def show(label, claims):
    print(
        f"{label:<17} complete as of {claims.complete_as_of.date()}, "
        f"deletions as of {claims.deletions_as_of.date()}, {claims.state.value}"
    )


ledger = ledger_of(orders)
show("Sep 20 to Oct 19", ledger.claims(monday.received_at, limit=timedelta.max, window=window))
before = (date.min, date(2026, 9, 19))
show("before Sep 20", ledger.claims(monday.received_at, limit=timedelta.max, window=before))
```

```text
monday: accepted, orders [1, 3]
Sep 20 to Oct 19  complete as of 2026-10-19, deletions as of 2026-10-19, complete
before Sep 20     complete as of 2026-10-18, deletions as of 2026-10-18, complete
```

The 30 days ending October 19 start on September 20, both ends included. The file has every order
dated in those days. Order 2, dated October 18, is missing from it, so it was deleted at the source
and the plan removed it. Order 1, dated September 1, is outside the window and stays. The window is
now proven as of Monday, October 19, and the rest of the table as of Sunday, October 18.
`ledger.claims(now, limit=..., window=...)` returns what the ledger proves about a range of days at
the instant `now`: the two dates and a state. `limit` is how old the reader allows the dates to be;
`timedelta.max` expires nothing.

## 5. A truncated file

```python
tuesday = send("tuesday", orders, "full", 20, [], declared=2)
print([check.name for check in tuesday.checks if not check.passed][0])
```

```text
tuesday: rejected, orders [1, 3]
row_count
```

The manifest declares two rows and the file has none, so the loader's `row_count` check fails, the
whole file is rejected and nothing changes. A truncated file whose manifest declared a matching
count would pass: the count must come from the export, computed in the same read, never from
counting the file.

## 6. The next full export, and the same file twice

```python
send("next-sunday", orders, "full", 25, [order(3, "2026-10-19", 300)])
again = send("next-sunday", orders, "full", 25, [order(3, "2026-10-19", 300)])
ledger = ledger_of(orders)
claims = ledger.claims(again.received_at, limit=timedelta.max)
print(f"whole table: complete as of {claims.complete_as_of.date()}, {claims.state.value}")
week = timedelta(days=7)
print(f"eight days later: {ledger.claims(again.received_at + timedelta(days=8), limit=week).state.value}")
```

```text
next-sunday: accepted, orders [3]
next-sunday: replayed, orders [3]
whole table: complete as of 2026-10-25, complete
eight days later: expired
```

The full export removes order 1, which is no longer in it. Sent again, the same manifest gets back
its first receipt from the loader's receipts, and nothing is read or written twice. Claims are
evaluated at the instant you ask. How old is too old is the reader's choice: with a limit of seven
days, eight days after October 25 the table is expired until a new file arrives.

## 7. Ask for the next read

```python
from dogana import Requirement, next_extraction, plan_delivery

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
file of a shape at its usual size, the 30 days ending October 26. Neither function reads the
source. Your extraction job runs the read and declares in the manifest the instant it read the
source ([which instant](concepts.md#which-instant-to-declare)).

## 8. Deliver deletions

```python
stock = Contract(
    "stock",
    ("id",),
    {"full": Shape.full(), "feed": Shape.feed()},
    types={"id": "int", "deleted": "bool"},
    deleted_column="deleted",
)
send("stock-sunday", stock, "full", 25, [{"id": 1, "qty": 5}, {"id": 2, "qty": 7}], generation="g1")
changes = [{"id": 2, "qty": 9, "deleted": "false"}, {"id": 1, "qty": "", "deleted": "true"}]
feed = send("stock-monday", stock, "feed", 26, changes, since="g1", generation="g2", declared=1, tombstones=1)
claims = ledger_of(stock).claims(feed.received_at, limit=timedelta.max)
print(f"stock: {claims.state.value}, both dates {claims.complete_as_of:%A}")
```

```text
stock-sunday: accepted, stock [1, 2]
stock-monday: accepted, stock [2]
stock: complete, both dates Monday
```

A feed is a series of change files, each starting where the previous one ended. Its position is a
generation, a token the source issues with each read: Sunday's export carries `g1`, and Monday's
file declares `since="g1"` and its new generation `g2`, so no change between them is missing. A row
whose `deleted` is true is a tombstone: it deletes its key, and the loader's plan has a `tombstone`
action for it. The manifest counts those rows (`tombstones=1`) but does not list them. Item 1 is
gone and item 2 is updated. Both dates move to Monday, because the feed starts where Sunday's export
ended and reports its deletions.

## 9. Decide related tables together

```python
from dogana import Group

invoices = Contract("invoices", ("id",), {"full": Shape.full()}, types={"id": "int"})
lines = Contract("invoice_lines", ("id",), {"full": Shape.full()}, types={"id": "int"})
group = Group((invoices, lines))
ledgers = {"invoices": Ledger(invoices), "invoice_lines": Ledger(lines)}


def together(invoices_at, lines_at):
    """Both files read from one snapshot carry one instant; the loader then commits both or none."""
    manifests = {
        "invoices": invoices.manifest("full", datetime(2026, 10, invoices_at, tzinfo=UTC), rows=1),
        "invoice_lines": lines.manifest("full", datetime(2026, 10, lines_at, tzinfo=UTC), rows=1),
    }
    receipts = group.apply(ledgers, manifests, now=datetime(2026, 10, 28, tzinfo=UTC))
    outcomes = {table: receipt.outcome.value for table, receipt in receipts.items()}
    print(outcomes, [check.detail for check in receipts["invoices"].checks if not check.passed])
    return receipts


accepted = together(26, 26)
after = {table: ledgers[table].follow(accepted[table]) for table in group.tables}
print(
    f"exact as of {group.claims(after, datetime(2026, 10, 28, tzinfo=UTC), limit=timedelta.max).exact_as_of:%A}"
)
together(27, 26)
```

```text
{'invoices': 'accepted', 'invoice_lines': 'accepted'} []
exact as of Monday
{'invoices': 'rejected', 'invoice_lines': 'rejected'} ['read boundaries differ: invoice_lines 2026-10-26T00:00:00+00:00, invoices 2026-10-27T00:00:00+00:00']
```

A group names tables read together. Invoices and their lines are read in one snapshot, so both
manifests carry the same instant, and `Group.apply` decides them as one. On Monday both are
accepted; the loader commits both tables and both ledgers in one transaction, exactly as `deliver`
does for one table, and both are exact as of Monday, so a join between them gives the source's
answer. Files stamped with different instants cannot come from one snapshot: both are rejected, and
nothing is committed.
