# Integrations

The integration boundary is publication: the gate accepts a candidate before it replaces visible
rows. The [protocol](protocol.md) defines the checks and atomicity a store must provide. The designs
below do not imply that production adapters are shipped.

## dlt

A dlt adapter belongs on the store side. `hold` identifies the normalized load package; checks read
that candidate; `publish` lets dlt write the accepted rows once and commits the ledger and receipt
with them. Loading the destination first and writing dogana's
ledger afterward violates atomic publication. A destination integration must supply one transaction
or one table-format commit; `pipeline.load()` alone is not evidence that both records commit together.

The ledger owns extraction progress. Run each resource with explicit `initial_value=request.since`
and `end_value=request.until`. dlt documents that `end_value` makes filtering stateless, so an old
resource cursor does not override the requested start. Its default end is open; dogana needs the
upper endpoint included. Configure `range_end="closed"`. Use `range_start="open"` for `(since, until]`
when `cursor_column` is declared or the source is keyless append. A keyed read without a declared
cursor column may include the start. See the [incremental API](https://dlthub.com/docs/api_reference/dlt/extract/incremental/__init__).

A stateful resource beside the ledger can lose data: it advances its own cursor during extraction,
the gate rejects the load, then the next extraction skips rows the ledger still needs. Keep one
progress owner and retain the normalized candidate for retries. The exact schema check also requires
projecting loader metadata columns away or declaring them consistently in the contract.

A transaction may stamp `updated_at=09:00:00` and commit at `09:00:07`, after a read ending at
`09:00:05`. The absent row is invisible to the gate. Keyed `Shape.lookback` subtracts a duration from
the weakest boundary so a later read overlaps it. This mitigates bounded commit lag without proving
completeness. Keyless appends cannot overlap without duplicating events and require a trustworthy
source boundary. A correct row timestamp check cannot discover a row that never arrived.

## dbt

Create the `dogana_regions` view from the [reference guide](reference.md#internal-tables-and-ledger-json)
in the landing database. It exposes current dates and unknown regions. dbt can use a custom
`loaded_at_query` for source freshness. This example deliberately uses the weakest deletion date;
unknown or missing evidence yields null rather than disappearing from `min`.
The configuration is supported in dbt 1.10 and later; consult the [freshness reference](https://docs.getdbt.com/reference/resource-configs/freshness)
for your installed version.

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
                     when count(*) > 0 and count(valid_as_of) = count(*)
                     then min(valid_as_of)
                   end
            from main.dogana_regions
            where table_name = 'orders'
          freshness:
            warn_after: {count: 1, period: day}
            error_after: {count: 7, period: day}
```

For a reporting cutoff, save the following as `tests/assert_orders_deletions_known.sql` and pass
`required_as_of` as an ISO UTC timestamp dbt variable. A singular data test returns failing records;
this one returns a row when the ledger is missing, any region is unknown or deletion knowledge is
too old. See [singular tests](https://docs.getdbt.com/docs/build/data-tests#singular-data-tests).

```sql
select 'orders' as table_name
from {{ source('landing', 'dogana_regions') }}
where table_name = 'orders'
having count(*) = 0
    or count(valid_as_of) < count(*)
    or min(valid_as_of) < cast('{{ var("required_as_of") }}' as timestamptz)
```

Compare completeness too when the consumer needs both guarantees. These tests check input evidence
thresholds, not query exactness. Mixed dates do not create a consistent snapshot, and an anti-join's
negated input needs deletion knowledge to avoid hiding true answers. Run the checks and downstream
reads against one pinned publication, or prevent intervening writes during the job.

[dbt snapshots](https://docs.getdbt.com/docs/build/snapshots) retain row history; hard-delete detection
needs complete source coverage. Restrict a snapshot input to a fixed scope with adequate evidence:

```sql
select * from {{ source('landing', 'orders') }}
where day between date '2026-10-01' and date '2026-10-31'
```

That scope must also be the snapshot target's scope. Do not move a filter on a global snapshot with
hard-delete detection: rows excluded by the new filter could look deleted. A patched region outside
a fixed scope need not block snapshotting a complete region inside it. Incremental transformations
cannot infer missing source deletions from fresh load timestamps alone.

## SQLMesh

SQLMesh snapshots record model versions and processed time intervals. Those intervals support
incremental execution and backfills. dogana instead records source-delivery coverage and whether
absence justified deletion. A successful model run does not establish that its source export was
complete. Feed dogana's input requirements into scheduling before SQLMesh processes an interval;
retain the publication identity used by the run. See [SQLMesh snapshots](https://sqlmesh.readthedocs.io/en/latest/concepts/architecture/snapshots/).

## Iceberg, Delta and DuckLake

A table-format store can map a plan onto its row operations and publish through an atomic commit.
`publish(candidate, receipt)` executes the accepted plan and has the evidence
needed for the commit. Its metadata must carry the full receipt or reference an already durable
copy, the expected parent and the resulting publication identity. A receipt hash alone is not a
recovery record. Large key lists usually favor a durable referenced receipt.

The commit must reject a stale `base_publication_id`; automatic rebase must not reuse old checks.
The ledger can be reconstructed from accepted receipts in committed order. Retain those records
independently of snapshot-expiration policy if recovery must outlive row snapshots. Map each
publication to the committed row version exposed to readers, not an intermediate rewrite step.
A snapshot timestamp says when storage changed; regional evidence says which source rows that
change establishes and through which source boundary.

## Streams: Kafka, Debezium, Spark Structured Streaming

A stream becomes a sequence of bounded deliveries through the existing shapes. For Kafka carrying
Debezium change data capture (CDC), use keyed `Shape.feed()` after a complete `Shape.full()` snapshot
that issues the matching starting offset as `generation`. Each feed manifest has the committed
generation as `since` and the next offset as `generation`. Encode the source identity and all relevant
partition offsets in one stable, opaque string: the ledger holds one table-wide cursor. The extractor
must establish continuity from the snapshot and all changes through a truthful `produced_at`;
an offset alone does not prove a source-time boundary.

Collapse each batch to one final live row or terminal tombstone per key. Manifest `keys` names every
live row key; `tombstones` names deleted keys; `rows` counts live rows only. A broken generation chain
needs a full reset. Continuous tombstones preserve an exact base; they do not repair an existing
deletion gap.

For immutable keyless events, use `Shape.append()`, `key=()` and a declared `start_at`. Read contiguous
arrival-order intervals `(since, produced_at]`, starting at `start_at` and then at the previous
completeness boundary. Arrival means entry into the source log, not receipt by this consumer. The
source must guarantee every arrival through the boundary and never revise or delete retained events.
If declaring `cursor_column`, use the source arrival timestamp. Equal payloads remain separate events.

For keyed upserts without a deletion signal, use
`Shape.changes(checkpoint="timestamp", deletions="none")` after a full snapshot, with `lookback` for
bounded lag. `next_extraction` extends the read back from the weakest completeness boundary; matching
by key makes overlap safe. Completeness advances while validity remains at its previous boundary.

A CDC consumer loop, after the snapshot:

1. Load the committed ledger and resume from its `generation`, with automatic broker offset commits disabled.
2. Poll a bounded offset range, establish its source read boundary and collapse its changes by key.
3. Build a manifest with the previous generation as `since`, the range end as `generation`, live keys,
   tombstones and live row count. Retain the rows and exact manifest for retries.
4. Call `deliver(store, contract, rows, manifest, now=receiver_time)`. The store atomically commits rows,
   ledger and accepted receipt against the expected parent. The generation in that commit is the
   authoritative consumed offset; the receipt is the destination commit record.
5. Acknowledge broker offsets only after acceptance or accepted replay, then repeat. On rejection or
   storage failure, stop without acknowledging; inspect the receipt or recover the committed state
   before retrying the retained delivery.

The broker acknowledgement is separate from the destination commit. A crash between them leaves the
broker behind; resume from destination evidence or replay the exact retained manifest. Ordered calls
do not create a transaction across Kafka and a database. The same atomicity obligation applies to a
table-format store: its commit carries the receipt or a reference to an already durable copy.

Spark's `foreachBatch` supplies a DataFrame and batch ID in micro-batch mode; deterministic queries
produce the same output for the same ID. See the
[`foreachBatch` API](https://spark.apache.org/docs/latest/api/python/reference/pyspark.ss/api/pyspark.sql.streaming.DataStreamWriter.foreachBatch.html).
A keyed generation feed can encode that ID with a stable checkpoint identity, such as
`orders-checkpoint-1:42`, and use the previous committed token as `since`. Spark's
[query ID](https://spark.apache.org/docs/latest/api/python/reference/pyspark.ss/api/pyspark.sql.streaming.StreamingQuery.id.html)
persists across restarts from checkpoint data. A reset checkpoint must not reuse the old identity;
a broken chain needs a full snapshot. The adapter still needs proof of a bounded, complete source
read and a stable manifest on retry. The batch ID supplies neither `produced_at` nor event time.
Keyless output still requires contiguous timestamp append intervals. Publish through the same atomic
store; a DataFrame write followed by a ledger write is insufficient.

Replay is at batch granularity: an accepted manifest is published once. It does not deduplicate an
event re-emitted inside a new batch; a key or the consumer must do that. Keyless append retains that
occurrence.

Time is the source read boundary, not event time. dogana has no watermarks or event-time windows.
An invisible late row can be absorbed by a keyed lookback only if it becomes visible while its timestamp
still falls within the overlap; the gate cannot detect an omitted row. Keyless append follows arrival
order: a late business event belongs to its later source-arrival interval, without reopening an
earlier event-time interval.

## Replication systems

For Airbyte, Fivetran or a custom loader, translate the actual configured extraction guarantees into
a shape and gate publication of the candidate. A full load to an isolated staging table is possible;
loading the consumer's table before acceptance is not. A faithful change-data-capture replica that
already preserves inserts, updates and deletions may not need a second delivery ledger.

## Dagster and Airflow

Ask `plan_delivery` for the required source evidence, extract, then call
`deliver(store, contract, source, manifest, now)`. Record publication identity with the task result
and use it for downstream reads. `now` is receiver time; the manifest's boundary must describe the
actual source read. Backdating a current read to a scheduler's logical date is not valid evidence.
Older windows can arrive safely when disjoint; an overlapping older full delivery is refused whole.

Retry retained manifests and candidates so accepted-receipt lookup can return the original result.
Operational failures propagate as storage errors. A rejected receipt means a declaration or check
failed and needs investigation; blind retries of unchanged rejected evidence do not repair it.

## Quality and data contracts

Business-quality checks can run on staged rows before invoking delivery. The gate currently requires
its exact named check set, so arbitrary Soda, Great Expectations or SQL outcomes cannot be appended
to `Gate.evaluate` as additional checks. Persist their results in the surrounding job's audit record.
Freshness and volume checks alone cannot establish deletion completeness; source reconciliation can
supply independent evidence that a manifest omitted something.

Project an existing data contract's table, key and types into `Contract`, adding the delivery shapes
and source guarantees. Governance fields, owner identity, signatures and service-level objectives
stay in that contract system. There is no shipped ODCS carrier or separate namespace field.

A sender sidecar, receiver filename pattern or controlled extractor can produce the manifest.
All three remain declarations. Owning the extractor does not make its boundary or coverage true by
construction: endpoint errors, missing pages and late commits still cross the same trust boundary.
