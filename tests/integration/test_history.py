from datetime import UTC, datetime, timedelta

import pytest

from tests.support import at

pytest.importorskip("duckdb")

from dogana import Column, Contract, Manifest, Shape
from dogana.reference.duckdb import DuckDBStore
from tests.integration.support import csv_file, row, sidecar
from tests.support import SINCE, contract


def test_repeated_full_exports_retain_unchanged_row_versions_and_exact_old_publications(tmp_path):
    c = contract(max_shrink_fraction=None)
    with DuckDBStore(":memory:") as store:
        receipts = []
        snapshots = [[row(1), row(2)], [row(1), row(2)], [row(1, value="changed")], [], [row(1)]]
        for day, rows in enumerate(snapshots, 18):
            source = csv_file(tmp_path, "snapshot.csv", rows)
            receipts.append(store.deliver(c, source, sidecar(source, c, "full", day, len(rows)), now=at(30)))
            assert receipts[-1].accepted
            for receipt, expected in zip(receipts, snapshots, strict=False):
                assert store.read(c.table, receipt.publication_id) == expected
        assert store.connection.execute("SELECT count(*) FROM __dogana_history_moves").fetchone()[0] == 4
        from dogana.errors import StoreError

        with pytest.raises(StoreError):
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
            receipt = store.deliver(c, source, manifest, now=at(30))
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
            receipt = store.deliver(c, source, sidecar(source, c, "full", day, 2), now=at(30))
            assert receipt.accepted
            assert store.read(c.table, receipt.publication_id) == [
                {"id": 1, "value": None},
                {"id": 2, "value": 2},
            ]
        assert store.connection.execute("SELECT count(*) FROM __dogana_history_nullable").fetchone()[0] == 2


def test_legacy_snapshot_tables_stay_readable_after_new_publications_use_row_versions(tmp_path):
    from dogana import InconsistentPublication

    c = contract(max_shrink_fraction=None)
    with DuckDBStore(":memory:") as store:
        source = csv_file(tmp_path, "first.csv", [row(1)])
        first = store.deliver(c, source, sidecar(source, c, "full", 18, 1), now=at(30))
        legacy = "__dogana_version_" + first.publication_id
        store.connection.execute(f'CREATE TABLE "{legacy}" AS SELECT * FROM moves')
        store.connection.execute("DROP TABLE __dogana_history_moves")
        store.connection.execute("DELETE FROM __dogana_publications")
        store.connection.execute("UPDATE __dogana_receipts SET document=?", [first.to_json()])
        with pytest.raises(InconsistentPublication, match="ledger ahead"):
            store.load_ledger(c)
        assert store.read(c.table, first.publication_id) == [row(1)]
        source = csv_file(tmp_path, "next.csv", [row(2)])
        second = store.deliver(c, source, sidecar(source, c, "full", 19, 1), now=at(30))
        assert second.accepted
        assert store.read(c.table, first.publication_id) == [row(1)]
        assert store.read(c.table, second.publication_id) == [row(2)]
        assert (
            store.connection.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE '__dogana_version_%'"
            ).fetchone()[0]
            == 1
        )
