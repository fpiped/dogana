import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("duckdb")

from dogana import (
    Checkpoint,
    Column,
    Completeness,
    Contract,
    Coverage,
    Deletions,
    InvalidContract,
    Manifest,
    SerializationError,
    Shape,
    State,
)
from dogana.reference.duckdb import DuckDBStore
from dogana.reference.files import FileDrop, FilePattern

BOUNDARY = datetime(2026, 10, 18, tzinfo=UTC)
FULL = Shape(Coverage.ALL, Completeness.COMPLETE, Deletions.ABSENCE, Checkpoint.NONE)


def _contract() -> Contract:
    return Contract("files", ("id",), {"full": FULL}, schema=(Column("id", "int"),))


def _delivery(path: Path, contract: Contract) -> Manifest:
    path.write_text("id\n1\n")
    manifest = Manifest(contract.table, "full", BOUNDARY, rows=1, schema_hash=contract.schema_hash)
    path.with_suffix(path.suffix + ".json").write_text(manifest.to_json())
    return manifest


@pytest.mark.parametrize(
    "document",
    [b'\xff{"table":"files"}', b"{", b"[" * 2000 + b"0" + b"]" * 2000],
    ids=["invalid_utf8", "invalid_json", "deep_json"],
)
def test_a_malformed_sidecar_is_a_serialization_error_without_publication(
    tmp_path: Path, document: bytes
) -> None:
    path = tmp_path / "data.csv"
    c = _contract()
    _delivery(path, c)
    path.with_suffix(".csv.json").write_bytes(document)
    with DuckDBStore(":memory:") as store:
        with pytest.raises(SerializationError):
            FileDrop(store, c).ingest(path, now=BOUNDARY)
        assert store.receipts() == ()
        assert store.load_ledger(c).publication_id is None


def test_a_utf8_sidecar_preserves_the_manifest_and_submitted_source(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    c = _contract()
    manifest = replace(_delivery(path, c), generation="\u00e9", content_hash="declared-digest")
    document = json.dumps(json.loads(manifest.to_json()), ensure_ascii=False)
    path.with_suffix(".csv.json").write_bytes(document.encode("utf-8"))
    with DuckDBStore(":memory:") as store:
        receipt = FileDrop(store, c).ingest(path, now=BOUNDARY)
        assert receipt.accepted, receipt.checks
        assert receipt.manifest == manifest
        assert receipt.source == str(path)
        assert store.receipts()[-1] == receipt


@pytest.mark.parametrize("age", [timedelta(seconds=-1), timedelta(), timedelta(days=8)])
def test_supplied_receiver_time_controls_admission_and_receipt_freshness(
    tmp_path: Path, age: timedelta
) -> None:
    path = tmp_path / "data.csv"
    c = _contract()
    _delivery(path, c)
    now = BOUNDARY + age
    with DuckDBStore(":memory:") as store:
        receipt = FileDrop(store, c).ingest(path, now=now)
        assert receipt.received_at == now
        assert receipt.accepted == (age >= timedelta())
        if age > c.freshness:
            assert receipt.claims.state is State.EXPIRED
        elif receipt.accepted:
            assert receipt.claims.state is State.COMPLETE
        else:
            assert store.load_ledger(c).publication_id is None
        assert store.receipts()[-1] == receipt


@pytest.mark.parametrize("explicit_root", [False, True])
@pytest.mark.parametrize("target", ["file", "sidecar", "dangling_sidecar"])
def test_an_escaping_file_or_sidecar_is_refused_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_root: bool, target: str
) -> None:
    root = tmp_path / "drop"
    root.mkdir()
    c = _contract()
    outside = tmp_path / "outside.csv"
    manifest = _delivery(outside, c)
    path = root / "2026-10-18T00:00:00Z.csv"
    if target == "file":
        path.symlink_to(outside)
        path.with_suffix(".csv.json").write_text(manifest.to_json())
    else:
        path.write_text("id\n1\n")
        destination = outside.with_suffix(".csv.json") if target == "sidecar" else tmp_path / "missing"
        path.with_suffix(".csv.json").symlink_to(destination)

    def refuse_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("unsafe delivery opened a file")

    pattern = FilePattern(r"(?P<produced_at>.*)\.csv", shape="full")
    with DuckDBStore(":memory:") as store:
        monkeypatch.setattr(Path, "open", refuse_open)
        drop = FileDrop(store, c, pattern, root=root if explicit_root else None)
        with pytest.raises(InvalidContract, match="escapes the drop root"):
            drop.ingest(path)
        assert store.receipts() == ()
        assert store.load_ledger(c).publication_id is None


