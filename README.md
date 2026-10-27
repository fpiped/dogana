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

dogana is a Python library for tables loaded from exports. It checks each delivered file against
its sender's manifest and the receiver's contract, and keeps in a ledger what every delivery shows
about the table.

Not every source has change data capture, a log of every insert, update and delete. Most data still arrives as exports: a full dump on
Sunday, the orders of the last 30 days by order date every night, the rows changed since yesterday.
Each export says something different, almost none says what was deleted, and once the rows are
loaded nobody remembers which proved what.

dogana remembers, as two dates for every part of a table (the whole table, or a range of its order
date or another date that never changes):

- **complete as of**: every row the source had then is here;
- **deletions as of**: no row the source deleted before then is still here.

A delivery never raises a date beyond what its manifest declares and the checks confirm. dogana
checks every file against its declaration; that the source exported every row it should have is the
sender's word, which no file can prove.

## How it works

1. The receiver writes a **contract** for each table: its key and the kinds of delivery it accepts.
2. Every file arrives with a **manifest**: which kind it is, as of when, how many rows and deletions,
   the SHA-256 of its bytes. Deleted keys travel in the file, as rows marked deleted.
3. A **gate** checks the file against both, and rejects the whole file if any check fails.
4. A **ledger** moves the two dates only as far as the checked delivery reaches, and anyone reading the
   table can ask it how far to trust the rows.
5. Tables that depend on each other, such as orders and their lines, can be delivered **together**
   from one consistent read: one decision, one publication, and a date that says when a join
   between them answers as the source would have.

*Dogana* (doh-GAH-nah) is Italian for customs: goods cross a border with a declaration, an officer
checks them against it, and they enter stamped or are held at the border. Here the goods are rows,
the declaration is the manifest, the officer is the gate and the stamped declaration is the receipt.

## In thirty seconds

A full dump on Sunday, then only the rows changed since:

```python
from datetime import UTC, datetime

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

claims = ledger.claims(monday)
print(
    f"{claims.state}: complete as of {claims.complete_as_of:%A}, deletions as of {claims.deletions_as_of:%A}"
)
```

```text
patched: complete as of Monday, deletions as of Sunday
```

Monday's changes brought every new and updated row, but no deletions: a row physically deleted at
the source since Sunday may still be here. That is what patched means. That sentence, for every table, is what dogana is for.

## Use it

dogana runs inside whatever already loads your tables: a cron job, an Airflow task, a dlt
pipeline. The package is pure Python with no dependencies and never parses a data file: your
loader checks the rows and hands dogana the results, or a store you write for your database
publishes rows and dates in one transaction, tested by the conformance suite. The repository's
[`examples/duckdb`](https://github.com/fpiped/dogana/tree/main/examples/duckdb) holds one such
store on DuckDB and a file drop that feeds it, outside the package; the quick start runs on them. A
sender writes its manifest as JSON, in any language, or with `dogana manifest`.

dogana is in beta: the manifest and ledger format may still change before 1.0.
Install it with `pip install dogana`.

If all your sources have change data capture with deletes, you already know which rows to trust.
dogana is for everything else.
<!-- --8<-- [end:body] -->

## Learn more

The documentation is at [fpiped.github.io/dogana](https://fpiped.github.io/dogana/).

- [Quick start](https://fpiped.github.io/dogana/quickstart/): a store, a rejected file, a replay, a deletion, two tables together.
- [Concepts](https://fpiped.github.io/dogana/concepts/): dogana from the beginning, every idea with an example.
- [Protocol](https://fpiped.github.io/dogana/protocol/): the specification.
- [Integrations](https://fpiped.github.io/dogana/integrations/): how dlt, dbt, Iceberg and orchestrators would call dogana; no adapter ships yet.

Licensed under [Apache-2.0](https://github.com/fpiped/dogana/blob/main/LICENSE).
