# Reference

The [protocol](protocol.md) defines required behavior. This page covers the command line, the
accounting path without a store, the conformance suite, and the example store and file drop in the
repository's `examples/duckdb`, not installed with dogana: `uv pip install -e . -e examples/duckdb`
from a checkout.

## Command line

`dogana`, also `python -m dogana`, hashes files but never parses them.

```text
dogana manifest FILE --contract PATH --shape NAME --produced-at TIME --rows N [options]
dogana check FILE --contract PATH [--manifest PATH]
dogana claims LEDGER [--at TIME] [--limit-days DAYS] [--window LOW HIGH]
```

`manifest` writes `FILE.json`, validated against the contract, with the SHA-256 of FILE and the
contract's schema hash. `--rows` declares the live rows and, for a tombstone shape, `--tombstones`
the deleted ones; the receiver's store counts them again. Values are typed by the contract:
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

```text
orders (-inf, 2026-09-26]: unknown; complete as of unknown; deletions as of unknown
orders [2026-09-27, 2026-10-26]: complete; complete as of 2026-10-26T06:00:00+00:00; deletions as of 2026-10-26T06:00:00+00:00
orders [2026-10-27, +inf): unknown; complete as of unknown; deletions as of unknown
orders: unknown; exact at no single instant
```

Exit status is 0 on success, 1 when dogana refuses (an invalid contract, manifest or ledger, a
mismatch, an unreadable file, an existing manifest) and 2 when the command is called wrongly.

## Without a Store

A loader can use the gate without implementing `Store`. After answering a replay with its accepted
receipt, a new manifest takes six calls:

```python
ledger = load_consistent_ledger(contract)
requirements = Gate.requirements(contract, manifest)
outcomes = check_candidate(source, contract, manifest, requirements)
receipt = Gate.evaluate(ledger, manifest, outcomes, now=now, source=description)
pending_rows = execute_if_accepted(source, receipt)
commit_or_record(pending_rows, receipt)
```

`Gate` comes from `dogana`; the rest is your loader's. Check the candidate against the ledger whose
head matches the rows, execute only an accepted plan, and in `commit_or_record` compare the head
with `receipt.base_publication_id`, then commit rows, ledger, stamp and receipt together, or record
the rejection alone. `Gate.preflight` catches a malformed declaration before any row is read.
Without atomic publication, nothing guarantees the dates describe the visible rows.

## Check a store

`dogana.conformance.StoreConformance`, which needs pytest, runs against a `StoreFactory` with
`make_store`, `write_source`, `read_table`, `read_publication`, `fail_after_rows` and
`pause_after_rows`; the last two drive one `_publication_point()` hook between provisional row
changes and commit. The example's factory is in `tests/test_conformance.py`.

`StampedLedgerConformance` adds recovery scenarios for stores with `publication_stamp` and `repair`
(`StampedStoreFactory` adds `set_ledger`, `set_stamp` and `remove_ledger`). `TogetherConformance`
adds groups for stores with `check_references` and `publish_together`. These cover one instant, a
refusal refusing every table, untested references, different read boundaries, failures after rows, a
table published alone between check and publication, receipts whose `references` answer the store
did not compute itself, and whole replay. Passing is evidence for these scenarios, not a proof under
every storage failure.

## Example store on DuckDB

`DuckDBStore(path)` opens the DuckDB database at `path`. A delivery's source is a CSV
or Parquet path, row dictionaries, or an Arrow table. A path is copied into the store's private
folder while it is hashed, and only that copy is read.
`deliver(store, contract, source, manifest, now)` commits rows, ledger, stamp and receipt in one
transaction; a refusal changes nothing published, and `StoreError` means storage failed.

- **Types.** Bare CSV and row dictionaries infer types. Use `FileDrop(..., columns=columns)` for
  typed CSV, and `Rows(rows, columns)` for row mappings that are empty or all null, such as an empty
  API poll. A column declared `decimal(p,s)` (precision up to 38) stays exact. It accepts a Parquet
  DECIMAL of that precision and scale, a CSV value with at most `s` fraction digits and no exponent,
  or a Python `Decimal`; it is never read as a float. A `datetime` without an offset fails
  `schema_hash`. Large batches load far faster as a file than as row mappings.
- **Tombstones** move to a staging table of keys and the deleted column is dropped, so checks and
  the published table see live rows only. Deliver them from a typed source.