@pytest.mark.parametrize("use_symlink", [False, True])
def test_an_explicit_root_rejects_sibling_paths_and_directory_symlink_escapes(
    tmp_path: Path, use_symlink: bool
) -> None:
    root, sibling = tmp_path / "drop", tmp_path / "drop-other"
    root.mkdir()
    sibling.mkdir()
    c = _contract()
    path = sibling / "data.csv"
    _delivery(path, c)
    if use_symlink:
        (root / "nested").symlink_to(sibling, target_is_directory=True)
        path = root / "nested" / path.name
    with DuckDBStore(":memory:") as store:
        with pytest.raises(InvalidContract, match="escapes the drop root"):
            FileDrop(store, c, root=root).ingest(path)
        assert store.receipts() == ()


@pytest.mark.parametrize("use_sidecar", [False, True])
def test_an_internal_symlink_preserves_the_submitted_filename_and_manifest(
    tmp_path: Path, use_sidecar: bool
) -> None:
    c = _contract()
    data = tmp_path / "data.csv"
    expected = _delivery(data, c)
    path = tmp_path / "2026-10-18T00:00:00Z.csv"
    path.symlink_to(data)
    if use_sidecar:
        path.with_suffix(".csv.json").symlink_to(data.with_suffix(".csv.json"))
    pattern = FilePattern(r"(?P<produced_at>.*)\.csv", shape="full")
    with DuckDBStore(":memory:") as store:
        receipt = FileDrop(store, c, pattern, root=tmp_path).ingest(path)
        assert receipt.accepted, receipt.checks
        assert receipt.manifest == expected
        assert receipt.source == str(path)
        assert store.receipts()[-1].source == str(path)
        assert store.read(c.table) == [{"id": 1}]


@pytest.mark.parametrize("character", ["*", "?", "[", "]", "{", "}"])
@pytest.mark.parametrize("parent_component", [False, True])
def test_glob_characters_are_refused_before_reading_a_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, character: str, parent_component: bool
) -> None:
    folder = tmp_path / f"drop{character}" if parent_component else tmp_path
    folder.mkdir(exist_ok=True)
    path = folder / ("data.csv" if parent_component else f"data{character}.csv")
    c = _contract()
    _delivery(path, c)

    def refuse_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("glob delivery read a sidecar")

    with DuckDBStore(":memory:") as store:
        monkeypatch.setattr(Path, "read_text", refuse_read)
        with pytest.raises(InvalidContract, match="glob characters"):
            FileDrop(store, c).ingest(path)
        assert store.receipts() == ()


@pytest.mark.parametrize("extension", ["csv", "parquet"])
@pytest.mark.parametrize("declared_rows", [False, True])
def test_direct_filename_derivation_refuses_globs_before_counting(
    tmp_path: Path, extension: str, declared_rows: bool
) -> None:
    count = r"_(?P<rows>0)" if declared_rows else ""
    pattern = FilePattern(r"(?P<produced_at>[^_]+)" + count + r"\*\." + extension, shape="full")
    suffix = "_0" if declared_rows else ""
    path = tmp_path / f"2026-10-18T00:00:00Z{suffix}*.{extension}"
    with pytest.raises(InvalidContract, match="glob characters"):
        pattern.manifest(path, _contract())


def test_a_plain_alias_cannot_hide_glob_characters_in_its_resolved_path(tmp_path: Path) -> None:
    path = tmp_path / "2026-10-18T00:00:00Z.csv"
    path.symlink_to(tmp_path / "*.csv")
    pattern = FilePattern(r"(?P<produced_at>.*)\.csv", shape="full")
    with pytest.raises(InvalidContract, match="glob characters"):
        pattern.manifest(path, _contract())
    with DuckDBStore(":memory:") as store:
        with pytest.raises(InvalidContract, match="glob characters"):
            FileDrop(store, _contract(), pattern).ingest(path)


@pytest.mark.parametrize("target", ["file", "sidecar"])
def test_directories_cannot_be_delivered_as_files_or_sidecars(tmp_path: Path, target: str) -> None:
    path = tmp_path / "data.csv"
    if target == "file":
        path.mkdir()
    else:
        path.write_text("id\n1\n")
        path.with_suffix(".csv.json").mkdir()
    with DuckDBStore(":memory:") as store:
        with pytest.raises(InvalidContract, match="regular file"):
            FileDrop(store, _contract()).ingest(path)
        assert store.receipts() == ()


