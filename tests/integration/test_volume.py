import csv
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import perf_counter

import pytest

from tests.support import at

pytest.importorskip("duckdb")

from dogana import Column, Contract, Ledger, Manifest, Shape
from dogana.reference.duckdb import DuckDBStore

FULL = Shape("all", "complete", "absence", "none")
SINCE = Shape("all", "changed_only", "none", "timestamp")


def test_a_200k_row_delivery_finishes_within_thirty_seconds_and_replays_once(tmp_path):
    c = Contract("bulk", ("id",), {"full": FULL}, schema=(Column("id", "int"), Column("value", "str")))
    path = tmp_path / "bulk.csv"
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("id", "value"))
        writer.writerows((i, f"value{i}") for i in range(200_000))
    manifest = Manifest(
        c.table, "full", datetime(2026, 1, 1, tzinfo=UTC), rows=200_000, schema_hash=c.schema_hash
    )
    with DuckDBStore(tmp_path / "bulk.duckdb") as store:
        start = perf_counter()
        receipt = store.deliver(c, path, manifest, now=at(30))
        elapsed = perf_counter() - start
        assert receipt.accepted, receipt.checks
        assert elapsed < 30, elapsed
        assert store.connection.execute("SELECT count(*) FROM bulk").fetchone()[0] == 200_000
        assert store.deliver(c, object(), manifest, now=at(30)) == replace(receipt, replayed=True)
        assert Ledger.from_receipts(c, store.receipts(c.table)) == store.load_ledger(c)


def test_300_small_deliveries_keep_one_receipt_per_publication_and_bounded_row_history(tmp_path):
    c = Contract(
        "growing",
        ("id",),
        {"full": FULL, "since": SINCE},
        schema=(Column("id", "int"), Column("value", "str")),
    )
    boundary = datetime(2026, 1, 1, tzinfo=UTC)
    path = tmp_path / "small.csv"
    accepted = []
    with DuckDBStore(tmp_path / "growing.duckdb") as store:
        start = perf_counter()
        for index in range(300):
            path.write_text(f"id,value\n{index},value{index}\n")
            manifest = Manifest(
                c.table,
                "full" if index == 0 else "since",
                boundary + timedelta(seconds=index),
                since=None if index == 0 else boundary + timedelta(seconds=index - 1),
                rows=1,
                schema_hash=c.schema_hash,
            )
            receipt = store.deliver(c, path, manifest, now=at(30))
            assert receipt.accepted, receipt.checks
            accepted.append(receipt)
            assert store.load_ledger(c) == Ledger.from_receipts(c, accepted)
        elapsed = perf_counter() - start
        assert elapsed < 30, elapsed
        assert len(store.read(c.table)) == 300
        persisted = store.receipts(c.table)
        assert len(persisted) == 300
        assert persisted == tuple(accepted)
        assert (
            store.connection.execute("SELECT max(length(document)) FROM __dogana_receipts").fetchone()[0]
            < 4_000
        )
        assert len(store.load_ledger(c).to_json()) < 30_000
        assert store.read(c.table, accepted[0].publication_id) == [{"id": 0, "value": "value0"}]
        assert len(store.read(c.table, accepted[149].publication_id)) == 150
        assert store.connection.execute("SELECT count(*) FROM __dogana_history_growing").fetchone()[0] == 300
        assert not store.connection.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE '__dogana_version_%'"
        ).fetchone()[0]
