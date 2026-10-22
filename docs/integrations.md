# Integrations

Where dogana sits next to the tools a data team already runs. Each relation is stated from the
tool's documented behaviour; a feature not listed here was not verified.

## dlt

dlt extracts, normalises and loads. dogana owns the delivery state. The division of labour:

- dogana computes the next read (`next_extraction` or `plan_delivery`) and hands dlt the interval.
  dlt's incremental cursor accepts `initial_value` and `end_value`; with `end_value` set, dlt
  does not modify its own incremental state, so the read is stateless and dogana remains the only
  owner of "where are we". Note the endpoint conventions: dlt's `initial_value` is inclusive and
  `end_value` exclusive by default (`range_start="closed"`, `range_end="open"`), while a complete
  dogana window includes both endpoints. An adapter translates.
- dogana's `ApplyPlan` maps onto dlt's merge strategies. `replace_all` is `write_disposition="replace"`.
  `upsert` is the `upsert` strategy. `tombstone` is the `hard_delete` hint. `replace_window` is
  the `cdc` strategy (documented as treating the load as a complete snapshot and deleting what the
  snapshot lacks) bounded by `destination_scope`, which limits the destination rows a merge may
  delete. With `destination_scope` set, dlt ignores `merge_key`.
- dlt writes `_dlt_loads` with one row per load package and treats a load as complete only when its
  status row exists. That is per-load bookkeeping; it does not say which regions of a table are
  complete or whether deletions were observed. The dogana ledger adds exactly that, keyed by the
  same table.

A dlt extractor adapter is the first planned integration: it takes an `ExtractionRequest`, runs the
resource with the interval, and emits the manifest from the run.

## dbt (Core 2.0 and 1.x)

dbt transforms; dogana publishes landing tables that dbt reads. Three points of contact:

- Source freshness. dbt computes `max(loaded_at_field)` and compares it with `warn_after` and
  `error_after`. That is one date per table and it only says when rows last arrived. dogana's
  claims give two dates per region. A dbt source test can read `valid_as_of` from the ledger and
  fail a model that depends on absence (items with no movement) when the region is patched.
- Incremental models. dbt's strategies (`append`, `merge`, `delete+insert`, `insert_overwrite`,
  `microbatch` with `event_time`, `batch_size`, `lookback`) decide how a model rebuilds itself.
  They do not know whether their input was complete. A model that runs after a patched delivery
  can carry deleted source rows forward until a full refresh. The ledger tells the orchestrator
  when that full refresh is actually needed instead of scheduling it weekly on faith.
- Snapshots. `hard_deletes: invalidate` or `new_record` close a snapshot row when the source row
  disappears. That works only when the source extract is complete, which is exactly the fact a
  dogana region records. Snapshotting a patched region invalidates nothing and should be skipped.

dbt v2 is a ground-up rewrite in Rust, distributed as a binary with Parquet artifacts; the
integration surface above is the same in both generations because it is the warehouse tables and
the ledger, not the dbt process.

## Airbyte and Fivetran

Both load with their own sync modes (full refresh, incremental append, incremental deduped;
Fivetran's inferred deletes via re-sync). dogana sits after landing: the adapter declares the
coverage and deletion guarantee of the configured sync as a shape, verifies what landed against it,
and publishes. The replication tool keeps its connectors; the receiver gains a ledger.

## Iceberg, DuckLake, Delta

Table formats own rows, snapshots and atomic commits. A store adapter maps the apply plan onto the
format's primitives: Iceberg `overwrite` with a filter for `replace_window`, `upsert` and `delete`
for the keyed actions; DuckLake multi-table transactions for rows plus ledger; Delta `replaceWhere`
for a bounded replace. Each format has a per-commit metadata slot (Iceberg snapshot summary,
DuckLake commit extra info, Delta commit user metadata) that carries the receipt identity, so the
format's own history links to the audit record. None of them stores per-region completeness, so
the ledger stays dogana's table inside the same catalog.

## Dagster and Airflow

Orchestrators schedule and display. In pull mode the orchestrator asks `plan_delivery` what is due,
runs the extractor, submits the result through the store, and emits an asset event with the
window in its payload. Dagster's per-partition status coincides with the ledger only when every run
is a full replace of its partition with exhaustive checks; for windows, keyed upserts and tombstones
the ledger is richer and should feed the orchestrator, not the reverse.

## Soda, Great Expectations, observability

Quality tools test row values; observability tools watch freshness and volume after arrival. A
missing delete changes neither row counts nor freshness, so it is invisible to both. The gate runs
the quality tool against the candidate (Soda's Python API returns pass, warn or error for a contract
run; Great Expectations evaluates a batch) and records the outcome as one of its checks. The ledger
then carries the one fact these tools cannot produce: that deletions are known as of a date.

## Where the manifest comes from

- Push from a sender you do not control: a JSON sidecar next to the file, or a file-name pattern
  declared in the contract that yields the manifest. The gate verifies the claim against the rows.
- Pull from your own extractor: the contract parameterises the read and the extractor writes the
  manifest, so claim and rows cannot disagree.
- From a replication tool: the adapter translates the sync configuration into a shape and the sync
  result into a manifest.

In all three the ledger, the receipts and the claims are the same objects.
