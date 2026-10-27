# Changelog

## 0.3.0 (2026-10-27)

Tables that depend on each other can be delivered and read together. The wire format is unchanged,
so stores and documents written by 0.2.0 keep working.

- `Claims.exact_as_of` is the instant at which every region equals the source, if there is one. On
  claims combined across tables it is their common instant: tables each exact at the same instant
  answer every query, joins and `NOT EXISTS` included, as the source would have.
- `Group` names tables read together; `Reference` states that each row of one table names the key
  of a row of another. `Group.claims` combines the tables' claims, each over an optional window.
- `deliver_together` decides one delivery per table as one: the files must come from one consistent
  read and share its instant, a refused table refuses them all, and every table is published in one
  atomic step. A group replays whole or is refused whole. Receipts gain the `together` and
  `references` checks.
- A reference is checked only where an orphan proves the shared instant false: the child exact
  where it was delivered and the parent complete over the whole table. `Group.unchecked` names the
  references a delivery cannot test, and the receipt says why.
- `plan_together` names one read per table at one shared boundary.
- `TogetherStore` is an optional store tier (`check_references`, `publish_together`), tested by
  `TogetherConformance`. The DuckDB store implements it and adds `read_together`; the file drop adds
  `ingest_together`.
- The DuckDB store shares one lock among stores on one database file whatever path names it, and
  refuses a second name for an open file, such as a hard link. While a rollback is unresolved it
  refuses reads of receipts and stamps too, and closing it releases the lock.
- In a CSV delivery to the DuckDB store, a quoted empty field is now the empty string; an unquoted
  empty field is still null.
- A window bound at the end of the date, timestamp or integer range now covers every stored value on
  that side, as the ledger does, including values past what Python can name.

## 0.2.0 (2026-10-27)

Deletions travel in the delivered file, not in the manifest. This changes the wire format to
version 2; version 1 documents and stores written by 0.1.0 are refused, and a table moves to 0.2.0
with a new full delivery into a new store.

- A contract with a tombstone shape names a boolean `deleted_column`. A delivered row whose deleted
  column is true is a tombstone: its key is deleted, its other values are ignored, and the column is
  never published nor part of the column hash.
- `Manifest.tombstones` is now the count of tombstone rows, and `Manifest.keys` is gone: a manifest
  no longer grows with the number of changed or deleted keys, and neither does its receipt.
- Key coverage is gone, since without the key list it proved the same as whole-table coverage:
  ten shapes remain, and `Shape.feed()` covers the whole table.
- The `named_keys` check is gone. `row_count` compares live rows and tombstones, `key_integrity`
  refuses a key that is both live and deleted, and `rows_in_window` also covers the published rows
  a window's tombstones delete. `Action` carries no keys.
- `dogana manifest` and the file drop count tombstones from the deleted column; `--tombstones N`
  declares the count for files dogana cannot read, and a filename may carry a `tombstones` group.
- The documentation explains the model from the beginning, the window column rule (a value that
  never changes for a row, never an update time) and soft deletes.

## 0.1.0 (2026-10-26)

First public release, in beta: the manifest and ledger format may change before 1.0.

- Contracts, manifests, a gate and a ledger that keep `complete_as_of` and `deletions_as_of` for every
  region of a table, with receipts that rebuild the ledger.
- Sixteen delivery shapes: full exports, complete windows, changes since a timestamp, an opaque
  generation or a commit-ordered position, change feeds with tombstones, and keyless timestamp appends.
- Deliveries checked against their manifest: row count, keys, windows, cursor intervals, column hash,
  shrink tolerance and the SHA-256 of the delivered bytes.
- A DuckDB reference store, a file drop reading sidecar or filename-derived manifests, and a
  conformance suite for other stores.
- A planner that names the next read, and the `dogana` command line to write and check manifests.
- JSON Schema for every wire value, with identity test vectors.
