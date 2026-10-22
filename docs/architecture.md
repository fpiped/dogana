# Architecture

How dogana is built, which patterns it follows, and what it borrows from projects that answered
the same questions well. The [protocol](protocol.md) defines the semantics; this page explains
the shape of the code and the reasons.

## One verb

A data system has three verbs. Load moves bytes from a source into landing tables. Transform
derives tables from tables. Publish decides what readers see and what they may conclude from it.
dogana owns the third verb and refuses the other two.

The test for the boundary is the recomputation rule: everything dogana writes must be derivable
from two inputs, the sequence of accepted manifests and the contract. The ledger passes (it is a
fold over manifests). Receipts pass (each is a function of ledger, manifest and check outcomes).
Rows fail (they are the source's), so dogana never owns a row. Schedules fail, so it never owns a
clock. This rule decides every "should this live in the core" question without a meeting.

## Ports and adapters

The core is `contract`, `ledger`, `gate`, `planner`, `wire`: pure functions over frozen values,
standard library only. Everything that touches the world is an adapter behind an optional extra:

| Port | Protocol | Reference adapter | Others that fit |
|---|---|---|---|
| Store | `Store`: hold a candidate, run row checks, execute a plan, publish atomically, read a publication, persist ledger and receipts | DuckDB file | Postgres, DuckLake, Iceberg, Delta |
| Extractor | `Extractor`: take an `ExtractionRequest`, return a manifest and a row source | none yet | dlt, Airbyte, a SQL script |
| Entry point | push a file with a manifest, or derive one from the name | `FileDrop` | an SFTP watcher, an S3 event, a queue |

Dependencies point inward. Adapters import the core; the core imports nothing. A test deletes
every adapter package and proves the core still validates, applies and answers. That test is the
architecture's unit test.

## Values, not calls

The core never asks a store to do something. It emits a value that says what must be done, and
the adapter executes it:

- `ApplyPlan`: an ordered tuple of `Action` values (`replace_all`, `replace_window`, `upsert`,
  `tombstone`, `append`). Any store with transactions can execute it; none needs to know why.
- `CheckRequirement` and `Check`: the gate names the row checks a delivery must pass; the adapter
  returns one outcome per name; the gate decides. A missing, duplicate or unexpected outcome is a
  rejection, so an adapter cannot skip a check by accident.
- `Receipt`: the decision as data, with the transition before and after. A store persists it; a
  reader reconstructs the ledger from the accepted ones.

Values can be serialised, compared, replayed and tested without a database. That is why the
protocol defines JSON forms for all of them and why every value carries `version` and `kind`.

## Write, audit, publish

Every delivery follows the pattern lakehouse teams call write-audit-publish: rows land in a
candidate, checks run against the candidate, and only then does one atomic step make the candidate
visible. dogana adds what the pattern leaves to each team: the audit is scoped by what the
delivery claims (a window delivery is checked inside its window), the publish writes a receipt, and
the ledger records what the publication proved. In the DuckDB adapter the candidate is a temporary
table and the publish is a transaction that commits rows, ledger and receipt together; a failure
anywhere rolls all three back.

## Idempotency by identity

A manifest's identity is the hash of its canonical JSON. An accepted identity applied again returns
the original receipt and changes nothing, however many deliveries came in between. A corrected file
needs a corrected manifest, which has a different identity. This is the whole retry story: senders
can resend, orchestrators can rerun, nothing doubles.

## The ledger is a fold

`Ledger.apply(manifest)` is a pure function from a ledger to a ledger. Regions are split exactly at
the delivery's bounds, dates move only where the delivery proves them, and adjacent regions with
identical evidence merge back. Because the function is pure, the test suite can fuzz it: random
sequences of full, windowed and changed-since deliveries, with the invariants checked after each
step (regions partition the domain, validity never exceeds completeness, untouched regions never
move, touched regions carry exactly the delivery's date).

## What is borrowed, and from whom

dlt. The drop-in library model: no daemon, no service, a function call that works in a notebook,
a Lambda or an Airflow task. dlt also stores its pipeline state in the destination next to the data
and restores it on a clean start; dogana does the same with the ledger and receipts, so a store is
self-describing and a lost working directory loses nothing.

dbt. Declarative configuration as the unit of intent: a model declares its incremental strategy and
dbt derives the SQL. dogana's contract declares shapes and the core derives plans and checks. dbt
also made "full refresh" the universal escape hatch for drift; dogana keeps the escape hatch and
makes the drift visible as a date instead of a feeling.

mosaico. Sessions as immutable layers that are finalised once and never edited, and a catalog that
can be rebuilt from durable storage if the metadata database is lost. dogana's publications are
immutable, and the ledger is rebuildable from receipts. mosaico also shows the cost of a daemon:
the right call for a robotics ingest path at gigabytes per second, the wrong one for a library
whose hot loop is a hash and a few interval splits.

Iceberg and Delta. Snapshot isolation and a per-commit metadata slot. When a store adapter targets
them, the receipt identity goes into the snapshot summary, so the table format's own history points
back at the audit record.

## Why Python and why no dependencies

The core's work is validation, interval arithmetic and hashing. Its hot path is not in Python; it
is in the store that executes the plan. The people who will adopt a delivery ledger write Python,
and every neighbour (dlt, pyiceberg, Dagster, Soda) is Python. A language with a faster loop would
buy nothing and cost the integration surface.

No runtime dependencies in the core means the library can be vendored, audited in an afternoon and
installed anywhere a Python interpreter runs. Adapters pay for their dependencies behind extras.

## Why DuckDB first

A single file, transactions, CSV and Parquet readers, and a type system close to the protocol's
logical types. The adapter proves the interfaces are sufficient in under 400 lines. It copies the
whole table on every publication to support reads at a version, which is honest for a reference
and wrong for a product: that is the first thing a DuckLake or Iceberg adapter would replace with a
snapshot.

## Non-goals, stated so they stay stated

- No scheduler, watcher or daemon. The library is called; it never wakes up.
- No second rule language. Row rules belong to Soda, Great Expectations or SQL.
- No parallel contract schema. The delivery contract complements a data contract (schema and
  semantics); it does not restate it.
- No platform. If a feature needs a server to exist, it belongs in an adapter or in another project.
