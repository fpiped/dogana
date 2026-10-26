<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/fpiped/dogana/main/docs/assets/wordmark-dark.svg">
<img src="https://raw.githubusercontent.com/fpiped/dogana/main/docs/assets/wordmark-light.svg" alt="dogana" width="400">
</picture>
</p>

<p align="center">
<a href="https://github.com/fpiped/dogana/actions/workflows/ci.yml"><img src="https://github.com/fpiped/dogana/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
<a href="https://github.com/fpiped/dogana/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/python-3.11%2B-3776ab" alt="Python 3.11+"></a>
<a href="https://github.com/fpiped/dogana/blob/main/LICENSE"><img src="https://img.shields.io/badge/licence-Apache--2.0-355eaa" alt="Licence Apache-2.0"></a>
</p>

# Know which rows you can trust

Not every source has change data capture. Most data still arrives as exports: a full dump on
Sunday, the last 30 days every night, the rows changed since yesterday. Each export proves
something different, almost none says what was deleted, and once the rows are loaded nobody
remembers which proved what.

dogana remembers, as two dates for every part of a table:

- **complete as of**: nothing is missing;
- **deletions as of**: nothing deleted is still here.

A delivery never raises a date above what it proves.

## How it works

1. The receiver writes a **contract** for each table: its key and the kinds of delivery it accepts.
2. Every file arrives with a **manifest**: which kind it is, as of when, how many rows, the SHA-256
   of its bytes.
3. A **gate** checks the file against both and refuses a contradiction whole.
4. A **ledger** moves the two dates only as far as the delivery proves, and anyone reading the
   table can ask it how far to trust the rows.

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

Monday's changes brought every new and updated row, but no deletions: a row deleted at the
source since Sunday may still be here. That sentence, for every table, is what dogana is for.

## Use it

dogana runs inside whatever already loads your tables: a cron job, an Airflow task, a dlt
pipeline. The core is pure Python with no dependencies. The `duckdb` extra adds a store that
commits rows and dates in one transaction, and a conformance suite proves a store for your own
warehouse. Senders in any language write their manifest with `dogana manifest`.

dogana is in beta: the manifest and ledger format may still change before 1.0.
Install it with `pip install "dogana[duckdb]"`, or `pip install dogana` for the core alone.

If all your sources have change data capture with deletes, you already know which rows to trust.
dogana is for everything else.

## Learn more

- [Quick start](https://github.com/fpiped/dogana/blob/main/docs/quickstart.md): a store, a refused file, a replay.
- [Concepts](https://github.com/fpiped/dogana/blob/main/docs/concepts.md): every term, and what dogana is not.
- [Protocol](https://github.com/fpiped/dogana/blob/main/docs/protocol.md): the specification.
- [Integrations](https://github.com/fpiped/dogana/blob/main/docs/integrations.md): dlt, dbt, Iceberg, Spark, orchestrators.

Licensed under [Apache-2.0](https://github.com/fpiped/dogana/blob/main/LICENSE).
