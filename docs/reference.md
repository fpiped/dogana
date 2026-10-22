# Reference implementations

The [protocol](protocol.md) defines required behavior. This page describes the shipped DuckDB and
file-drop implementations and how to supply your own storage.

## DuckDB store

`DuckDBStore(path, adopt=False)` accepts a CSV or Parquet path, an iterable of row dictionaries,
or an Arrow table when PyArrow is installed.
`hold(source, manifest, contract)` stages the candidate with declared types. CSV values are parsed
using the contract schema, not inferred from whether a particular file happens to contain decimals
or nulls. Parquet retains its physical schema for validation. Successful candidates normalize to
BIGINT, DOUBLE, VARCHAR, BOOLEAN, DATE and TIMESTAMPTZ. CSV datetime values without an offset
are interpreted as UTC by the store session; include offsets when the source uses another timezone.
This CSV convention does not relax the requirement for aware datetimes in core values and manifests.

Call `dogana.deliver(store, contract, source, manifest, now)` with the actual receiver time.
The store's `deliver` convenience uses the same core sequence. Rows, ledger, stamp and receipt commit
in one transaction. A rejected receipt changes none of the published rows or evidence. `StoreError`
means storage failed; it is not a rejection of the declaration. Unexpected failures roll back and
release candidates. If a commit succeeded before cleanup failed, retry finds the committed receipt.

An existing destination table without publication evidence is refused unless `adopt=True` is
explicitly chosen. Adoption does not certify its existing rows; establish evidence with a complete
read. The `__dogana_` prefix is reserved regardless of case. Registered table spelling must remain
consistent because DuckDB identifiers are case-insensitive while protocol identities are not.

Stores on the same resolved file share a process-local lock. Checks and publication serialize;
reads cannot observe an unfinished transaction. Use one writer process per DuckDB file. External
SQL writers must coordinate with the store. A direct row edit that leaves the stamp unchanged is
not detectable by stamp comparison.

`read(table, publication_id=None)` returns row dictionaries; pass a receipt's publication identity
to read its version. Pair that version with the receipt's ledger, recomputing claims with today's
`now`. Loading current rows and current claims separately can cross a publication boundary.
`receipts(table=None)` returns persisted decisions in order, including rejections. A returned replay
has `outcome="replayed"` and preserves original identity and `received_at`; it is not another commit.
This is a decision history, not a journal of every call or operational exception.

## Internal tables and ledger JSON

These tables are implementation details, useful for inspection and SQL consumers:

| Table | Columns | Purpose |
|---|---|---|
| `__dogana_ledgers` | `table_name`, `document` | Current canonical ledger JSON. |
| `__dogana_receipts` | `receipt_id`, `table_name`, `manifest_id`, `accepted`, `publication_id`, `document` | Persisted decision JSON and lookup indexes. |
| `__dogana_publications` | `table_name`, `publication_id` | Current row publication stamp. |
| `__dogana_commits` | `publication_id`, `table_name`, `manifest_id`, `base_publication_id`, `receipt_id`, `sequence`, `history_table`, `columns` | Accepted publication order and row-version metadata. |
| `__dogana_history_<table>`, `__dogana_layout_<hash>` | Declared row columns, `__dogana_from`, `__dogana_until` | Row versions, with inclusive start and exclusive end publication sequences. |

`document` in the ledger table contains `version`, `kind`, `contract`, `regions`, `generation` and
`publication_id`. It has no accepted-identity array. Every region contains `low`, `high`, `low_open`,
`high_open`, `complete_as_of` and `valid_as_of`. Unknown dates and unbounded endpoints are JSON null;
a datetime is `{"$datetime":"2026-10-18T00:00:00+00:00"}` and a date is
`{"$date":"2026-10-18"}`. Contract and region values also carry `version` and `kind`.
The [complete ledger fixture](examples/ledger.json) is executable JSON.

Create this DuckDB view to expose dates without discarding unknown regions or endpoint flags:

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
       json_extract_string(r.value, '$.valid_as_of."$datetime"')::timestamptz as valid_as_of
from __dogana_ledgers l,
     json_each(l.document, '$.regions') r;