@pytest.mark.parametrize("field", ["produced_at", "since", "low", "high"])
@pytest.mark.parametrize("declared_timezone", [False, True])
def test_every_filename_datetime_needs_an_offset_or_declared_timezone(
    field: str, declared_timezone: bool
) -> None:
    shape = Shape(Coverage.WINDOW, Completeness.CHANGED_ONLY, Deletions.NONE, Checkpoint.TIMESTAMP)
    c = Contract("events", ("id",), {"recent": shape}, "at", (Column("id", "int"), Column("at", "datetime")))
    values = {
        "produced_at": "2026-10-19T02:00:00+02:00",
        "since": "2026-10-18T02:00:00+02:00",
        "low": "2026-10-18T02:00:00+02:00",
        "high": "2026-10-19T02:00:00+02:00",
    }
    values[field] = values[field].removesuffix("+02:00")
    pattern = FilePattern(
        r"(?P<produced_at>[^_]+)__(?P<since>[^_]+)__(?P<low>[^_]+)__(?P<high>[^_]+)__(?P<rows>0)\.csv",
        shape="recent",
        timezone=timezone(timedelta(hours=2)) if declared_timezone else None,
    )
    filename = "__".join(values.values()) + "__0.csv"
    if not declared_timezone:
        with pytest.raises(InvalidContract, match="declared timezone"):
            pattern.manifest(filename, c)
    else:
        manifest = pattern.manifest(filename, c)
        assert manifest.produced_at == BOUNDARY + timedelta(days=1)
        assert manifest.since == BOUNDARY
        assert manifest.window == (BOUNDARY, BOUNDARY + timedelta(days=1))
        manifest.check(c)


def test_a_date_only_source_boundary_requires_an_explicit_timezone() -> None:
    expression = r"(?P<produced_at>2026-10-18)_(?P<rows>0)\.csv"
    with pytest.raises(InvalidContract, match="declared timezone"):
        FilePattern(expression, shape="full").manifest("2026-10-18_0.csv", _contract())
    manifest = FilePattern(expression, shape="full", timezone=UTC).manifest("2026-10-18_0.csv", _contract())
    assert manifest.produced_at == BOUNDARY


def test_a_declared_timezone_does_not_override_an_explicit_offset_or_parse_generation_strings() -> None:
    shape = Shape(Coverage.KEYS, Completeness.CHANGED_ONLY, Deletions.TOMBSTONE, Checkpoint.GENERATION)
    c = Contract("files", ("id",), {"feed": shape})
    pattern = FilePattern(
        r"(?P<produced_at>[^_]+)__(?P<since>[^_]+)__(?P<generation>[^_]+)__(?P<rows>0)\.csv",
        shape="feed",
        timezone=UTC,
    )
    manifest = pattern.manifest("2026-10-18T02:00:00+02:00__2026-10-17__2026-10-18__0.csv", c)
    assert manifest.produced_at == BOUNDARY
    assert manifest.since == "2026-10-17"
    assert manifest.generation == "2026-10-18"


def test_date_window_bounds_remain_dates_without_a_declared_timezone() -> None:
    c = Contract(
        "files",
        ("id",),
        {"daily": Shape(Coverage.WINDOW, Completeness.COMPLETE, Deletions.ABSENCE, Checkpoint.NONE)},
        "day",
        (Column("id", "int"), Column("day", "date")),
    )
    pattern = FilePattern(
        r"(?P<produced_at>[^_]+)__(?P<low>[^_]+)__(?P<high>[^_]+)__(?P<rows>0)\.csv", shape="daily"
    )
    manifest = pattern.manifest("2026-10-18T00:00:00Z__2026-10-01__2026-10-18__0.csv", c)
    assert manifest.window == (date(2026, 10, 1), date(2026, 10, 18))


@pytest.mark.parametrize("wall_time", ["2026-03-29T02:30:00", "2026-10-25T02:30:00"])
def test_a_dst_gap_or_fold_requires_an_explicit_offset(wall_time: str) -> None:
    pattern = FilePattern(
        r"(?P<produced_at>.*)_(?P<rows>0)\.csv", shape="full", timezone=ZoneInfo("Europe/Rome")
    )
    with pytest.raises(InvalidContract, match="explicit UTC offset"):
        pattern.manifest(f"{wall_time}_0.csv", _contract())


def test_a_window_derived_from_local_time_uses_utc_duration_across_dst() -> None:
    c = Contract(
        "files",
        ("id",),
        {
            "daily": Shape(
                Coverage.WINDOW,
                Completeness.COMPLETE,
                Deletions.ABSENCE,
                Checkpoint.NONE,
                window_size=timedelta(days=1),
            )
        },
        "at",
        (Column("id", "int"), Column("at", "datetime")),
    )
    pattern = FilePattern(
        r"(?P<produced_at>.*)_(?P<rows>0)\.csv", shape="daily", timezone=ZoneInfo("Europe/Rome")
    )
    manifest = pattern.manifest("2026-03-30T00:00:00_0.csv", c)
    assert manifest.window == (datetime(2026, 3, 28, 22, tzinfo=UTC), datetime(2026, 3, 29, 22, tzinfo=UTC))
