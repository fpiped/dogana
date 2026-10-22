# Architecture

How dogana is built and why. The [protocol](protocol.md) defines the semantics; this page explains
the shape of the code and the decisions behind it.

## One verb

A data system has three verbs. Load moves bytes from a source into landing tables. Transform
derives tables from tables. Publish decides what readers see and what they may conclude from it.
dogana owns the third verb and refuses the other two.

The test for the boundary is the recomputation rule: everything dogana writes must be derivable
from two inputs, the sequence of accepted manifests and the contract. The ledger passes (it is a
fold over manifests). Receipts pass (each is a function of ledger, manifest and check outcomes).
Rows fail (they are the source's), so dogana never owns a row. Schedules fail, so it never owns a
clock. This rule decides every "should this live in the core" question.

## Rules, not implementations

The core is `contract`, `ledger`, `gate`, `planner`, `wire`: pure functions over frozen values,
standard library only. It emits values that say what must be done (a plan, a list of checks, a
receipt) and never does it. Whoever writes rows meets the four store obligations of the protocol:
checks before rows, rows and ledger together, receipts kept, replay returns the stored receipt.

Those obligations are stated as rules because they can be checked mechanically. `dogana.conformance`
is the check: the same scenarios (a week of files, a contradictory file, a crash after the rows
changed, a replay, an out-of-order window) run against any object that implements `Store`. A store
that passes has met the contract, whoever wrote it. That is why the repository ships no catalogue
of warehouse adapters: the product is the rule and its test, and a warehouse team writes its store
the way it writes everything else, then runs the suite.

Two reference implementations live in `dogana.reference`, installed under extras: a DuckDB store and
a file drop that derives a manifest from a filename. They exist so the protocol has a running
example, the README runs, and the conformance suite has a subject. They are fixtures that happen
to be usable, not warehouse support.

Dependencies point inward. The reference imports the core; the core imports nothing. A test deletes
the reference package and proves the core still validates, applies and answers.

## Atomicity: three mechanisms and one guard

The one invariant a store cannot delegate to the core is that rows and the fact about the rows move
together. The protocol admits three ways to keep it: one transaction when rows and ledger share an
engine; the table format's commit when rows live in Iceberg, Delta or DuckLake, with the receipt
identity in the snapshot metadata; ordering plus idempotency when neither exists, which is enough
for every plan except append.

The guard that makes a violation visible is the publication stamp. The ledger's `publication_id`
is a hash chain; the store writes it next to the rows in the same step as the rows; `load_ledger`
compares the two and refuses deliveries while they differ. A crash between rows and ledger, or rows
replaced behind the gate, becomes a refused delivery with a reason instead of a wrong answer.

## Values, not calls

- `ApplyPlan`: an ordered tuple of `Action` values (`replace_all`, `replace_window`, `upsert`,
  `tombstone`, `append`). Any store with transactions can execute it; none needs to know why.
- `CheckRequirement` and `Check`: the gate names the row checks; the store returns one outcome per
  name; the gate decides. A missing, duplicate or unexpected outcome is a rejection, so a store
  cannot skip a check by accident.
- `Receipt`: the decision as data, with the transition before and after. The accepted receipts in
  order reconstruct the ledger.

Values can be serialised, compared, replayed and tested without a database. Every value carries
`version` and `kind` and has a JSON form in the protocol.

## Write, audit, publish

Every delivery follows the pattern lakehouse teams call write-audit-publish: rows land in a
candidate, checks run against the candidate, and one atomic step makes the candidate visible.
dogana adds what the pattern leaves to each team: the audit is scoped by what the delivery claims
(a window delivery is checked inside its window), the publish writes a receipt, and the ledger
records what the publication proved.

## Idempotency by identity

A manifest's identity is the hash of its canonical JSON. An accepted identity applied again returns
the original receipt and changes nothing, however many deliveries came in between. A corrected file
needs a corrected manifest, which has a different identity. Senders can resend, orchestrators can
rerun, nothing doubles.

## The ledger is a fold

`Ledger.apply(manifest)` is a pure function from a ledger to a ledger. Regions split exactly at the
delivery's bounds, dates move only where the delivery proves them, and adjacent regions with
identical evidence merge back. Because the function is pure, the suite fuzzes it: random sequences
of full, windowed and changed-since deliveries, with the invariants checked after each step
(regions partition the domain, validity never exceeds completeness, untouched regions never move,
touched regions carry exactly the delivery's date).

## Who should not use it

A team whose every source arrives through log-based change data capture with soft deletes already
holds a faithful copy; both dates would always be equal and dogana would record nothing new. The
library is for the other feeds: files on SFTP, APIs that answer "changed since", exports that are
full on Sunday and windowed on weekdays, append-only logs.

## What is borrowed, and from whom

dlt: the drop-in library model, no daemon, a function call that works in a notebook or a task, and
state stored next to the data so a lost working directory loses nothing. dbt: declarative intent
(a shape) from which the library derives the mechanics (plan and checks), and full refresh as the
universal escape hatch, here made visible as a date. mosaico: immutable layers finalised once and a
catalog rebuildable from durable storage. Iceberg and Delta: snapshot isolation and a per-commit
metadata slot, where the receipt identity goes. Iceberg also shows the distribution model:
a specification plus one reference library, implementations elsewhere.

## Why Python and why no dependencies

The core's work is validation, interval arithmetic and hashing; the hot path is in the store that
executes the plan. The people who adopt a delivery ledger write Python, and every neighbour is
Python. No runtime dependencies means the core can be vendored and audited in an afternoon.

## Non-goals, stated so they stay stated

- No scheduler, watcher or daemon. The library is called; it never wakes up.
- No second rule language. Row rules belong to Soda, Great Expectations or SQL.
- No parallel contract schema. The delivery contract complements a data contract; it does not
  restate it.
- Reference implementations only. A store is written by the team that owns the warehouse and proved by the
  conformance suite.
- No platform. If a feature needs a server to exist, it belongs in another project.