```

The view reads current ledger evidence. It does not fix a publication for later queries or calculate
freshness. SQL `min` ignores nulls, so require every region's date to be non-null before aggregating.
The [dbt recipes](integrations.md#dbt) use that rule and refuse a missing ledger too.

Evolved schemas use layout tables named by a hash of the table and physical columns.
History stores changed row versions instead of copying the whole table on every publication.
Keyed updates compare rows with null-safe equality; unchanged rows keep their version. Equal
keyless events retain their multiplicity. History grows with changes and has no automatic retention
policy; comparison still scans published rows. Accepted-receipt lookup carries replay history outside
the ledger, so embedded ledger size depends on regions rather than the number of deliveries.

## Recovery and contract changes

`load_ledger` raises `InconsistentPublication` when ledger, retained receipts and row stamp disagree.
`repair(contract)` folds accepted receipts and restores a missing or stale ledger only if the
reconstructed head equals the stamp. Missing accepted receipts or an unknown stamp require
investigation. A newer complete whole-table read can replace untrusted rows while continuing a
recoverable history; a window or replay cannot repair them. Never edit a stamp to make it agree.
Rebuilding a receipt prefix restores evidence only, not the historical rows.

Use a compatible new `Contract` to change freshness, shrink policy, costs or other nonbinding fields.
`Ledger.with_contract` enforces unchanged table, key and window bindings, including their types.
Receipts preserve the contract used for each decision. Establish changed row schemas with a complete
whole-table delivery so retained rows have defined values under the new schema. A source restoration
can invalidate an unchanged timestamp checkpoint: request a genuinely complete read instead of
continuing from its old numeric value. Opaque generation tokens must distinguish source incarnations.

## File drop

`FileDrop(store, contract, pattern=None, root=None).ingest(path, now=None)` accepts CSV or Parquet files.
The sidecar is `<file>.json`, for example `orders.csv.json`. Without it, `FilePattern` derives a
manifest using a full-match filename expression. It requires `produced_at` and either a named
`shape` group or a fixed `shape` argument. Optional groups include `rows`, `low`, `high`, `since`,
`generation` and `schema_hash`. The file drop supplies receiver time at ingestion unless `now` is explicitly supplied.

For filenames such as `orders_2026-10-19.csv`, declare the timezone instead of assuming one:

```python
from datetime import UTC
from dogana.reference.files import FilePattern

pattern = FilePattern(r"orders_(?P<produced_at>\d{4}-\d{2}-\d{2})\.csv", shape="daily", timezone=UTC)
```

Naive filename timestamps without a declared timezone are refused. Ambiguous or nonexistent local
times need an explicit offset. DATE windows use UTC calendar dates. Missing bounds derive a closed
window from the shape's `window_size`; explicit bounds must appear as a pair and match the schema.
Thirty DATE days ending October 19 start September 20. A window lacking both bounds and duration
is refused. The duration is not a restriction on explicitly declared push windows.

Without a `rows` group, CSV records are counted, including quoted newlines; Parquet counting needs
the DuckDB extra. Path-based receipts identify the source. Filename derivation does not add a content hash;
compute one and include it in a supplied manifest when payload identity is required. Count and hash
describe the received file, not whether it contains all source rows.
Literal regular files are required; glob characters are refused. Resolved files and sidecars must
remain inside the drop root, including through symlinks. With no root, the submitted file's parent
is the boundary. These checks are not a sandbox or a resource quota: limit input size and memory
at the caller. Compressed Parquet can expand far beyond its file size. There is no watcher or mover.

## Without a Store

An existing loader can use the gate without implementing `Store`. For a new, structurally valid
manifest after accepted-receipt lookup, the publication path is six calls:

```python
ledger = load_consistent_ledger(contract)
requirements = Gate.requirements(contract, manifest)
outcomes = check_candidate(source, contract, manifest, requirements)
receipt = Gate.evaluate(ledger, manifest, outcomes, now=now, source=description)
pending_rows = execute_if_accepted(source, receipt)
commit_or_record(pending_rows, receipt)
```

`Gate` comes from `dogana`; the other functions are your loader's primitives. Load a ledger whose
head matches the rows, check the candidate against that base, and execute only an accepted plan.
`commit_or_record` compares the current head with `receipt.base_publication_id` and commits rows,
ledger, stamp and acceptance together, or persists the rejection without changing published state.
Preflight malformed declarations with `Gate.preflight(ledger, manifest, now=now)` and record their
rejection through `Gate.evaluate`. Abort open work on errors. Before this path, return an existing
accepted receipt on replay without executing its plan again. Using the core for accounting without
an atomic publication mechanism gives no guarantee that the evidence describes the visible rows.

## Check a store

Install the `dev` extra and run `dogana.conformance.StoreConformance` against a `StoreFactory`.
The suite depends on pytest, not DuckDB. Required factory hooks are `make_store`, `write_source`,
`read_table`, `read_publication`, `fail_after_rows` and `pause_after_rows`. The latter two exercise
one `_publication_point()` hook inside publication after provisional row changes and before commit.

Stores exposing both `publication_stamp` and `repair` can also inherit `StampedLedgerConformance`;
its `StampedStoreFactory` adds `set_ledger`, `set_stamp` and `remove_ledger` for recovery scenarios.
Stores without that tier still run every required publication scenario. Diagnosis matches
`LedgerAhead` and `StampAhead`, never exception text.

Scenarios cover full and window deliveries, feeds, contradictions, checks, replays, stale accepted
and rejected bases, atomic failures, malformed receipt persistence and reconstruction. A refusal
whose receipt cannot round-trip raises `SerializationError` before the log changes. The DuckDB
reference also refuses a destination with unexpected column order or types before publication.
Passing provides evidence for these cases, not a proof under every storage failure or of source truth.
The core port requires seven storage primitives, not a `deliver` or `read` method; factories adapt
transport and read APIs to the suite. Ordered writes are not a conforming alternative.
