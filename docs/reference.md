# Reference

The [protocol](protocol.md) defines required behavior. This page covers the command line, a loader
written against the gate, and a layout for the ledger a loader may keep.

## Command line

`dogana`, also `python -m dogana`, hashes files but never parses them.

<!-- illustration: command syntax with placeholders and optional arguments -->
```text
dogana manifest FILE --contract PATH --shape NAME --produced-at TIME --rows N [options]
dogana check FILE --contract PATH [--manifest PATH]
dogana claims LEDGER [--at TIME] [--limit-days DAYS] [--window LOW HIGH]
```

`manifest` writes `FILE.json`, validated against the contract, with the SHA-256 of FILE and the
contract's schema hash. `--rows` declares the live rows and, for a tombstone shape, `--tombstones`
the deleted ones; the receiver counts them again. Values are typed by the contract:
`--window LOW HIGH` by the window column (omitted for a rolling shape ending at `--produced-at`),
`--since` by the shape's checkpoint, `--generation` by the table's kind of position. Times need a
UTC offset; `--produced-at` is the source read, never when the command runs. An existing file is
replaced only with `--force`, atomically; `-o -` writes to standard output.

`check` validates FILE's manifest against the contract, schema hash included, and FILE's bytes
against its `content_hash`, refusing a manifest without one. Rows are checked by the receiver.

`claims` prints what a ledger, as JSON (`store.load_ledger(contract).to_json()`), claims at `--at`,
now by default: one line per region, cut to `--window` if given, then the table's state and its
exact instant. With `--limit-days`, a region proven longer ago than that is expired. Date and
integer bounds print closed.

<!-- test setup: reproduce the claims output with a real window delivery and the command line

```python
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from dogana import Contract, Ledger, Shape
from dogana.cli import main

orders = Contract(
    "orders",
    ("id",),
    {"daily": Shape.window(size=timedelta(days=30))},
    window_column="day",
    types={"id": "int", "day": "date"},
)
now = datetime(2026, 10, 26, 6, tzinfo=UTC)
manifest = orders.manifest("daily", now, window=(date(2026, 9, 27), date(2026, 10, 26)), rows=1)
ledger = Ledger(orders).apply(manifest)
Path("orders.ledger.json").write_text(ledger.to_json(), encoding="utf-8")
assert main(["claims", "orders.ledger.json", "--at", now.isoformat()]) == 0
```

-->

```text
orders (-inf, 2026-09-26]: unknown; complete as of unknown; deletions as of unknown
orders [2026-09-27, 2026-10-26]: complete; complete as of 2026-10-26T06:00:00+00:00; deletions as of 2026-10-26T06:00:00+00:00
orders [2026-10-27, +inf): unknown; complete as of unknown; deletions as of unknown
orders: unknown; exact at no single instant
```

Exit status is 0 on success, 1 when dogana refuses (an invalid contract, manifest or ledger, a
mismatch, an unreadable file, an existing manifest) and 2 when the command is called wrongly.

## A loader

dogana never reads rows. The loader does, in the order the [protocol](protocol.md#the-receiver)
states; the [quick start](quickstart.md) is one on SQLite.

<!-- illustration: loader pseudocode; load_ledger, check_rows, execute and save are the loader's own -->
```python
earlier = accepted_receipt(manifest.identity)  # a replay answers with it and reads nothing
ledger = load_ledger(contract)
checks = Gate.preflight(ledger, manifest, now=now)
outcomes = check_rows(staged, contract, manifest) if all(c.passed for c in checks) else ()
receipt = Gate.evaluate(ledger, manifest, outcomes, now=now)
if receipt.accepted:
    execute(receipt.transition.plan)  # then, in the same transaction:
    save(ledger.follow(receipt), receipt)
else:
    save(receipt)
```

`check_rows` is the loader's, and reports what the manifest declares: `rows` live rows and
`tombstones` tombstones, keys non-null and unique, window values inside the window, the column hash
and `content_hash`. Each result is a `Check(name, passed, detail)`; a failed one rejects, and the
receipt records them all.

The plan's actions, which `execute` performs on the published table:

| Action | Meaning |
|---|---|
| `replace_all` | Delete every row, then insert the read's live rows. |
| `replace_window` | Delete the rows whose window value is inside the window, and earlier copies of the read's keys, then insert. |
| `upsert` | Delete the published rows sharing a key with a live row of the read, then insert those rows. |
| `tombstone` | Delete the published rows whose keys the tombstones name. |
| `append` | Insert every row without matching. Keyless replacement matches no keys. |

What the loader owes, in four sentences. Rows, ledger and receipt change in one transaction. A
decision made against a ledger that moved is made again. Every receipt is kept, and the ledger
equals `Ledger.from_receipts` of the accepted ones. Readers read rows and ledger from one committed
state.

## Ledger JSON

A ledger document, `ledger.to_json()`, holds `contract`, `regions`, `generation` and
`publication_id`; each region holds `low`, `high`, `low_open`, `high_open`, `complete_as_of` and
`deletions_as_of`, null when unknown or unbounded (see the [ledger fixture](examples/ledger.json)).
A loader that keeps it in a table `dogana_ledgers(table_name, document)` can expose the regions to
SQL; on DuckDB:

<!-- illustration: a DuckDB view over the ledger documents; the JSON functions are DuckDB's -->
```sql
create or replace view dogana_regions as
select l.table_name,
       json_extract_string(l.document, '$.publication_id') as publication_id,
       r.key::integer as region_index,
       json_extract(r.value, '$.low') as low,
       json_extract(r.value, '$.high') as high,
       json_extract(r.value, '$.low_open')::boolean as low_open,
       json_extract(r.value, '$.high_open')::boolean as high_open,
       json_extract_string(r.value, '$.complete_as_of."$datetime"')::timestamptz as complete_as_of,
       json_extract_string(r.value, '$.deletions_as_of."$datetime"')::timestamptz as deletions_as_of
from dogana_ledgers l,
     json_each(l.document, '$.regions') r;
```

The view reads current evidence, without pinning a publication or computing freshness. SQL `min`
ignores nulls, so require every region's date to be non-null before aggregating, as the
[dbt recipes](integrations.md#dbt) do.

## Recovery and contract changes

`Ledger.from_receipts(contract, receipts)` rebuilds the ledger from the accepted receipts; a stored
ledger that differs from it is wrong, and rebuilding recovers no rows. Rows in doubt are replaced by
a newer complete whole-table read, never by a window or a replay. A new `Contract` may change
policies and shapes but not the table, key, window column, deleted column or their types; a changed
row schema needs a complete whole-table delivery. A source restored from backup invalidates a
timestamp checkpoint: request a complete read. Generation tokens must tell source incarnations
apart.
