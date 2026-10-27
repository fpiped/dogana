from dataclasses import replace
from datetime import UTC, timedelta

import pytest

pytest.importorskip("duckdb")

from example_store import DuckDBStore, FileDrop, FilePattern

from dogana import InvalidContract
from tests.integration.support import csv_file, row
from tests.support import COLUMNS, FULL, WINDOW, at, contract


def test_a_fixed_feed_shape_and_counted_csv_need_only_a_source_date_in_the_filename(tmp_path):
    c = contract()
    path = csv_file(tmp_path, "orders-2026-10-19.csv", [row(1, 19, "line one\nline two"), row(2, 19)])
    pattern = FilePattern(r"orders-(?P<produced_at>\d{4}-\d{2}-\d{2})\.csv", shape="daily", timezone=UTC)
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        receipt = FileDrop(store, c, pattern, columns=COLUMNS).ingest(path, now=at(30))
        assert receipt.accepted, receipt.checks
        assert receipt.manifest.rows == 2
        assert receipt.manifest.window == (at(19).date() - timedelta(days=29), at(19).date())
        assert store.read(c.table) == [row(1, 19, "line one\nline two"), row(2, 19)]


def test_a_header_only_feed_counts_zero_rows(tmp_path):
    c = contract(shapes={"full": FULL})
    path = csv_file(tmp_path, "2026-10-18.csv", [])
    manifest = FilePattern(r"(?P<produced_at>.*)\.csv", shape="full", timezone=UTC).manifest(path, c)
    assert manifest.rows == 0


def test_a_parquet_feed_counts_physical_rows(tmp_path):
    import duckdb

    c = contract(shapes={"full": FULL})
    path = tmp_path / "full-2026-10-18.parquet"
    with duckdb.connect() as writer:
        writer.execute("COPY (SELECT * FROM range(3)) TO ? (FORMAT PARQUET)", [str(path)])
    manifest = FilePattern(r"(?P<shape>full)-(?P<produced_at>.*)\.parquet", timezone=UTC).manifest(path, c)
    assert manifest.rows == 3


def test_a_datetime_feed_derives_a_closed_duration_ending_at_the_source_boundary(tmp_path):
    columns = {"id": "int", "day": "datetime", "value": "str"}
    c = contract(columns=columns)
    path = csv_file(tmp_path, "2026-10-19.csv", [])
    manifest = FilePattern(r"(?P<produced_at>.*)\.csv", shape="daily", timezone=UTC).manifest(path, c)
    assert manifest.window == (at(19) - timedelta(days=30), at(19))


def test_explicit_bounds_and_count_override_derivation_without_reading_a_file():
    c = contract()
    pattern = FilePattern(
        r"(?P<produced_at>2026-10-19)_(?P<low>2026-10-01)_(?P<high>2026-10-19)_(?P<rows>7)\.csv",
        shape="daily",
        timezone=UTC,
    )
    manifest = pattern.manifest("2026-10-19_2026-10-01_2026-10-19_7.csv", c)
    assert manifest.rows == 7
    assert manifest.window == (at(1).date(), at(19).date())


def test_a_feed_without_any_shape_declaration_is_refused():
    with pytest.raises(InvalidContract, match="shape"):
        FilePattern(r"(?P<produced_at>.*)\.csv", timezone=UTC)


def test_a_window_feed_without_bounds_or_duration_is_refused():
    c = contract(shapes={"daily": replace(WINDOW, window_size=None)})
    pattern = FilePattern(r"(?P<produced_at>.*)_(?P<rows>0)\.csv", shape="daily", timezone=UTC)
    with pytest.raises(InvalidContract, match="bounds or window_size"):
        pattern.manifest("2026-10-19_0.csv", c)


def test_a_feed_cannot_supply_only_one_bound_group():
    with pytest.raises(InvalidContract, match="both window bounds"):
        FilePattern(r"(?P<produced_at>.*)_(?P<low>.*)\.csv", shape="daily", timezone=UTC)


def test_a_filename_cannot_override_a_conflicting_fixed_feed_shape():
    pattern = FilePattern(
        r"(?P<shape>full)_(?P<produced_at>.*)_(?P<rows>0)\.csv", shape="daily", timezone=UTC
    )
    with pytest.raises(InvalidContract, match="differs"):
        pattern.manifest("full_2026-10-19_0.csv", contract())


def test_a_derived_date_window_ends_on_the_utc_source_date_even_with_a_filename_offset():
    c = contract()
    pattern = FilePattern(r"(?P<produced_at>.*)_(?P<rows>0)\.csv", shape="daily", timezone=UTC)
    manifest = pattern.manifest("2026-10-19T00:30:00+02:00_0.csv", c)
    assert manifest.produced_at == at(18, 22) + timedelta(minutes=30)
    assert manifest.window == (at(18).date() - timedelta(days=29), at(18).date())
