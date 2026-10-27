# Architecture

The [protocol](protocol.md) is the authority on behaviour; this page shows how the code is laid out
and why.

## Layers

```text
core          contract ledger gate group planner wire errors     standard library only, no I/O
sequence      delivery.deliver, deliver_together, Store        the one place that calls storage
reference     reference.duckdb, reference.files                adapters built on the core
entry points  cli, conformance                                 the core imports neither
```

Dependencies point inward: reference code and entry points import the core, never the reverse, and
a test enforces it, together with a test that the command line imports no third-party package.

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
  `Ledger.claims(now)` reads it at the reader's time.
- **`gate.py`**: `Gate.requirements` names the row checks a store must answer for a manifest;
  `Gate.preflight` checks the manifest and its transition without rows; `Gate.evaluate` turns the
  checks into a `Receipt` with an `ApplyPlan`, the row operations to execute. The core describes
  row operations and never reads rows.
- **`group.py`**: `Group` and `Reference`, tables read together and the rules the source keeps
  between them. `Group.claims` combines their claims, and `Claims.exact_as_of` is the instant at
  which all of them equal the source, if there is one.
- **`planner.py`**: because `Ledger.apply` has no side effects, the planner simulates each admitted
  read on the current ledger and keeps the cheapest one that establishes the required dates.
- **`wire.py`**: every value as versioned JSON, and its identity, the SHA-256 of its RFC 8785
  canonical form. Identities make replay, receipts and the publication chain content-addressed.

## One delivery

`deliver(store, contract, source, manifest, now)` is the only sequence, so a store cannot get the
order wrong:

1. load the ledger and look for an accepted receipt with the same manifest identity; if there is
   one, the delivery is a replay (after the store has verified a declared hash against the bytes);
2. preflight, and record a refusal that changes nothing if it fails;
3. `hold` the candidate rows and `check` them;
4. `Gate.evaluate`, then `publish` rows, ledger and receipt in one atomic step, compared against
   the publication the checks were made on; `abort` releases staged work after any failure.

`deliver_together` runs the first three steps for each table of a group, adds the joint checks
(one read boundary, every table accepted on its own, references), and publishes every table in one
`publish_together` step or refuses them all.

`Store` is a `typing.Protocol` of seven methods (`hold`, `check`, `publish`, `abort`,
`load_ledger`, `accepted_receipt`, `receipts`). Any class with those methods is a store; the
conformance suite proves that it behaves.

## Receipts are the history

Accepted receipts, in order, are the history of a table; the ledger is computed from them, and
`Ledger.from_receipts` rebuilds it. Each publication extends a hash chain, SHA-256 of the previous
head, the contract and the manifest, and the store stamps the published rows with its head. A
ledger that disagrees with that stamp is diagnosed as `LedgerAhead` (evidence ahead of the rows:
deliver a fresh full) or `StampAhead` (rows without matching evidence: repair from receipts). The
stamp cannot detect a direct edit of the rows that leaves it unchanged.

## Patterns

- **Functional core, imperative shell**: decisions are pure functions of immutable values; only
  `deliver` performs I/O, through the store.
- **Ports and adapters**: `Store` is the port; DuckDB is one adapter, a warehouse store is another.
- **Parse, don't validate**: a manifest becomes a typed delivery once, and exhaustive matching
  (`assert_never`) proves every kind is handled.
- **Write, audit, publish**: candidate rows are staged and checked before visible rows change.
- **Event sourcing**: receipts are the events, the ledger their projection.