- **CSV** follows the protocol's grammar; a quoted empty field is the empty string, an unquoted one
  null. A file DuckDB cannot read is a recorded rejection.
- **Existing tables** without evidence are refused: the store publishes only tables it created. The
  `__dogana_` prefix is reserved, and a table's spelling must not change, since DuckDB ignores case.
- **Concurrency.** Use one writer process per file; stores on it share a process-local lock and
  refuse a second name for it, such as a hard link. After a rollback that could not be resolved,
  the store refuses reads until it is closed, from the thread that used it. External writers must
  coordinate, and a row edit that keeps the stamp is undetectable.
- **Reads.** `read(table, publication_id=None)` returns row dictionaries, of a past version when
  given one; `read_publication(contract)` returns rows with their ledger under one publication, and
  `read_together(group)` every table of a group under one lock, for `group.claims(ledgers, now,
  limit=...)`. `receipts(table)` lists stored decisions in order, rejections included; a replay is
  answered from its stored receipt.
- **Groups.** `check_references` tries every candidate's plan in a transaction it rolls back and
  counts orphan child rows, naming up to three values; it costs about as much as the delivery, and a
  new check of any candidate discards its answer. A reference whose columns are missing, or whose
  type differs from the parent key's other than int and float, fails.

## Internal tables and ledger JSON

| Table | Holds |
|---|---|
| `__dogana_ledgers` | `table_name`, `document`: the current ledger JSON |
| `__dogana_receipts` | every decision's JSON, with `receipt_id`, `table_name`, `manifest_id`, `accepted`, `publication_id` |
| `__dogana_publications` | each table's current publication stamp |
| `__dogana_commits` | accepted publications in order, with their row-version metadata |
| `__dogana_history_<table>`, `__dogana_layout_<hash>` | changed row versions, from publication sequence `__dogana_from` included to `__dogana_until` excluded |

A ledger `document` holds `contract`, `regions`, `generation` and `publication_id`; each region
holds `low`, `high`, `low_open`, `high_open`, `complete_as_of` and `deletions_as_of`, null when
unknown or unbounded (see the [ledger fixture](examples/ledger.json)). This view exposes them:

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

The view reads current evidence, without pinning a publication or computing freshness. SQL `min`
ignores nulls, so require every region's date to be non-null before aggregating, as the
[dbt recipes](integrations.md#dbt) do. History has no retention policy.

## Recovery and contract changes

`load_ledger` raises `InconsistentPublication` when ledger, receipts and stamp disagree;
`repair(contract)` restores the ledger from the receipts only if the rebuilt head equals the stamp.
Never edit a stamp. Untrusted rows are replaced by a newer complete whole-table read, never by a
window or a replay. A new `Contract` may change policies and shapes but not the table, key, window
column or their types; a changed row schema needs a complete whole-table delivery. A source
restored from backup invalidates a timestamp checkpoint: request a complete read. Generation tokens
must tell source incarnations apart.

## File drop

`FileDrop(store, contract, pattern=None, *, columns, root=None).ingest(path, *, now=None)` delivers
a CSV or Parquet file. `columns` maps every data column to its type in order, for example `{"id":
"int", "day": "date", "amount": "int"}`, agreeing with the contract; without the deleted column,
files deliver no tombstones. The manifest is the sidecar `<file>.json`, or a `FilePattern` derives
it from the file name, which must give `produced_at` and the shape, and may give `rows`,
`tombstones`, `low`, `high`, `since`, `generation` and `schema_hash`:

```python
from datetime import UTC
from example_store import FilePattern

pattern = FilePattern(r"orders_(?P<produced_at>\d{4}-\d{2}-\d{2})\.csv", shape="daily", timezone=UTC)
```

A file-name time needs a declared timezone. A missing window is derived from the shape's
`window_size`. Without a `rows` group, the drop counts the file, and a manifest without a digest
gets the file's SHA-256, so a changed file is a new delivery, not a replay. The drop cannot tell a
half-written file: senders write under another name and rename the finished file into the folder.
Files must be regular files inside the drop root, symlinks included. Nothing limits size: compressed
Parquet can expand far beyond its file, so limit memory at the caller.

`ingest_together(group, files, *, now=None)` delivers one file per table of a group, each pairing
its table's `FileDrop` with a path. Every file must carry the instant of the one read that produced
them all: a date-only file name gives separate jobs one instant and voids the guarantee.
