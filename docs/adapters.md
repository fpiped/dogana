# Reference adapters

The adapters prove the core's interfaces are sufficient. Their constraints are theirs, not the
protocol's.

## DuckDB store

The DuckDB adapter uses a transaction against the published table, with the incoming file held in a
temporary candidate table first. DuckDB isolation hides uncommitted changes and permits atomic rows,
ledger JSON and receipt JSON without a second mutable copy. Immutable publication tables support
`read(..., publication_id=...)`; these copies deliberately trade storage for a small reference adapter.
Contracts cannot change within a table's history. One store instance serializes deliveries.
The DuckDB extra includes `pytz`, required by its Python client when returning timezone-aware columns.
The internal `__dogana_` namespace is reserved regardless of letter case. DuckDB refuses a spelling
alias of an already registered table because its SQL identifiers are case-insensitive, while core
table identities preserve spelling.


## File drop

The file-drop adapter accepts CSV or Parquet paths. `<file>.json` is the optional sidecar. Without it,
an explicit full-match filename regular expression supplies named groups `shape`, `produced_at`,
`rows`, and optional `low`, `high`, `since`, `generation`. Bounds use the contract column's logical type;
`schema_hash` defaults to the declared schema hash and is checked against the actual file. File arrival
order is delivery order; there is no watcher, file mover or scheduler. CSV types are inferred from all
rows. For a header-only CSV, the declared schema supplies types because the payload contains no type
evidence. Parquet retains its physical schema even when empty. Successful candidates normalize logical types to DuckDB BIGINT, DOUBLE,
VARCHAR, BOOLEAN, DATE and TIMESTAMPTZ, so publication types do not depend on file inference. Neither encoding silently accepts
missing or retyped columns.

