# Reference implementations

These exist so the protocol has a running example and the conformance suite a subject. Their constraints are theirs, not the
protocol's.

## DuckDB store

The DuckDB reference implementation uses a transaction against the published table, with the incoming file held in a
temporary candidate table first. DuckDB isolation hides uncommitted changes and permits atomic rows,
ledger JSON and receipt JSON without a second mutable copy. Immutable publication tables support
`read(..., publication_id=...)`; these copies deliberately trade storage for a small reference implementation.
`__dogana_publications` stamps rows with their publication identity in the same transaction as
the ledger and receipt. `load_ledger` raises `InconsistentPublication` on disagreement, naming the
ahead side. `repair(contract)` folds receipts and restores a missing or stale ledger only when the
result matches the stamp. A ledger ahead of the stamp requires a new complete whole-table delivery;
a window or a replay cannot repair rows. An unknown stamp or an incomplete receipt chain needs
investigation rather than a ledger reset.
Contracts cannot change within a table's history. Stores for the same resolved DuckDB file share a process-local lock across checks and publication.
Two threads using one instance or two instances serialize; an older overlapping delivery gets a
rejected receipt. Reads and ledger loads on these instances use the same lock. This is a single-process
reference implementation: independent processes are subject to DuckDB's file locking and cannot share
this Python lock. Direct SQL writers must coordinate with it. A stamp cannot detect a writer that
changes rows without updating their stamp.
The DuckDB extra includes `pytz`, required by its Python client when returning timezone-aware columns.
The internal `__dogana_` namespace is reserved regardless of letter case. DuckDB refuses a spelling
alias of an already registered table because its SQL identifiers are case-insensitive, while core
table identities preserve spelling.


## File drop

The file-drop reference implementation accepts CSV or Parquet paths. `<file>.json` is the optional sidecar. Without it,
an explicit full-match filename regular expression supplies `produced_at` and optionally `shape`,
`rows`, `low`, `high`, `since`, `generation`. `FilePattern(expression, shape="daily")` declares the
shape once when the sender cannot provide a sidecar or a shape in the name. Conflicting filename
and fixed shapes are refused. No shape anywhere is refused. Without a `rows` group the reference
implementation counts CSV records (including quoted newlines) or Parquet rows. Parquet counting
requires the `duckdb` extra. Without bounds groups, a window shape uses its `window_size` ending
at `produced_at`: thirty DATE days ending October 19 start September 20; datetime bounds span the
exact duration. A window shape without either bounds or `window_size` is refused. Both bounds
groups must be present together. `pattern.manifest(path, contract)` needs the file path when counting
rows, and accepts a filename alone when its groups carry the count. Bounds use the contract column's logical type;
`schema_hash` defaults to the declared schema hash and is checked against the actual file. File arrival
order is delivery order; there is no watcher, file mover or scheduler. CSV types are inferred from all
rows. For a header-only CSV, the declared schema supplies types because the payload contains no type
evidence. Parquet retains its physical schema even when empty. Successful candidates normalize logical types to DuckDB BIGINT, DOUBLE,
VARCHAR, BOOLEAN, DATE and TIMESTAMPTZ, so publication types do not depend on file inference. Neither encoding silently accepts
missing or retyped columns.


## Prove a store

Install `dogana[dev]` for pytest. `dogana.conformance` imports only the core, the standard library
and pytest; it has no DuckDB dependency. In your pytest module:

```python
import pytest
from dogana.conformance import StoreConformance
from your_project.testing import WarehouseFactory


class TestWarehouse(StoreConformance):
    @pytest.fixture
    def factory(self, tmp_path, monkeypatch):
        return WarehouseFactory(tmp_path, monkeypatch)
```

Implement the `StoreFactory` protocol: `make_store()`, `write_source(rows, schema)` and
`read_table(store, table)` supply the transport. `atomic_append` declares whether a transaction
or format commit makes append safe. The three fault hooks are deliberately destructive test
operations: `fail_after_rows(store)` is a context manager that interrupts one publication after
rows change; `set_ledger(store, ledger)` overwrites only the ledger; `set_stamp(store, table, id)`
overwrites only the stamp. Run against disposable storage. A store exposes `deliver`, `load_ledger`,
`receipts(table)` in persistence order, and `repair`; optional `close()` releases it after each test.

The inherited scenarios check rows, dates, audit history, replay, contradictory files, generation
breaks, overlapping windows, atomic failure or idempotent retry, append refusal, and both stamp
mismatches. Every completed delivery also rebuilds the ledger from accepted receipts. The repository
runs the same suite against DuckDB, a whole-state atomic in-memory implementation, and an ordered
in-memory implementation that writes rows, receipt and ledger separately and refuses append.
