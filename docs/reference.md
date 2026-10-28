# Reference

The [protocol](protocol.md) defines required behavior. This page covers the command line, the
accounting path without a store, the conformance suite, and the example store and file drop in the
repository's `examples/duckdb`, which are not installed with dogana: install them from a checkout
with `uv pip install -e . -e examples/duckdb`, copy them, or write a store for your own database.

## Column types and hashes

`Contract.types` maps exactly the key, window, cursor and deleted columns to logical types: `int`,
`str`, `date` or `datetime` for key and window, `datetime` or `int` for a cursor, `bool` for the
deleted column. The optional, opaque `schema_hash` comes from the data contract that owns the
columns, computed with the [protocol's recipe](protocol.md#contract-and-shapes); when present, the
gate requires the manifest's hash and the candidate's to equal it. The deleted column is never
published, so the hash leaves it out. The example store uses the recipe; the
[quick start](quickstart.md) hashes a hand-written column mapping.

## Example store on DuckDB

`DuckDBStore(path, adopt=False)` accepts a CSV or Parquet path, an iterable of row dictionaries,
or an Arrow table when PyArrow is installed. A path is opened once, without following a final
symbolic link, and copied into the store's private folder while it is hashed; every read uses that
copy, so a file replaced afterwards is never read.

`hold(source, manifest, contract)` stages the candidate. Bare CSV paths and row dictionaries infer
types; for typed CSV use `FileDrop(store, contract, columns=columns)`, which also types files with
only nulls or no rows. Row mappings cannot infer types from an empty input or an all-null column:
`Rows(rows, columns)` supplies them, so an empty API poll is still a delivery. Row mappings load far
slower than a file; deliver large batches as CSV, Parquet or Arrow. Candidates normalize to BIGINT,
DOUBLE, VARCHAR, BOOLEAN, DATE, TIMESTAMPTZ and DECIMAL. A column declared `decimal(p,s)` in
`columns` (precision up to 38) stays exact: a Parquet DECIMAL of that precision and scale, a CSV
value with at most `s` fraction digits and no exponent, or a Python `Decimal`; a DECIMAL is never
read as a float. Key, window and cursor columns keep the contract's types.

Tombstone rows move to a staging table of their keys, and the deleted column is dropped, so every
later check and the published table see live rows only. A CSV read without `columns` infers an
all-empty column as text, so deliver tombstones through a typed source. CSV follows the protocol's
one grammar, so a `'` or a leading `#` is data; a quoted empty field is the empty string, an
unquoted one null. A `datetime` without an offset fails `schema_hash`, as does a Parquet `TIMESTAMP`
without a time zone. A file DuckDB cannot read (not UTF-8, compressed, corrupt, out of range) is a
recorded rejection.

`deliver(store, contract, source, manifest, now)` commits rows, ledger, stamp and receipt in one
transaction; a rejection changes nothing published. `StoreError` means storage failed, not the
declaration. Unexpected failures roll back; if a commit succeeded before cleanup failed, a retry
finds its receipt.

An existing table without publication evidence is refused unless `adopt=True`, which certifies
none of its rows: establish evidence with a complete read. The `__dogana_` prefix is reserved in any
case, and a table's spelling must stay the same, because DuckDB identifiers ignore case and
protocol identities do not.

Stores on one database file share a process-local lock, keyed by the file DuckDB opened, and a
second name for an open file, such as a hard link, is refused. Checks and publication serialize;
reads never see an unfinished transaction. After a rollback that could not be resolved, the store
refuses reads until it is closed, from the thread that used it. Use one writer process per file;
external SQL writers must coordinate with the store, and a row edit that leaves the stamp unchanged
is undetectable.

`read(table, publication_id=None)` returns row dictionaries, of a past version when given its
publication identity. `read_publication(contract)` returns the current rows with their ledger under
one publication; reading rows and claims separately can straddle one. `receipts(table=None)` returns
the stored decisions in order, rejections included: a history of decisions, not of every call. A
replay is answered from its stored receipt, with its original identity and `received_at`, and is
not stored again.

The store implements the [together tier](protocol.md#tables-together). `check_references(group,
candidates)` applies every candidate's plan in a transaction it rolls back and, for each reference
`Group.unchecked` leaves to test, counts the child rows in the child's coverage that name no parent,
reporting up to three values. A reference fails when its columns are missing; a reference column whose type differs from its parent key column's,
other than int and float, fails as soon as a row names a parent; a float names an int key only with
its exact integer value. A new check of any candidate discards the answer, and the trial costs
about as much as the delivery. `publish_together(group,
entries)` publishes every table in one transaction, and `read_together(group)` returns `(rows,
ledgers)` under one lock; pass the ledgers to `group.claims(ledgers, now)` for the common instant.

## Internal tables and ledger JSON

These tables are implementation details, useful for inspection and SQL:

| Table | Columns | Purpose |
|---|---|---|
| `__dogana_ledgers` | `table_name`, `document` | Current canonical ledger JSON. |
| `__dogana_receipts` | `receipt_id`, `table_name`, `manifest_id`, `accepted`, `publication_id`, `document` | Persisted decision JSON and lookup indexes. |
| `__dogana_publications` | `table_name`, `publication_id` | Current row publication stamp. |
| `__dogana_commits` | `publication_id`, `table_name`, `manifest_id`, `base_publication_id`, `receipt_id`, `sequence`, `history_table`, `columns` | Accepted publication order and row-version metadata. |
| `__dogana_history_<table>`, `__dogana_layout_<hash>` | Declared row columns, `__dogana_from`, `__dogana_until` | Row versions, with inclusive start and exclusive end publication sequences. |

The ledger `document` holds `version`, `kind`, `contract`, `regions`, `generation` and
`publication_id`. Every region holds `low`, `high`, `low_open`, `high_open`, `complete_as_of` and
`deletions_as_of`; unknown dates and unbounded ends are null, a datetime is
`{"$datetime":"2026-10-18T00:00:00+00:00"}` and a date `{"$date":"2026-10-18"}`. See the
[ledger fixture](examples/ledger.json).

This view exposes the dates without dropping unknown regions or endpoint flags:

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
from __dogana_ledgers l,
     json_each(l.document, '$.regions') r;
```

It reads current evidence, without pinning a publication or computing freshness. SQL `min` ignores
nulls, so require every region's date to be non-null before aggregating, as the
[dbt recipes](integrations.md#dbt) do.

History stores changed row versions, not a copy per publication: keyed updates compare rows with
null-safe equality and unchanged rows keep their version; equal keyless events keep their
multiplicity. History has no retention policy. Evolved schemas use layout tables named by a hash of
the table and its columns. A receipt names the ledger it leaves by identity, so it grows with its
delivery's coverage, not the table's history.

## Recovery and contract changes

`load_ledger` raises `InconsistentPublication` when ledger, receipts and row stamp disagree.
`repair(contract)` folds the accepted receipts and restores the ledger only if the rebuilt head
equals the stamp; missing receipts or an unknown stamp need investigation. A newer complete
whole-table read can replace untrusted rows; a window or a replay cannot. Never edit a stamp to make
it agree. Rebuilding receipts restores evidence, not historical rows.

A compatible new `Contract` may change freshness, shrink policy, costs and other nonbinding fields;
`Ledger.with_contract` refuses a changed table, key or window binding, types included. Establish a changed row
schema with a complete whole-table delivery; DuckDB refuses a layout change while earlier rows
remain after the plan's deletions. A source restored from backup can invalidate a timestamp
checkpoint: request a complete read. Generation tokens must distinguish source incarnations.

## File drop

`FileDrop(store, contract, pattern=None, *, columns, root=None).ingest(path, *, now=None)` delivers a
CSV or Parquet file. `columns` maps every data column to its logical type in order, for example
`{"id": "int", "day": "date", "amount": "int"}`: it types CSV parsing and gives the column hash. It
must agree with `Contract.types`, which is not a full input schema, and `schema_hash`; it may
include the deleted column as `bool`, and a mapping without it delivers no tombstones.
The manifest is the sidecar `<file>.json`, for example `orders.csv.json`; without one, a
`FilePattern` derives it from the file name with a full-match expression that names `produced_at`
and a `shape` group or argument, and optionally `rows`, `tombstones`, `low`, `high`, `since`,
`generation` and `schema_hash`. `now` defaults to the time of ingestion.

```python
from datetime import UTC
from example_store import FilePattern

pattern = FilePattern(r"orders_(?P<produced_at>\d{4}-\d{2}-\d{2})\.csv", shape="daily", timezone=UTC)
```

A file-name time without a declared timezone is refused, and an ambiguous or nonexistent local time
needs an explicit offset. Without bounds, a closed window is derived from the shape's
`window_size`, DATE windows on UTC calendar dates; explicit bounds come in pairs typed by the window
column.

Without a `rows` group, the drop counts the file: live rows and tombstones, CSV records including
quoted newlines. With one, the name declares the counts (`tombstones` zero when absent). A manifest
without a digest gets the SHA-256 of the received file, so a changed file under the same manifest is
a new delivery, not a replay. Count and hash describe the received file, not the source, and the drop cannot tell a
half-written file: senders write under another name and rename the finished file into the folder.

Files must be literal regular files inside the drop root (by default the file's parent), including
through symlinks, and the store opens the file it checked. These checks are not a sandbox: limit
input size and memory at the caller, since compressed Parquet can expand far beyond its size. There
is no watcher or mover.

`ingest_together(group, files, *, now=None)` delivers one file per table of a group through
`deliver_together`, pairing each table's `FileDrop` (one store, the group's contracts) with its
path. Every file must carry the instant of the one read that produced them all, in every sidecar or
as a full timestamp in every name: a date-only pattern gives separate jobs one instant and voids the
guarantee.

## Command line

`dogana`, also `python -m dogana`, hashes files but never parses them.

```text
dogana manifest FILE --contract PATH --shape NAME --produced-at TIME --rows N [options]
dogana check FILE --contract PATH [--manifest PATH]
dogana claims LEDGER [--at TIME] [--window LOW HIGH]
```

`manifest` writes `FILE.json`, validated against the contract. `--rows` declares the live rows and,
for a tombstone shape, `--tombstones` the deleted ones: the export knows them, and the receiver's
store counts them again. It adds the SHA-256 of the bytes and the contract's schema hash. Values are
typed by the contract: `--window LOW HIGH` by the window column, omitted for a rolling shape ending
at `--produced-at`; `--since` by the shape's checkpoint; `--generation` by the table's kind of
position. Times need a UTC offset,
and `--produced-at` is the source read, never when the command runs. An existing file is replaced
only with `--force`, atomically; `-o -` writes to standard output.

`check` validates FILE's manifest against the contract, schema hash included, and FILE's SHA-256
against its `content_hash`, refusing a manifest without one. A sender runs it before handing a file
over; counts, keys, windows and types are checked by the receiver's store.

`claims` prints what a ledger, as JSON (`store.load_ledger(contract).to_json()`), claims at `--at`,
now by default: one line per region, then the table's state and the instant it is exact at, if any.
`--window LOW HIGH` cuts the regions to that window. Date and integer bounds print closed.

```text
orders (-inf, 2026-09-26]: unknown; complete as of unknown; deletions as of unknown
orders [2026-09-27, 2026-10-26]: complete; complete as of 2026-10-26T06:00:00+00:00; deletions as of 2026-10-26T06:00:00+00:00
orders [2026-10-27, +inf): unknown; complete as of unknown; deletions as of unknown
orders: unknown; exact at no single instant
```

Exit status is 0 on success, 1 when dogana refuses (an invalid contract, manifest or ledger, a
mismatch, an unreadable file, an existing manifest) and 2 when the command is called wrongly.

## Without a Store

An existing loader can use the gate without implementing `Store`. After returning an existing
accepted receipt on replay, a new manifest takes six calls:

```python
ledger = load_consistent_ledger(contract)
requirements = Gate.requirements(contract, manifest)
outcomes = check_candidate(source, contract, manifest, requirements)
receipt = Gate.evaluate(ledger, manifest, outcomes, now=now, source=description)
pending_rows = execute_if_accepted(source, receipt)
commit_or_record(pending_rows, receipt)
```

`Gate` comes from `dogana`; the rest are your loader's. Load a ledger whose head matches the rows,
check the candidate against that base, and execute only an accepted plan. `commit_or_record`
compares the current head with `receipt.base_publication_id`, then commits rows, ledger, stamp and
receipt together, or records the rejection alone. `Gate.preflight(ledger, manifest, now=now)`
catches a malformed declaration before any row is read. Without atomic publication, nothing
guarantees the dates describe the visible rows.

## Check a store

Run `dogana.conformance.StoreConformance`, which needs pytest, against a `StoreFactory` with
`make_store`, `write_source`, `read_table`, `read_publication`, `fail_after_rows` and
`pause_after_rows`; the last two drive one `_publication_point()` hook, after provisional row
changes and before commit. The example's factory is in `tests/test_conformance.py`.

A store with `publication_stamp` and `repair` can also run `StampedLedgerConformance`, whose
`StampedStoreFactory` adds `set_ledger`, `set_stamp` and `remove_ledger`; diagnosis matches
`LedgerAhead` and `StampAhead` by type. A store with `check_references` and `publish_together` can
run `TogetherConformance`: one instant for a group, a refusal refusing every table, an untested
reference, different read boundaries, a failure after rows, a table published alone between check
and publication, receipts without the store's own references answer, and whole replay.

The scenarios cover full and window deliveries, feeds, contradictions, replays, stale bases, atomic
failures, receipt persistence and reconstruction. Passing is evidence for them, not a proof under
every storage failure. The port is eight storage methods; factories adapt reads and transport to the
suite, and ordered writes do not conform.
