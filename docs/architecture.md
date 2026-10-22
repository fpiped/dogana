# Architecture

The [protocol](protocol.md) defines semantics. This page explains the boundaries that keep them
independent of extraction and storage.

## Evidence as values

A contract declares what a source promises. A manifest declares what one read contains. The ledger
records two source-evidence dates per region: completeness and deletion knowledge. Plans describe
row operations, checks report candidate predicates, and receipts retain decisions with their base
publication. These values can be validated and serialized without opening a database.

The core uses only the Python standard library. Contract validation, interval arithmetic, planning
and gate decisions are pure operations over frozen values. `dogana.deliver` owns the orchestration:
it calls a supplied store's primitives, but knows no SQL engine, extractor or scheduler. All concrete
I/O lives in reference packages or the caller's implementation. Dependency direction is inward;
removing reference packages leaves validation, accounting and claims usable.

`cursor_column` exposes a timestamp predicate to storage without putting row access in the core.
The gate requires every named result; the store must implement the predicate correctly. This exact
check set is not a plug-in API for arbitrary business-quality checks.

## One publication

A publication binds candidate rows, checks against a base, resulting rows, ledger, stamp and accepted
receipt. It becomes visible in one transaction or one table-format commit. The latter carries the
full receipt, or references a receipt already durable before the commit. A hash pointing at a record
that might never be written cannot recover a publication.

The commit compares its current head with `receipt.base_publication_id`. If another writer moved
that head, the prepared plan and check results are stale. Retry must start from the new base;
it cannot silently rebase an old acceptance. `execute` may prepare changes in a transaction or defer
them to `publish`, which has the receipt needed for a table-format commit.

Ordered row, receipt and ledger writes are not a third atomicity mechanism. A crash between them can
make replay report success while the ledger still describes older rows. An accounting-only caller
must supply atomic publication itself before claiming that rows and evidence agree.

The six store obligations are checks before rows, atomic publication, comparison against the checked
base, ordered durable receipts, idempotent replay and publication-consistent reads. These apply per
manifest and per table. A coherent multi-table snapshot needs external transaction or coordination
support; combining minimum dates does not create one.

## What the stamp detects

A publication identity hashes the preceding head, full contract identity and manifest identity.
The store writes that identity beside the rows as their stamp. Comparing ledger and stamp detects a
ledger lagging a committed publication, or external publication that moved the stamp. It is an
association check, not a content checksum: edits that retain the stamp, lost rejected receipts and
joint loss of all evidence remain outside its detection model.

Accepted receipts in publication order rebuild the ledger and chain. Repair is allowed only when
the reconstructed head matches the row stamp. Reconstructing evidence does not reconstruct rows;
historical reads require retained row versions. A missing ledger with surviving publication evidence
is a recovery problem, not permission to start a new history.

## Replay and bounded ledger state

The ledger contains contract, regions, generation and publication head. Accepted identities live in
the store's receipt index. Core delivery looks up an accepted identity before preflight and returns
the original receipt marked `replayed=True`, with unchanged receipt identity and receiver time.
This keeps each embedded ledger proportional to region count instead of accumulated deliveries.
Large key enumerations can still make individual manifests and receipts large.

Manifest identity describes the declaration, including optional `content_hash`. Without that digest,
a different payload under an identical declaration is indistinguishable from replay. A corrected
digest makes a new delivery, which must still satisfy source-time monotonicity. Sidecar bytes are
not the identity input: the frozen identity form normalizes values and omits defaults and version.
The replay flag is always excluded from receipt identity.

## An ordered fold

`Ledger.apply(manifest)` advances accounting under continuity and time constraints. It is deterministic
for the same ordered input history, but does not itself detect replay. Delivery idempotency belongs
to the receipt lookup and atomic publication sequence.

Regions split at coverage boundaries and merge when adjacent evidence agrees. Unknown gaps remain
visible. Disjoint admissible windows commute on regional evidence; their publication hashes retain
order. This fold is not a join-semilattice or a conflict-free merge of concurrent ledgers. An older
whole-table export after a newer window is rejected entirely to keep row replacement and evidence
consistent, even though part of its evidence could have been useful.

Completeness and validity are conditional source guarantees, not properties inferred from row count.
Keyless append raises both dates only because its contract declares immutable, never-deleted events.
Timestamp lookback mitigates late commits within a bound; it does not verify that no change was missed.

## Implementations and limits

DuckDB and file drop make the protocol executable. The DuckDB store stages candidates, commits rows
and evidence together and retains changed row versions for publication reads. It is a reference
implementation with table scans and retained history, not a production connector catalog.
The conformance suite exercises the same obligations against transaction and table-format models.
Its scenarios help catch mistakes; passing them is not proof of every possible implementation.

Existing data-contract schemas can supply the table, key and types. The delivery rules add semantics
that ordinary schema and quality declarations do not express. Until a carrier is implemented, callers
must project those fields into `Contract`; claiming there is no schema duplication would be false.
Governance ownership, signatures and producer credentials remain outside this protocol.

Python fits the surrounding extraction and warehouse libraries. Validation, hashing and interval
arithmetic stay small; row operations execute in the store. The project has no scheduler, watcher,
daemon, command-line interface, quality language or service. One window column gives one dimension
of evidence; plant-by-date regions need a different representation.
