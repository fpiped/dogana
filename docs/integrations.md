# Integrations

Every integration meets dogana at publication: the gate accepts a candidate before it replaces
visible rows, under the checks and atomicity the [protocol](protocol.md) requires of a store. No
adapter ships; these are designs.

## dlt

A dlt adapter is a store: `hold` names the normalized load package, the checks read it, and
`publish` has dlt write the accepted rows and commits ledger and receipt with them, in one
transaction or table-format commit. Loading first and writing the ledger after is not atomic, and
`pipeline.load()` alone does not commit both.

The ledger owns extraction progress. Run each resource with `initial_value=request.since`,
`end_value=request.until` (which makes dlt's filtering stateless) and `range_end="closed"`, since
dogana includes the upper end; add `range_start="open"` when a cursor column is declared or the
table is a keyless append. See the
[incremental API](https://dlthub.com/docs/api_reference/dlt/extract/incremental/__init__). A
stateful resource beside the ledger loses data: it advances its cursor, the gate rejects the load,
and the next run skips rows the ledger still needs. Retain the normalized candidate for retries.

With a schema hash, project dlt's metadata columns away or declare them in the data contract. A
boolean column marked by dlt's `hard_delete` hint can be the deleted column: the rows dlt deletes
are the tombstones.

## dbt

Create the [`dogana_regions` view](reference.md#internal-tables-and-ledger-json) in the landing
database. Source freshness can read the weakest deletion date through a `loaded_at_query` (dbt 1.10
and later, [freshness reference](https://docs.getdbt.com/reference/resource-configs/freshness));
an unknown region yields null instead of vanishing from `min`.

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

For a reporting cutoff, this [singular test](https://docs.getdbt.com/docs/build/data-tests#singular-data-tests),
`tests/assert_orders_deletions_known.sql` with `required_as_of` an ISO UTC timestamp variable,
fails when the ledger is missing, a region is unknown or the deletion date is too old:

```sql
select 'orders' as table_name
from {{ source('landing', 'dogana_regions') }}
where table_name = 'orders'
having count(*) = 0
    or count(deletions_as_of) < count(*)
    or min(deletions_as_of) < cast('{{ var("required_as_of") }}' as timestamptz)
```

Compare `complete_as_of` too when the consumer needs both. These tests check input dates, not a
query's exactness: an anti-join's negated input needs its deletion date. Run checks and downstream
reads against one pinned publication.

For tables delivered together, this test fails unless they share one instant, their claims'
`exact_as_of`:

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

Add window bounds when the models read only part of a table, and run it in the same transaction
as those models, or against pinned publications.

[dbt snapshots](https://docs.getdbt.com/docs/build/snapshots) detect hard deletes only over complete
coverage. Restrict a snapshot's input to a fixed scope with both dates:

```sql
select * from {{ source('landing', 'orders') }}
where day between date '2026-10-01' and date '2026-10-31'
```

The target must have the same scope; moving the filter makes the rows it excludes look deleted.
A patched region outside the scope does not block the snapshot.

## SQLMesh

[SQLMesh snapshots](https://sqlmesh.readthedocs.io/en/latest/concepts/architecture/snapshots/)
record which intervals a model processed, not whether its source export was complete. Check the
interval's input requirements with dogana before SQLMesh runs it, and record the publication
identity the run read.

## Iceberg, Delta and DuckLake

A table-format store maps a plan onto row operations and publishes in one atomic commit whose
metadata carries the full receipt, or a reference to a durable copy, with the expected parent and
the new publication identity; a receipt hash alone recovers nothing. The commit rejects a stale
`base_publication_id`, and an automatic rebase must not reuse old checks. Keep accepted receipts
beyond snapshot expiration if recovery must outlive row snapshots, and map each publication to the
row version readers see. A snapshot timestamp says when storage changed; the ledger says which
source rows that change establishes.

## Micro-batches from a stream

A micro-batch committed from a stream is one delivery. A change feed consumed in offset ranges
after a snapshot is `Shape.feed()`, `since` the committed offsets and `generation` the next, each
encoded as one opaque string per table, and each batch collapsed to one live or tombstone row per
key. Immutable events
without identity are `Shape.append()` over contiguous arrival-order intervals; keyed upserts
without a delete signal are `Shape.changes()` with a `lookback`.

The consumer loop is exactly-once with the receipt as the commit record: resume from the ledger's
generation with broker auto-commit off, poll a bounded range, retain rows and manifest, `deliver`,
and acknowledge the broker only after an acceptance or a replay. A crash before the acknowledgement
converges by replaying the retained manifest. A Spark `foreachBatch` id can be a keyed feed's
generation under a stable checkpoint location; it gives neither the read boundary nor event time.

Replay is per batch: an event re-emitted in a new batch is deduplicated by its key or the consumer,
not by the ledger. Time is the read boundary, not event time: a late event with an earlier
timestamp is invisible to the gate.

## Replication systems

For Airbyte, Fivetran or a custom loader, translate the configured extraction into a shape and
gate the candidate: load into an isolated staging table, never into the consumer's table before
acceptance. A CDC replica that already applies deletes may not need dogana.

## Dagster and Airflow

Ask `plan_delivery` what to read, extract, then `deliver(store, contract, source, manifest, now)`
and pass the publication identity to downstream reads. `now` is the receiver's time; the manifest
declares the source read, never the scheduler's logical date
([which instant](concepts.md#which-instant-to-declare)). Retry with the retained manifest and
candidate, so a replay returns the original receipt. A storage error is operational; a rejection
is a failed declaration or check, and retrying it unchanged repairs nothing.

## Quality and data contracts

Business checks (Soda, Great Expectations, SQL) run on the staged rows before delivery and keep
their results in the job's own record: the gate takes exactly its named checks. Freshness and
volume checks cannot show deletions are complete; reconciliation with the source can show a
manifest omitted something.

The data contract owns the columns. Take its table and key into `Contract`, add the shapes, and
its column hash as `schema_hash`, computed with the [protocol's recipe](protocol.md#contract-and-shapes).
Owners, signatures and service levels stay in the data contract.

A manifest from the sender, a filename pattern or your own extractor is still a declaration: owning
the extractor does not make its boundary true, since failed requests, missing pages and late
commits cross the same trust boundary.
