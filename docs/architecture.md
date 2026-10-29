# Architecture

The [protocol](protocol.md) is the authority on behavior; this page shows how the code is laid out
and why.

## Layers

<!-- illustration: package layer diagram, not command output -->
```text
core          contract ledger gate group planner wire errors     standard library only, no I/O
entry points  cli                                              hashes a file, parses none
your loader   outside the package                              rows, checks, the commit
```

The core never reads a row or a file: it decides from declarations and the ledger. Tests enforce
that no module of the package imports a reader of data files or a database driver, and that a copy
of the core's modules alone still validates a manifest, applies it and answers claims.

## The core

- **`contract.py`**: `Contract`, `Shape` and `Manifest`. Every value checks itself when it is
  built, so an invalid contract or manifest cannot exist. `Manifest.check(contract)` turns a
  manifest into one of six delivery kinds (full, window, timestamp changes, generation changes,
  sequence changes, append), each carrying exactly the fields it needs; the rest of the code
  matches on the kind instead of checking fields again.
- **`ledger.py`**: the ledger stores the points where the evidence changes along the window
  column, and the evidence from each point to the next: unknown, or the two dates. `Region` is the
  view of the pieces between points. `Ledger.apply(manifest)` runs the guards (no gap, no stale
  overwrite, a proven base for changes) and returns a new ledger; values are never modified.
  `Ledger.claims(now, limit=...)` reads it at the reader's time, with the reader's age limit.
- **`gate.py`**: `Gate.preflight` checks a manifest and its transition; `Gate.evaluate` adds the
  checks the loader reports on the rows and turns the decision into a `Receipt` with an
  `ApplyPlan`, the row operations the loader executes. The core describes row operations and never
  reads rows.
- **`group.py`**: `Group`, tables read together. `Group.apply` decides their deliveries as one, and
  `Claims.exact_as_of` is the instant at which all of them equal the source, if there is one.
- **`planner.py`**: because `Ledger.apply` has no side effects, the planner simulates each admitted
  read on the current ledger and keeps the cheapest one that establishes the required dates.
- **`wire.py`**: every value as versioned JSON, and its identity, the SHA-256 of its RFC 8785
  canonical form. Identities make replay, receipts and the publication chain content-addressed.

## One delivery

The loader owns the sequence, and the [protocol](protocol.md#the-receiver) states it:

1. look for an accepted receipt with the same manifest identity; if there is one, the delivery is a
   replay, once a declared hash matches the bytes;
2. `Gate.preflight`, and record a refusal that changes nothing if it fails;
3. check the staged rows against the manifest;
4. `Gate.evaluate`, then execute the plan and save rows, ledger and receipt in one transaction,
   against the ledger the decision was made on.

`Group.apply` runs the decision for each table of a group, adds the joint check (one read boundary,
every table accepted on its own), and accepts every table or refuses every table.

## Receipts are the history

Accepted receipts, in order, are the history of a table; the ledger is computed from them, and
`Ledger.from_receipts` rebuilds it. Each acceptance extends a hash chain, SHA-256 of the previous
head, the contract and the manifest; `Ledger.follow` refuses a receipt that does not continue the
chain it names.

## Patterns

- **Functional core, imperative shell**: decisions are pure functions of immutable values; the
  loader, outside the package, performs the I/O.
- **Parse, don't validate**: a manifest becomes a typed delivery once, and exhaustive matching
  (`assert_never`) proves every kind is handled.
- **Event sourcing**: receipts are the events, the ledger their projection.
