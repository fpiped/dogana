# Example store on DuckDB

A complete dogana store on DuckDB and a file drop that feeds it: one way to publish rows, ledger and
receipt in one transaction. It is tested with dogana in this repository and passes the conformance
suite, but it is not part of the `dogana` package and is not published.

Install it from a checkout of the repository:

```sh
uv pip install -e . -e examples/duckdb
```

Then `from example_store import DuckDBStore, FileDrop`. The [quick start](../../docs/quickstart.md)
runs on it, and the [reference](../../docs/reference.md#example-store-on-duckdb) describes what it
checks. For your own database, copy it or write a store against `dogana.conformance.StoreConformance`.
