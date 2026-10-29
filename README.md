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

dogana is a Python library for tables you load from files another system exports. It tells you,
for each table, how far you can trust it to match that system.

Most data still arrives as exported files: a full dump on Sunday, the orders of the last 30 days
every night, the rows changed since yesterday. Each kind of file proves something different, and
almost none says which rows were deleted. Once the rows are loaded, nobody remembers which file
proved what.

dogana keeps that record as two dates. It keeps them for the whole table, or separately for each
range of one column whose value never changes on a row, such as the order date:

- **complete as of**: every row the source had at that instant is here;
- **deletions as of**: no row the source deleted before that instant is still here.

A file moves a date only as far as it proves. dogana never sees the source: it checks each file
against what the sender, whoever exports it, declares about it. If the export misses rows the source
has, and the declaration counts only what was exported, no check can tell.

## How it works

1. You write a **contract** for each table: its key and the kinds of file you accept, called
   shapes.
2. Every file arrives with a **manifest**, the sender's declaration: the shape, the instant the
   source was read, the number of rows and of rows reporting a deletion, and the SHA-256 of the
   file's bytes.
3. A **gate** checks the manifest against the contract and against what is already proven. Your
   loader checks the rows against the manifest and reports the result. If any check fails, the whole
   file is refused.
4. A **ledger** holds the two dates and moves them only as far as each accepted file proves.
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

A full dump on Sunday, then only the rows changed since. This example skips the files and the row
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

Monday's file brought every new and updated row, but said nothing about deletions: a row deleted
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
