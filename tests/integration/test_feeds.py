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


@pytest.mark.parametrize(
    "data,message",
    [
        (b"id,deleted\n1,false,extra\n", "a record has 3 fields"),
        (b"id,deleted\n1,maybe\n", "deleted must be true or false"),
        (b"id,deleted\n1,\xff\n", "not UTF-8 CSV"),
        (b'id,deleted\n1,"unclosed', "not UTF-8 CSV"),
    ],
)
def test_csv_count_derivation_refuses_malformed_rows_markers_and_encoding(tmp_path, data, message):
    from example_store._sources import _count_rows

    path = tmp_path / "bad.csv"
    path.write_bytes(data)
    with pytest.raises(InvalidContract, match=message):
        _count_rows(path, "deleted")


@pytest.mark.parametrize(
    "contents,message",
    [
        ("corrupt", "not a readable Parquet file"),
        ("1::INTEGER", "must be a BOOLEAN column"),
        ("NULL::BOOLEAN", "must be true or false"),
    ],
)
def test_parquet_count_derivation_refuses_corrupt_files_and_invalid_deleted_columns(
    tmp_path, contents, message
):
    import duckdb
    from example_store._sources import _count_rows

    path = tmp_path / "bad.parquet"
    if contents == "corrupt":
        path.write_bytes(b"not parquet")
    else:
        with duckdb.connect() as connection:
            connection.execute(f"COPY (SELECT {contents} AS deleted) TO ? (FORMAT PARQUET)", [str(path)])
    with pytest.raises(InvalidContract, match=message):
        _count_rows(path, "deleted")


def test_count_derivation_requires_a_csv_or_parquet_extension(tmp_path):
    from example_store._sources import _count_rows

    with pytest.raises(InvalidContract, match="row counting supports CSV and Parquet files"):
        _count_rows(tmp_path / "data.txt")


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


@pytest.mark.parametrize(
    "options,message",
    [
        (dict(expression=r"(?P<shape>full)"), "needs a produced_at group"),
        (dict(expression=r"(?P<produced_at>.*)", shape=3), "fixed shape must be a nonempty name"),
        (
            dict(expression=r"(?P<produced_at>.*)", shape="full", timezone="UTC"),
            "timezone must be a tzinfo value",
        ),
    ],
)
def test_filename_patterns_require_a_source_date_a_named_shape_and_a_timezone_value(options, message):
    with pytest.raises(InvalidContract, match=message):
        FilePattern(**options)


@pytest.mark.parametrize(
    "expression,filename,shape,message",
    [
        (r"(?P<produced_at>2026).*", "wrong.csv", "full", "filename does not match"),
        (
            r"(?P<shape>full)?(?P<produced_at>2026-10-18).csv",
            "2026-10-18.csv",
            None,
            "filename supplies no shape",
        ),
        (
            r"(?P<produced_at>2026-10-18)(?:_(?P<low>.*))?_(?P<high>x).csv",
            "2026-10-18_x.csv",
            "daily",
            "filename needs both window bounds",
        ),
        (
            r"(?P<produced_at>2026-10-18)_(?P<low>x)(?:_(?P<high>.*))?.csv",
            "2026-10-18_x.csv",
            "daily",
            "filename needs both window bounds",
        ),
        (
            r"(?P<produced_at>2026-10-18)_(?P<low>bad)_(?P<high>bad).csv",
            "2026-10-18_bad_bad.csv",
            "daily",
            "not an ISO 8601 date",
        ),
    ],
)
def test_filenames_must_supply_the_declared_shape_and_valid_window_bounds(
    expression, filename, shape, message
):
    with pytest.raises(InvalidContract, match=message):
        FilePattern(expression, shape=shape, timezone=UTC).manifest(filename, contract())


@pytest.mark.parametrize("kind,low,high", [("int", "12", "34"), ("str", "east", "west")])
def test_filename_window_bounds_use_the_contract_scalar_type(kind, low, high):
    from tests.support import accounting_contract

    c = accounting_contract(window_type=kind)
    pattern = FilePattern(
        r"(?P<produced_at>[^_]+)_(?P<low>[^_]+)_(?P<high>[^_]+)_(?P<rows>0).csv", shape="daily", timezone=UTC
    )
    manifest = pattern.manifest(f"2026-10-18_{low}_{high}_0.csv", c)
    assert manifest.window == ((int(low), int(high)) if kind == "int" else (low, high))


@pytest.mark.parametrize(
    "columns,message",
    [({**COLUMNS, "id": "str"}, "bound column types"), ({**COLUMNS, "extra": "str"}, "schema_hash")],
)
def test_file_drop_columns_match_the_bound_types_and_published_schema(columns, message):
    with DuckDBStore(":memory:") as store, pytest.raises(InvalidContract, match=message):
        FileDrop(store, contract(), columns=columns)


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
