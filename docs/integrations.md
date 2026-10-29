# Integrations

Every integration meets dogana at publication: the gate accepts a candidate before it replaces
visible rows, with the atomicity the [protocol](protocol.md) requires of a store. No adapter ships;
these are designs.

## dlt

A dlt adapter is a store: `hold` names the normalized load package, the checks read it, and
`publish` has dlt write the accepted rows in the same transaction or table-format commit as ledger
and receipt. Loading first and writing the ledger after is not atomic.

The ledger owns extraction progress: run each resource with `initial_value=request.since`,
`end_value=request.until` and `range_end="closed"`, plus `range_start="open"` with a cursor column
or a keyless append ([incremental API](https://dlthub.com/docs/api_reference/dlt/extract/incremental/__init__)).
A resource keeping its own cursor loses data when the gate rejects a load it already advanced past.
With a schema hash, project dlt's metadata columns away; a boolean marked by the `hard_delete` hint
can be the deleted column.

## dbt

Create the [`dogana_regions` view](reference.md#internal-tables-and-ledger-json) in the landing
database. Source freshness can read the weakest deletion date
([freshness](https://docs.getdbt.com/reference/resource-configs/freshness), dbt 1.10 and later):

<!-- illustration: dbt source configuration; requires dbt and its adapter, neither ships here -->
```yaml
version: 2
sources:
  - name: landing
    schema: main
    tables:
      - name: dogana_regions
      - name: orders
        config:
          loaded_at_query: |
            select case
                     when count(*) > 0 and count(deletions_as_of) = count(*)
                     then min(deletions_as_of)
                   end
            from main.dogana_regions
            where table_name = 'orders'
          freshness:
            warn_after: {count: 1, period: day}
            error_after: {count: 7, period: day}
```

A [singular test](https://docs.getdbt.com/docs/build/data-tests#singular-data-tests) for a reporting
cutoff, `required_as_of` an ISO UTC timestamp, fails when the ledger is missing, a region is unknown
or the deletion date is too old; compare `complete_as_of` too when the consumer needs both:

<!-- test setup: populate a DuckDB file and reuse the view defined in Reference

```python
import re
from datetime import UTC, datetime, timedelta

from dogana import Contract, Ledger, Region, Shape, deliver
from example_store import DuckDBStore

now = datetime(2026, 10, 26, tzinfo=UTC)
store = DuckDBStore("landing.duckdb")
connection = store.connection
contracts = {
    table: Contract(
        table,
        ("id",),
        {"full": Shape.full(), "window": Shape.window()},
        window_column="id",
        types={"id": "int"},
    )
    for table in ("orders", "order_lines")
}
for table, contract in contracts.items():
    assert deliver(store, contract, [{"id": 1}], contract.manifest("full", now, rows=1), now=now).accepted
reference = (documentation / "docs/reference.md").read_text(encoding="utf-8")
view = re.search(r"^```sql[^\n]*\n(.*?)^```", reference, re.MULTILINE | re.DOTALL)
assert view is not None
connection.execute(view[1])
original = connection.execute("select * from __dogana_ledgers order by table_name").fetchall()


def restore():
    connection.execute("delete from __dogana_ledgers")
    connection.executemany("insert into __dogana_ledgers values (?, ?)", original)


def evidence(table, regions):
    ledger = Ledger(contracts[table], regions=tuple(regions))
    connection.execute(
        "update __dogana_ledgers set document = ? where table_name = ?", [ledger.to_json(), table]
    )


sql_bindings = {
    "{{ source('landing', 'dogana_regions') }}": "main.dogana_regions",
    '{{ var("required_as_of") }}': now.isoformat(),
}
```

-->

```sql
select 'orders' as table_name
from {{ source('landing', 'dogana_regions') }}
where table_name = 'orders'
having count(*) = 0
    or count(deletions_as_of) < count(*)
    or min(deletions_as_of) < cast('{{ var("required_as_of") }}' as timestamptz)
```

<!-- test setup: a cutoff accepts current evidence and refuses missing, unknown and stale evidence

```python
assert sql_result == []
cutoff_query = sql_query
connection.execute("delete from __dogana_ledgers where table_name = 'orders'")
assert connection.execute(cutoff_query).fetchall() == [("orders",)]
restore()
evidence("orders", [Region(None, None)])
assert connection.execute(cutoff_query).fetchall() == [("orders",)]
evidence("orders", [Region(None, None, complete_as_of=now, deletions_as_of=now - timedelta(days=1))])
assert connection.execute(cutoff_query).fetchall() == [("orders",)]
evidence("orders", [Region(None, 0, complete_as_of=now, deletions_as_of=now), Region(0, None, low_open=True)])
assert connection.execute(cutoff_query).fetchall() == [("orders",)]
restore()
```

-->

For tables delivered together, this test fails unless they share one instant, their `exact_as_of`;
add window bounds when the models read part of a table:

```sql
select 'orders, order_lines' as tables_read_together
from {{ source('landing', 'dogana_regions') }}
where table_name in ('orders', 'order_lines')
having count(distinct table_name) < 2
    or count(complete_as_of) < count(*)
    or count(distinct complete_as_of) <> 1
    or count(distinct deletions_as_of) <> 1
    or min(complete_as_of) <> min(deletions_as_of)
```

<!-- test setup: a common instant requires every date in every region of both tables

```python
assert sql_result == []
together_query = sql_query
failure = [("orders, order_lines",)]
connection.execute("delete from __dogana_ledgers where table_name = 'order_lines'")
assert connection.execute(together_query).fetchall() == failure
connection.execute("delete from __dogana_ledgers")
assert connection.execute(together_query).fetchall() == failure
restore()
earlier = now - timedelta(days=1)
evidence("order_lines", [Region(None, None, complete_as_of=earlier, deletions_as_of=earlier)])
assert connection.execute(together_query).fetchall() == failure
evidence("order_lines", [Region(None, None, complete_as_of=now, deletions_as_of=earlier)])
assert connection.execute(together_query).fetchall() == failure
evidence("order_lines", [Region(None, None)])
assert connection.execute(together_query).fetchall() == failure
evidence(
    "order_lines", [Region(None, 0, complete_as_of=now, deletions_as_of=now), Region(0, None, low_open=True)]
)
assert connection.execute(together_query).fetchall() == failure
restore()
store.close()
```

-->

Run tests and models against one pinned publication. [dbt snapshots](https://docs.getdbt.com/docs/build/snapshots)
detect hard deletes only over complete coverage: restrict the input and the target to one fixed
window range with both dates, and never move that filter, or the rows it drops look deleted.

## SQLMesh

[SQLMesh](https://sqlmesh.readthedocs.io/en/latest/concepts/architecture/snapshots/) records which
intervals a model processed, not whether its source export was complete: check the interval's input
requirements with dogana before it runs, and record the publication identity it read.

## Iceberg, Delta and DuckLake

A table-format store applies the plan's row operations in one atomic commit whose metadata carries
the receipt, or a reference to a durable copy, with the expected parent and the new publication
identity. The commit rejects a stale `base_publication_id`, and a rebase must not reuse old checks.
Keep accepted receipts beyond snapshot expiration if recovery must outlive row snapshots.

## Streams

A micro-batch committed from a stream is one delivery. A CDC feed read in offset ranges after an
initial snapshot uses `Shape.feed()`: `since` is the last committed offset and `generation` the next
one, each encoded as one opaque string per table. Collapse each batch to one row per key. Keyless
events are `Shape.append()`; upserts without deletes are `Shape.changes()` with a `lookback`.
Debezium's new-record-state transform writes a delete as a row whose boolean `__deleted` is true:
declare it as the deleted column.

With the receipt as the commit record, the loop delivers each batch exactly once. Turn the broker's
auto-commit off and resume from the ledger's generation. Keep each batch's rows and manifest, call
`deliver`, and acknowledge the batch only after it is accepted or replayed. After a crash,
delivering the kept manifest again is a replay, so the loop ends in the same state. A Spark
`foreachBatch` id can be a feed's generation under a stable checkpoint location; it gives neither
read boundary nor event time. Events re-emitted in a new batch are deduplicated by key, not by the
ledger, and a late event with an earlier timestamp is invisible.

## Orchestrators and loaders

In Dagster, Airflow or a cron job: `plan_delivery`, extract, `deliver(store, contract, source,
manifest, now)`, then pass the publication identity to downstream reads. The manifest declares the
source read, never the scheduler's date ([which instant](concepts.md#which-instant-to-declare)).
Retry with the retained manifest and the same bytes, so a replay returns the original receipt; a
rejection retried unchanged repairs nothing. For Airbyte, Fivetran or a custom loader, translate the
configured extraction into a shape and load into staging, never into the consumer's table before
acceptance.

## Data contracts and quality

The data contract owns the columns: take its table and key into `Contract`, and its column hash,
computed with the [protocol's recipe](protocol.md#contract-and-shapes), as `schema_hash`. Business
checks (Soda, Great Expectations, SQL) run on staged rows and keep their own results; the gate takes
only its named checks. A manifest from your own extractor is still a declaration: failed requests,
missing pages and late commits cross the same trust boundary.
