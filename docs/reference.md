# Reference implementations

These make the protocol executable. Their constraints belong to these implementations, not to the
protocol.

## DuckDB store

The DuckDB reference implementation holds the incoming CSV or Parquet file in a temporary candidate.
It checks the candidate before a transaction changes the published table. Rows, publication stamp,
ledger and receipt commit together. Unexpected exceptions and interrupts abort the candidate before
propagating. Cleanup failure after commit returns the committed receipt for handled I/O errors;
resubmission returns it after an interrupt.

`__dogana_publications` holds the row stamp. `load_ledger` raises `InconsistentPublication` when it
differs from the ledger, naming the ahead side. `repair(contract)` folds receipts and restores a lost
or stale ledger only when the result matches the stamp. A ledger ahead of the stamp requires a new
complete whole-table delivery. A window or replay cannot repair rows. Lost ledger and stamp records
with retained receipts also require a new complete delivery, continuing the retained chain. Unknown
stamps or incomplete receipt chains require investigation.

Row versions in `__dogana_history_<table>` support `read(..., publication_id=...)`. Each version has
its first publication sequence and exclusive end sequence. Keyed publications compare live rows
with active versions using null-safe equality. Unchanged rows survive without copies. Append adds
candidate events, including equal events. History grows with changed rows; comparisons still scan
the table. Stored receipt JSON replaces accepted-identity lists with internal prefix counts over
the receipt index. `receipts()` restores the public wire value. The current ledger retains all
accepted identities; requesting every full receipt materializes their historical prefixes.

Stores for the same resolved file share a process-local lock across checks and publication.
Two threads using one instance or two instances serialize; an older overlapping delivery gets a
rejected receipt. Reads and ledger loads use the same lock. Independent processes remain subject
to DuckDB's file locking. Direct SQL writers must coordinate with the store. A stamp cannot detect
a writer that changes rows without changing the stamp.

Contracts cannot change within a published table's history. The `__dogana_` namespace is reserved
regardless of case. A spelling alias of a table registered by a ledger, stamp or receipt is refused:
DuckDB identifiers ignore case, while core table identities preserve it. The `duckdb` extra includes
`pytz`, needed by DuckDB's client when returning timezone-aware columns.

## File drop

The file-drop reference implementation accepts CSV or Parquet paths. `<file>.json` is an optional
sidecar. Without it, a full-match filename expression supplies `produced_at` and optionally `shape`,
`rows`, `low`, `high`, `since`, `generation` and `schema_hash`.

`FilePattern(expression, shape="daily")` declares the shape once when the filename omits it.
Conflicting fixed and filename shapes are refused, as is a missing shape. Without a `rows` group,
CSV records are counted, including quoted newlines; Parquet counting requires `dogana[duckdb]`.
`pattern.manifest(path, contract)` needs the file path when counting, and accepts a filename alone
when its groups carry the count.

Without bounds groups, a window derives its closed interval from `window_size`, ending at the UTC
`produced_at` boundary. Thirty DATE days ending October 19 start September 20. Datetime bounds span
the exact duration. A window without bounds or a duration is refused; bounds groups come in pairs.
Explicit bounds use the window column's logical type. Filename dates without an offset mean UTC.
The schema hash defaults to the contract's hash and is checked against the file.

File arrival order is delivery order. CSV inference reads all rows. Header-only CSVs use declared
types; empty Parquet retains its physical schema. Successful candidates normalize to BIGINT, DOUBLE,
VARCHAR, BOOLEAN, DATE and TIMESTAMPTZ. Missing or retyped columns are refused. There is no watcher,
file mover or scheduler.

## Prove a store

Install `dogana[dev]` for pytest. `dogana.conformance` imports the core, the standard library and
pytest, with no DuckDB dependency. In your pytest module:

```python
import pytest
from dogana.conformance import StoreConformance
from your_project.testing import WarehouseFactory


class TestWarehouse(StoreConformance):
    @pytest.fixture
    def factory(self, tmp_path, monkeypatch):
        return WarehouseFactory(tmp_path, monkeypatch)
```

Implement `StoreFactory`: `make_store()`, `write_source(rows, schema)` and
`read_table(store, table)` supply the transport. `atomic_append` declares whether a transaction
or format commit makes append safe. Three hooks inject faults against disposable storage:

- `fail_after_rows(store)`: a context manager interrupting one publication after rows change.
- `set_ledger(store, ledger)`: overwrite only the ledger.
- `set_stamp(store, table, id)`: overwrite only the stamp, or remove it for `None`.

The store facade exposes `deliver`, `load_ledger`, `receipts(table)` in persistence order, and
`repair`. Optional `close()` releases it after each test; candidate staging methods are unnecessary.

Scenarios cover a week of ERP deliveries, rows, dates, receipts, claims, contradictory deliveries,
generation breaks, overlapping windows, replay, atomic failure or idempotent retry, append refusal
and both stamp mismatches. Completed deliveries rebuild the ledger from receipts. The repository
runs this suite against DuckDB, an in-memory implementation that swaps its whole state, and an
ordered implementation that writes rows, receipt and ledger separately and refuses append.
