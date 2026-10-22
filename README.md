# dogana

Customs for data integrations. Every delivery presents a declaration, is checked against it, waits
until it proves what it claims, and leaves a stamped receipt; the register says, per table and per
region of a table, where the closed world assumption holds: where a missing row means a deleted row,
as of when, and where it does not.

Most integrations receive data from systems they do not control: ERPs without change data capture,
vendors dropping files on SFTP, legacy databases with no soft deletes. The files arrive as full
extracts, windows of the last N days, or rows changed since a timestamp, and the receiver applies them
without recording what each one proved. The result is the familiar drift: deletions lost until the
next full load, windows that silently overlap, and consumers who cannot tell a complete table from a
patched one.

dogana makes that knowledge explicit:

- A **delivery contract** per table declares the identity of rows and the shapes deliveries may take:
  coverage, completeness within coverage, deletion signal, checkpoint.
- A **manifest** per delivery names its shape and states what it claims, written by the sender or derived by the
  receiver from the file pattern or its own extraction.
- A **ledger** keeps, per region of the key space, two dates: complete as of (no missing rows) and
  valid as of (no rows that should be gone). A delivery can never raise them above what it proves.
- A **receipt** records what arrived, what was verified and what the ledger became, and the claims a
  reader may rely on.

It is a protocol and a reference library. It is not a pipeline, not a scheduler and not a quality
monitor: extraction stays with your extractor, scheduling with your orchestrator, row-level checks
with your quality tool. The core depends on nothing but the Python standard library.

## Status

Design stage. The protocol is in [docs/protocol.md](docs/protocol.md). The core in `src/dogana`
implements contract validation and the ledger transitions, with tests. No store or extractor adapter
exists yet.

## Development

```
uv venv && uv pip install -e '.[dev]' && .venv/bin/pytest
```

## Licence

Apache-2.0.
