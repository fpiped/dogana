# Changelog

## Unreleased

The protocol checks declarations only. Every row check compares the rows with what the manifest or
the contract declares, so an honest delivery never fails one.

- The shrink limit is gone: `max_shrink_fraction` leaves the contract and `shrink_tolerance` the row
  checks. It refused honest files, such as a full after the source archived half its rows, and
  passed dishonest ones, such as two files truncated by 40% each. A truncated file is caught by a
  count the export computes in the same read; a rule on how much a table may shrink is a business
  check.
- The wire format is version 4, since a version 3 contract carries the shrink limit: stores and
  documents written by 0.6.0 or earlier are refused, and a table moves to the new version with a
  new full delivery into a new store.

## 0.6.0 (2026-10-28)

The documentation is rewritten for a reader who knows SQL but not dogana: Concepts follows one
orders table from the first file to tables read together, defines every term where it first
appears, and links the research it builds on.

- The package ships a skill for coding agents, `dogana/.agents/skills/dogana/SKILL.md`: the rules no
  check can enforce (the instant to declare, a window column that never changes, honest shapes,
  counts from the export, one snapshot for tables read together), a contract, the shapes and the
  calls. Its code runs in the tests.
- The documentation site serves `llms.txt`, `llms-full.txt` and every page as Markdown.
- The protocol states the soft-delete rule correctly again: a flag is a deleted column only if
  setting it moves the cursor column, since otherwise a changes read never delivers the deletion. It
  also states that a tombstone's cursor may be null, and that an empty changes file keeps a group's
  instant only for a tombstone shape.

## 0.5.0 (2026-10-27)

The package never parses a data file. The DuckDB store and the file drop leave it for the
repository's `examples/duckdb`, where they are tested with every change but not published, and the
`duckdb` extra goes: `pip install dogana` is the whole package. A receiver checks rows with its own
loader, or publishes through a store it writes against the conformance suite or copies from the
example.

- `dogana.reference.duckdb` and `dogana.reference.files` are gone. From a checkout,
  `uv pip install -e . -e examples/duckdb` installs the example, imported as `example_store`
  (`DuckDBStore`, `FileDrop`, `FilePattern`, `Rows`, `ingest_together`).
- `dogana manifest` no longer counts rows: `--rows` is required, and `--tombstones` for a tombstone
  shape, since the export that wrote the file knows them and the receiver's store counts them again.
  It still hashes the file. `dogana check` compares the manifest with the contract, its schema hash
  included, and the file's SHA-256 with the manifest, and refuses a manifest without a hash.
- `dogana claims LEDGER` prints what a ledger, as JSON, claims at an instant: each region's bounds,
  state and two dates, then the instant the table is exact at, if any.
- The development tools are a `dev` dependency group, not an extra, so the published package
  declares no optional dependencies.
- A tombstone's cursor value, when it carries one, must lie in the read interval like a live row's:
  `rows_in_interval` refused a live row stamped outside `(since, produced_at]` but let a deletion
  stamped there through. A null cursor on a tombstone stays allowed.
- The check `window_stability` is renamed `window_present`, which is what it tests: every live row
  has a window value, so it falls in a region. Receipts written by 0.4.0 keep the old name.

## 0.4.0 (2026-10-27)

The store publishes exactly the bytes the sender hashed and counted, every delivery it cannot read
is a recorded rejection, and a receipt no longer grows with the table's history. This changes the
wire format to version 3: stores and documents written by 0.3.0 are refused, and a table moves to
0.4.0 with a new full delivery into a new store.

- A receipt records one decision and names the ledger it leaves by identity (`ledger_identity`)
  instead of carrying it, its claims and the whole ledger before it. `Transition` keeps the plan and
  the regions over the delivery's coverage. With one window a day, after a year a receipt carried four
  copies of a 90 KB ledger, over 350 KB; it is now about 2.5 KB. The ledger is the fold of accepted
  receipts: `Ledger.follow(receipt)` takes one checked step, `Ledger.from_receipts` all of them, and
  `store.load_ledger(contract)` reads the current one.

- A delivered file is opened once and copied into the store's private folder while it is hashed;
  every read uses the copy. Below a drop root each folder is opened from the one above it without
  following a link, so a file or folder swapped for a link after the root check is refused, and a
  FIFO is refused instead of waited on. A file changed while it was read can no longer put other
  bytes behind a receipt. Windows, which cannot open that way, gets a weaker check.
- CSV has one grammar, RFC 4180: comma, double quote, UTF-8, header row. DuckDB no longer guesses a
  dialect, so a `'` or a leading `#` is data, and `dogana manifest` counts exactly what the store
  reads.
- A `datetime` value needs a date, a time and its UTC offset, in CSV as in Parquet; anything less is
  refused instead of read as UTC. A one-column CSV's blank line is an empty value, as DuckDB reads
  it, so the sender's count agrees; a record with the wrong number of fields is refused.
- The `duckdb` extra needs DuckDB 1.2 or later.
- A file the store cannot read (missing, not UTF-8, compressed, corrupt, a value out of range) is a
  recorded rejection, not a `StoreError` that a retrying receiver would repeat forever.
- A payload column can be declared `decimal(p,s)`, published as DECIMAL and read back as
  `Decimal`. A DECIMAL used to be read as a float, so 1234567890123456.78 was published as
  1234567890123456.8; it is now exact, or refused when the declared type differs. A `Decimal` row
  value with more digits than its column holds is refused rather than rounded.
- A window delivery refuses a key published outside its window. Moving it used to leave the region
  it came from claiming exactness without it; only a delivery covering the whole table moves a key.
- A replay compares the bytes with the accepted digest and nothing else, through the new `Store`
  method `digest(source)`, so a contract that changed since is no reason to refuse it. A writer
  that loses a race to publish the same manifest is answered for its own bytes: a replay, or a
  refusal when they differ, never the winner's acceptance.
- The file drop gives a manifest without `content_hash` the digest of the received file, so a changed
  file under the same sidecar is a new delivery, not a silent replay. Filename counts must be integers.
- `dogana check` names the comparisons it made, says that keys, windows and types are checked on
  delivery, and refuses a file it could compare in no way. `dogana manifest`
  explains a file it cannot read instead of printing a traceback, writes a sidecar other accounts can
  read, and never replaces one without `--force`, even in a race.
- The protocol states that `produced_at` of a sequence read is earlier than the first unread commit,
  that `start_at` excludes its own instant, what a soft-deleting source's dates speak about, and that
  the query theorem assumes both sides compare and compute alike.
- `Rows(rows, columns)`, from `dogana.reference.duckdb`, types a batch of row mappings, so an empty
  API poll or an all-null batch is still a delivery without writing a file.
- A field of the wrong type names the type expected, and `Ledger.apply` refuses anything but a
  `Manifest`, saying that rows reach a store through `dogana.deliver`.
- The docs describe dogana in one sentence everywhere, qualify "proves" by the sender's honesty, say
  that the DuckDB store and file drop are reference implementations, and cite the research the two
  dates come from. The source archive carries the tests and docs it needs to run.
- `StoreConformance` checks that a store publishes only what its own checks accepted, and, through an
  optional factory `reopen`, that a publication survives reopening and that two stores on one
  storage publish only one candidate.

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
