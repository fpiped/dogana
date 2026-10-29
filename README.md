<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/fpiped/dogana/main/docs/assets/logo-dark.png">
<img src="https://raw.githubusercontent.com/fpiped/dogana/main/docs/assets/logo-light.png" alt="dogana" width="400">
</picture>
</p>

<p align="center">
<a href="https://fpiped.github.io/dogana/"><img src="https://img.shields.io/badge/docs-fpiped.github.io%2Fdogana-064778" alt="Documentation"></a>
<a href="https://github.com/fpiped/dogana/actions/workflows/ci.yml"><img src="https://github.com/fpiped/dogana/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
<a href="https://github.com/fpiped/dogana/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/python-3.11%2B-3776ab" alt="Python 3.11+"></a>
<a href="https://github.com/fpiped/dogana/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-064778" alt="License Apache-2.0"></a>
</p>

<!-- --8<-- [start:body] -->
# Make messy data integration trustworthy

dogana is a Python library for tables you load from another system. It tells you how far each
table matches that system, and as of when.

Loading a table from another system is two jobs. One **reads** the source: it runs an export, a
query or an API call, and knows what it got, at what instant, in what shape, how many rows. The
other **writes** the table, and sees only rows. Often they are different people, systems or
vendors; sometimes they are one script. Either way, what the reader knew is gone once the rows
land, unless it is written down.

dogana makes the reader write it down, in a **manifest**, and makes the writer check it and
remember it, in a **ledger**. The ledger keeps two dates per table, or per range of a column that
never changes on a row, such as the order date:

- **complete as of**: every row the source had at that instant is here;
- **deletions as of**: no row the source deleted before that instant is still here.

A full export on Sunday proves both dates. The rows changed since yesterday prove only the first,
because a deleted row is not among them. dogana refuses any delivery that would move a date further
than its declaration proves. It never sees the source: if the export misses rows the source has,
and the declaration counts only what was exported, no check can tell.

## How it works

1. You write a **contract** for each table: its key and the kinds of read you accept, called
   shapes.
2. Every read arrives with a **manifest**, the sender's declaration: the shape, the instant the
   source was read, the number of rows and of rows reporting a deletion, and the SHA-256 of the
   file's bytes.
3. A **gate** checks the manifest against the contract and against what is already proven. Your
   loader checks the rows against the manifest and reports the result. If any check fails, the whole
   delivery is refused.
4. A **ledger** holds the two dates and moves them only as far as each accepted read proves.
   Before trusting the rows, a reader asks the ledger.
5. Related tables, such as orders and their lines, can be read in one snapshot, one read that sees
   every table at the same instant, and delivered **together**. When both then have both dates
   equal to that instant, which dogana calls exact, a join between them gives the answer the source
   would have given.

*Dogana* (doh-GAH-nah) is Italian for customs: goods cross a border with a declaration, an officer
checks them against it, and they enter with a stamp or are turned back. Here the goods are rows,
the declaration is the manifest, the officer is the gate, and the stamp is the receipt, the record
dogana keeps of each decision.

## In thirty seconds

A full dump on Sunday, then only the rows changed since. This example skips the rows and their
checks: it hands two manifests straight to a ledger, to show how the dates move. The quick start
runs the same steps on real files.

```python
from datetime import UTC, datetime, timedelta

from dogana import Contract, Ledger, Shape

shapes = {"full": Shape.full(), "changes": Shape.changes()}
orders = Contract("orders", key=("id",), types={"id": "int"}, shapes=shapes)

sunday = datetime(2026, 10, 25, tzinfo=UTC)
monday = datetime(2026, 10, 26, tzinfo=UTC)
ledger = (
    Ledger(orders)
    .apply(orders.manifest("full", sunday))
    .apply(orders.manifest("changes", monday, since=sunday))
)

claims = ledger.claims(monday, limit=timedelta.max)  # expire nothing
print(
    f"{claims.state}: complete as of {claims.complete_as_of:%A}, deletions as of {claims.deletions_as_of:%A}"
)
```

```text
patched: complete as of Monday, deletions as of Sunday
```

Monday's read brought every new and updated row, but said nothing about deletions: a row deleted
at the source since Sunday may still be here. That is what patched means.

## Use it

`pip install dogana`. It is pure Python with no dependencies, and it never reads a row or a file:
it decides from the declarations and the ledger. It runs inside whatever loads your tables, such as
a cron job, an Airflow task or a dlt pipeline. Your loader does the three things dogana cannot: it
checks the rows against the manifest, it executes the plan the receipt returns on your table, and it
saves rows, ledger and receipt in one transaction. The [quick start](https://fpiped.github.io/dogana/quickstart/)
is such a loader on SQLite, in about sixty lines. A sender writes its manifest as JSON in any
language, or with `dogana manifest`.

dogana is in beta: the manifest and ledger formats may change before 1.0. If every source sends
you change data capture with deletes, you already know which rows to trust. dogana is for
everything else.

Coding agents find dogana's rules in the installed package, at
`dogana/.agents/skills/dogana/SKILL.md`, and the documentation as Markdown at
[llms.txt](https://fpiped.github.io/dogana/llms.txt).
<!-- --8<-- [end:body] -->

## Learn more

The documentation is at [fpiped.github.io/dogana](https://fpiped.github.io/dogana/).

- [Quick start](https://fpiped.github.io/dogana/quickstart/): a loader on SQLite, a rejected file, the same file sent twice, a deletion, two tables together.
- [Concepts](https://fpiped.github.io/dogana/concepts/): dogana from the beginning, every idea with an example.
- [Protocol](https://fpiped.github.io/dogana/protocol/): the specification.
- [Integrations](https://fpiped.github.io/dogana/integrations/): how dlt, dbt, Iceberg and orchestrators call dogana.

Licensed under [Apache-2.0](https://github.com/fpiped/dogana/blob/main/LICENSE).
