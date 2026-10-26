# Changelog

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
