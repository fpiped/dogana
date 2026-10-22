# Architecture

The [protocol](protocol.md) defines what each delivery proves and is the authority for implementation.

The core uses standard-library frozen values and pure validation, accounting, planning and gate decisions, while `deliver` calls a supplied store for I/O.

`Manifest.check` parses the wire declaration into a full, window, timestamp-change, generation-change or append delivery with its required fields.

The ledger stores sorted cuts with Unknown or Known evidence to their right and derives `Region` values for claims and wire documents.

The gate requires a fixed set of row predicates, and quality tools retain additional business-rule outcomes outside that set.

The required store port holds and checks candidates, publishes or aborts them, loads the ledger, lists receipts and looks up an accepted receipt for replay.

Publication checks the expected parent and commits accepted rows with evidence atomically, while rejection records a round-trippable receipt against the same unchanged parent.

Receipts are the event log and the ledger is its projection: accepted history reconstructs evidence, while rows need storage retention and replay lookup prevents another publication.

The optional stamped tier uses core diagnosis to distinguish `LedgerAhead` from `StampAhead` and repairs evidence only when reconstructed history matches the row stamp, which cannot detect unstamped row edits.

Reference code depends on the core, never the reverse; DuckDB supplies executable storage and file ingestion, while the pytest-based conformance suite tests required publication behavior and optional stamped recovery separately.
