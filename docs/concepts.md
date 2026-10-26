# Concepts

| Concept | Meaning |
|---|---|
| Contract | Receiver declaration of table, key, window column, their types, admitted shapes, policy, the hash of the data contract's columns, and optionally the finite `domain` of window values. |
| Shape | Coverage, completeness, deletion signal and checkpoint; helpers include `full`, `window`, `changes`, `feed` and `append`. |
| Manifest | One source read's shape, boundary, coverage, count and optional SHA-256 of the delivered bytes. |
| Gate | Fixed checks that turn a manifest and row outcomes into a publication decision. |
| Ledger | Evidence for the table, rebuilt from accepted receipts. |
| Region | An interval on one declared window column, including explicitly unknown space. |
| The two dates | `complete_as_of`: no live source row missing; `deletions_as_of`: deletions accounted for through that boundary. |
| State | Unknown, complete, patched or expired, evaluated at the reader's `now`; default freshness is seven days. |
| Claims | Regional evidence and weakest input dates; neither a common snapshot nor a guarantee about a joined result. |
| Receipt | Accepted, rejected or replayed outcome, checks, receiver time and resulting evidence. |
| Store | Holds and checks candidates, then publishes rows and evidence atomically against the expected head. |

The data contract owns the columns. The delivery contract's `types` names exactly its key, window
and optional cursor columns, with no extras. Its optional `schema_hash` is an opaque string from
the data contract; the gate requires a matching manifest and candidate hash only when it is present.
The core never computes a full schema.

## Source patterns

| Source | Shape | Evidence |
|---|---|---|
| Nightly full export | `Shape.full()` | Both dates advance everywhere. |
| Daily window, weekly full | `Shape.window(size=...)` and `Shape.full()` | Both dates advance inside delivered coverage. |
| Changes since `updated_at` | `Shape.changes()` | Completeness advances; deletion knowledge waits. |
| One export per plant | Complete singleton windows, plants listed in `domain` | Each plant has dates; once every listed plant is delivered, the whole table has them. |
| Cursor feed with tombstones | `Shape.feed()` | Continuous changes and deleted keys preserve an exact base. |
| Immutable keyless history | `Shape.append()` with `start_at` | Contiguous intervals advance both dates. |
| A change feed consumed in offset batches | `Shape.feed(checkpoint="sequence")` after a snapshot | Each committed range of offsets continues the last, checked by order. |
| Rows read after a commit-ordered position (log position, offset) | `Shape.changes(checkpoint="sequence")` | Each read continues the position; an `int` cursor column checks every row. Auto-increment ids need a safe margin. |

Window membership must be immutable at the source. A generation is an opaque table-wide cursor,
a sequence an ordered integer one; a tombstone names a deleted key. The [shape decision table](protocol.md#choosing-a-shape)
details what each pattern cannot prove.

## What it is not

- SQLMesh tracks processed model intervals; dogana records incoming source evidence and deletion coverage.
- Iceberg snapshots identify committed table versions; dogana describes source evidence within a version.
- dbt snapshots retain row history; dogana decides when incoming absence justifies deletion.
- A watermark table records a cursor; dogana also records coverage, deletion knowledge and refused files.
- CDC transports changes; dogana accounts for the evidence a delivered batch establishes.
- Not a stream processor: it accounts for bounded deliveries, and a micro-batch committed from a stream is one of them. No watermarks, no event-time windows.

## Reading and extending

For "items with no movement", retained deleted movements hide true answers. Negation reverses
which input guarantee matters; exact aggregates and mixed-date joins need more than minimum dates.
The [protocol](protocol.md) defines the semantics and [wire schemas](wire/) describe JSON.

Store authors can run `dogana.conformance.StoreConformance`; stamped recovery is optional.
See [reference implementations](reference.md), [integrations](integrations.md) and
[architecture](architecture.md). Quality tools run business rules outside the fixed gate.

## Grounding and status

The dates adapt table completeness and deletion evidence to regions and source history: missing live rows
and retained deleted rows are different failures. Protocol version 1 is unreleased. Compatible
policy changes preserve history; key and window bindings cannot change. DuckDB and file drop are
executable reference implementations, not production warehouse support.
