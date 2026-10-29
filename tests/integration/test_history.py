from datetime import UTC, datetime, timedelta

import pytest

from tests.support import COLUMNS, at, schema_hash

pytest.importorskip("duckdb")

from example_store import DuckDBStore
from example_store._sources import _FileSource

from dogana import Contract, Manifest, Shape
from tests.integration.support import csv_file, row, sidecar
from tests.support import SINCE, contract


@pytest.mark.parametrize(
    "low,high", [(datetime.min.replace(tzinfo=UTC), at(18)), (at(18), datetime.max.replace(tzinfo=UTC))]
)
def test_datetime_windows_at_domain_ends_replace_rows_and_preserve_old_publications(low, high):
    from example_store import Rows

    columns = {"id": "int", "day": "datetime", "value": "str"}
    c = contract(columns=columns)
    rows = [{"id": 1, "day": low, "value": "low"}, {"id": 2, "day": high, "value": "high"}]
    with DuckDBStore(":memory:") as store:
        first = store.deliver(c, Rows(rows, columns), c.manifest("full", at(18), rows=2), now=at(30))
        replacement = [{**rows[0], "value": "replacement"}]
        second = store.deliver(
            c, Rows(replacement, columns), c.manifest("daily", at(19), window=(low, high), rows=1), now=at(30)
        )
        assert first.accepted and second.accepted, second.checks
        assert store.read(c.table) == replacement
        assert store.read(c.table, first.publication_id) == rows
        assert store.read(c.table, second.publication_id) == replacement


def test_repeated_full_exports_retain_unchanged_row_versions_and_exact_old_publications(tmp_path):
    c = contract()
    with DuckDBStore(":memory:") as store:
        receipts = []
        snapshots = [[row(1), row(2)], [row(1), row(2)], [row(1, value="changed")], [], [row(1)]]
        for day, rows in enumerate(snapshots, 18):
            source = csv_file(tmp_path, "snapshot.csv", rows)
            receipts.append(
                store.deliver(
                    c, _FileSource(source, COLUMNS), sidecar(source, c, "full", day, len(rows)), now=at(30)
                )
            )
            assert receipts[-1].accepted
            for receipt, expected in zip(receipts, snapshots, strict=False):
                assert store.read(c.table, receipt.publication_id) == expected
        assert store.connection.execute("SELECT count(*) FROM __dogana_history_moves").fetchone()[0] == 4
        from dogana.errors import StoreError

        with pytest.raises(StoreError):
            store.read(c.table, "unknown")


def test_append_history_preserves_duplicate_equal_events_at_each_publication(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    columns = {"event": "str"}
    c = Contract("events", (), {"log": SINCE}, schema_hash=schema_hash(columns), start_at=start)
    source = csv_file(tmp_path, "events.csv", [{"event": "same"}], columns)
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
            receipt = store.deliver(c, _FileSource(source, columns), manifest, now=at(30))
            assert receipt.accepted
            assert store.read(c.table, receipt.publication_id) == [{"event": "same"}] * index
        assert store.connection.execute("SELECT count(*) FROM __dogana_history_events").fetchone()[0] == 3


def test_history_equality_retains_null_values_without_copying_a_complete_table(tmp_path):
    columns = {"id": "int", "value": "int"}
    c = Contract(
        "nullable",
        ("id",),
        {"full": Shape(coverage="all", completeness="complete", deletions="absence", checkpoint="none")},
        types={"id": "int"},
        schema_hash=schema_hash(columns),
    )
    source = csv_file(tmp_path, "nullable.csv", [{"id": 1, "value": None}, {"id": 2, "value": 2}], columns)
    with DuckDBStore(":memory:") as store:
        for day in (18, 19):
            receipt = store.deliver(
                c, _FileSource(source, columns), sidecar(source, c, "full", day, 2), now=at(30)
            )
            assert receipt.accepted
            assert store.read(c.table, receipt.publication_id) == [
                {"id": 1, "value": None},
                {"id": 2, "value": 2},
            ]
        assert store.connection.execute("SELECT count(*) FROM __dogana_history_nullable").fetchone()[0] == 2


def test_legacy_snapshot_tables_stay_readable_after_new_publications_use_row_versions(tmp_path):
    from dogana.errors import LedgerAhead

    c = contract()
    with DuckDBStore(":memory:") as store:
        source = csv_file(tmp_path, "first.csv", [row(1)])
        first = store.deliver(c, _FileSource(source, COLUMNS), sidecar(source, c, "full", 18, 1), now=at(30))
        legacy = "__dogana_version_" + first.publication_id
        store.connection.execute(f'CREATE TABLE "{legacy}" AS SELECT * FROM moves')
        store.connection.execute("DROP TABLE __dogana_history_moves")
        store.connection.execute("DELETE FROM __dogana_publications")
        store.connection.execute("UPDATE __dogana_receipts SET document=?", [first.to_json()])
        with pytest.raises(LedgerAhead):
            store.load_ledger(c)
        assert store.read(c.table, first.publication_id) == [row(1)]
        source = csv_file(tmp_path, "next.csv", [row(2)])
        second = store.deliver(c, _FileSource(source, COLUMNS), sidecar(source, c, "full", 19, 1), now=at(30))
        assert second.accepted
        assert store.read(c.table, first.publication_id) == [row(1)]
        assert store.read(c.table, second.publication_id) == [row(2)]
        assert (
            store.connection.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE '__dogana_version_%'"
            ).fetchone()[0]
            == 1
        )
