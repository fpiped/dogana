from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("duckdb")

from dogana import Column, Contract, Manifest, Shape
from dogana.reference.duckdb import DuckDBStore
from tests.integration.support import csv_file, row, sidecar
from tests.support import SINCE, contract


def test_repeated_full_exports_retain_unchanged_row_versions_and_exact_old_publications(tmp_path):
    c = contract()
    with DuckDBStore(":memory:") as store:
        receipts = []
        snapshots = [[row(1), row(2)], [row(1), row(2)], [row(1, value="changed")], [], [row(1)]]
        for day, rows in enumerate(snapshots, 18):
            source = csv_file(tmp_path, "snapshot.csv", rows)
            receipts.append(store.deliver(c, source, sidecar(source, c, "full", day, len(rows))))
            assert receipts[-1].accepted
            for receipt, expected in zip(receipts, snapshots, strict=False):
                assert store.read(c.table, receipt.publication_id) == expected
        assert store.connection.execute("SELECT count(*) FROM __dogana_history_moves").fetchone()[0] == 4
        with pytest.raises(KeyError):
            store.read(c.table, "unknown")


def test_append_history_preserves_duplicate_equal_events_at_each_publication(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    c = Contract("events", (), {"log": SINCE}, schema=(Column("event", "str"),), start_at=start)
    source = csv_file(tmp_path, "events.csv", [{"event": "same"}], ("event",))
    with DuckDBStore(":memory:") as store:
        for index in range(1, 4):
            manifest = Manifest(
                c.table,
                "log",
                start + timedelta(hours=index),
                since=start + timedelta(hours=index - 1),
                rows=1,
                schema_hash=c.schema_hash,
            )
            receipt = store.deliver(c, source, manifest)
            assert receipt.accepted
            assert store.read(c.table, receipt.publication_id) == [{"event": "same"}] * index
        assert store.connection.execute("SELECT count(*) FROM __dogana_history_events").fetchone()[0] == 3


def test_history_equality_retains_null_values_without_copying_a_complete_table(tmp_path):
    c = Contract(
        "nullable",
        ("id",),
        {"full": Shape("all", "complete", "absence", "none")},
        schema=(Column("id", "int"), Column("value", "int")),
    )
    source = csv_file(
        tmp_path, "nullable.csv", [{"id": 1, "value": None}, {"id": 2, "value": 2}], ("id", "value")
    )
    with DuckDBStore(":memory:") as store:
        for day in (18, 19):
            receipt = store.deliver(c, source, sidecar(source, c, "full", day, 2))
            assert receipt.accepted
            assert store.read(c.table, receipt.publication_id) == [
                {"id": 1, "value": None},
                {"id": 2, "value": 2},
            ]
        assert store.connection.execute("SELECT count(*) FROM __dogana_history_nullable").fetchone()[0] == 2
