"""What the store publishes is what the sender hashed and counted: one read, one grammar."""

from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import duckdb
import pytest
from example_store import DuckDBStore, FileDrop, FilePattern
from example_store._sources import _count_rows, _FileSource

from dogana import Contract, InvalidContract, Manifest, deliver
from tests.support import FULL, at, schema_hash

COLUMNS = {"id": "int", "v": "str"}
C = Contract("t", ("id",), {"full": FULL}, types={"id": "int"}, schema_hash=schema_hash(COLUMNS))


def declare(path: Path, rows: int, *, hashed: bool = True) -> Manifest:
    digest = sha256(path.read_bytes()).hexdigest() if hashed else None
    manifest = Manifest("t", "full", at(18), rows=rows, schema_hash=C.schema_hash, content_hash=digest)
    path.with_suffix(path.suffix + ".json").write_text(manifest.to_json())
    return manifest


def published(store: DuckDBStore) -> list[tuple[object, ...]]:
    return store.connection.execute('SELECT * FROM "t" ORDER BY ALL').fetchall()


def test_a_link_swapped_after_the_root_check_is_refused_not_read(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.csv").write_text("id,v\n7,secret\n")
    (root / "in.csv").write_text("id,v\n1,a\n")
    link = root / "f.csv"
    link.symlink_to(root / "in.csv")
    declare(link, 1, hashed=False)
    with DuckDBStore(":memory:") as store:
        load = store.load_ledger

        def swap(contract):
            link.unlink()
            link.symlink_to(outside / "secret.csv")
            return load(contract)

        store.load_ledger = swap
        receipt = FileDrop(store, C, columns=COLUMNS, root=root).ingest(link, now=at(19))
        assert not receipt.accepted
        assert store.receipts()[-1] == receipt


@pytest.mark.protocol("trust-model.4")
@pytest.mark.parametrize(
    ("data", "rows", "values"),
    [
        (b"id,v\n1,'quoted'\n2,plain\n", 2, [(1, "'quoted'"), (2, "plain")]),
        (b'id,v\n1,"two\nlines"\n2,b\n', 2, [(1, "two\nlines"), (2, "b")]),
    ],
)
def test_the_sender_and_the_store_read_one_csv_grammar(tmp_path, data, rows, values):
    path = tmp_path / "f.csv"
    path.write_bytes(data)
    assert _count_rows(path) == (rows, 0)
    declare(path, rows)
    with DuckDBStore(":memory:") as store:
        receipt = FileDrop(store, C, columns=COLUMNS).ingest(path, now=at(19))
        assert receipt.accepted, receipt.checks
        assert published(store) == values


@pytest.mark.protocol("trust-model.4")
def test_a_line_starting_with_a_hash_is_a_row(tmp_path):
    columns = {"v": "str", "id": "int"}
    contract = replace(C, schema_hash=schema_hash(columns))
    path = tmp_path / "f.csv"
    path.write_text("v,id\n#x,1\ny,2\n")
    manifest = Manifest("t", "full", at(18), rows=2, schema_hash=contract.schema_hash)
    with DuckDBStore(":memory:") as store:
        assert deliver(store, contract, _FileSource(path, columns), manifest, at(19)).accepted
        assert published(store) == [("#x", 1), ("y", 2)]


def _uint64_parquet(path: Path) -> None:
    with duckdb.connect() as connection:
        connection.execute(f"COPY (SELECT (2::UBIGINT ** 63)::UBIGINT AS id, 'a' AS v) TO '{path}'")


@pytest.mark.protocol("trust-model.4")
@pytest.mark.parametrize(
    ("name", "write"),
    [
        ("latin1.csv", lambda p: p.write_bytes(b"id,v\n1,caf\xe9\n")),
        ("gzip.csv", lambda p: p.write_bytes(b"\x1f\x8b\x08\x00" + bytes(16))),
        ("big.parquet", _uint64_parquet),
    ],
)
def test_a_file_that_does_not_read_is_a_recorded_rejection(tmp_path, name, write):
    path = tmp_path / name
    write(path)
    manifest = declare(path, 1)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, C, _FileSource(path, COLUMNS), manifest, at(19))
        assert not receipt.accepted
        assert store.receipts() == (receipt,)


@pytest.mark.protocol("trust-model.4")
def test_a_parquet_time_without_a_zone_is_refused(tmp_path):
    columns = {"id": "int", "at": "datetime"}
    contract = Contract("t", ("id",), {"full": FULL}, types={"id": "int"}, schema_hash=schema_hash(columns))
    path = tmp_path / "f.parquet"
    with duckdb.connect() as connection:
        connection.execute(f"COPY (SELECT 1 AS id, TIMESTAMP '2026-10-18 12:00' AS at) TO '{path}'")
    manifest = Manifest("t", "full", at(18), rows=1, schema_hash=contract.schema_hash)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, contract, path, manifest, at(19))
        assert not receipt.accepted
        assert not next(check for check in receipt.checks if check.name == "schema_hash").passed


@pytest.mark.protocol("manifest.6")
def test_a_changed_file_under_a_hashless_sidecar_is_a_new_delivery(tmp_path):
    path = tmp_path / "f.csv"
    path.write_text("id,v\n1,a\n")
    declare(path, 1, hashed=False)
    with DuckDBStore(":memory:") as store:
        drop = FileDrop(store, C, columns=COLUMNS)
        assert drop.ingest(path, now=at(19)).accepted
        path.write_text("id,v\n2,b\n")
        assert drop.ingest(path, now=at(19)).outcome == "rejected"
        assert published(store) == [(1, "a")]


@pytest.mark.protocol("store.1")
def test_a_replay_is_answered_by_its_bytes_under_any_later_contract(tmp_path):
    path = tmp_path / "f.csv"
    path.write_text("id,v\n1,a\n")
    manifest = declare(path, 1)
    with DuckDBStore(":memory:") as store:
        assert deliver(store, C, _FileSource(path, COLUMNS), manifest, at(19)).accepted
        evolved = replace(C, schema_hash=schema_hash({**COLUMNS, "w": "str"}))
        assert deliver(store, evolved, _FileSource(path, COLUMNS), manifest, at(19)).outcome == "replayed"
        path.write_text("id,v\n1,b\n")
        assert deliver(store, evolved, _FileSource(path, COLUMNS), manifest, at(19)).outcome == "rejected"
        assert [r.outcome for r in store.receipts()] == ["accepted", "rejected"]


def test_a_writer_that_loses_a_race_is_answered_for_its_own_bytes(tmp_path):
    good, bad = tmp_path / "good.csv", tmp_path / "bad.csv"
    good.write_text("id,v\n1,a\n")
    bad.write_text("id,v\n1,forged\n")
    manifest = declare(good, 1)
    database = tmp_path / "landing.duckdb"
    with DuckDBStore(database) as winner, DuckDBStore(database) as loser:
        check = loser.check

        def race(*args):
            assert deliver(winner, C, _FileSource(good, COLUMNS), manifest, at(19)).accepted
            return check(*args)

        loser.check = race
        receipt = deliver(loser, C, _FileSource(bad, COLUMNS), manifest, at(19))
        assert receipt.outcome == "rejected"
        assert not next(check for check in receipt.checks if check.name == "content_hash").passed
        assert published(winner) == [(1, "a")]


def test_filename_counts_must_be_integers(tmp_path):
    path = tmp_path / "f_2026-10-18T00:00:00Z_.csv"
    path.write_text("id,v\n1,a\n")
    pattern = FilePattern(r"f_(?P<produced_at>[^_]+)_(?P<rows>\d*)\.csv", shape="full")
    with DuckDBStore(":memory:") as store, pytest.raises(InvalidContract, match="integer"):
        FileDrop(store, C, pattern, columns=COLUMNS).ingest(path, now=at(19))


MONEY = {"id": "int", "amount": "decimal(18,2)"}
PRICED = Contract("t", ("id",), {"full": FULL}, types={"id": "int"}, schema_hash=schema_hash(MONEY))


def _priced(rows: int) -> Manifest:
    return Manifest("t", "full", at(18), rows=rows, schema_hash=PRICED.schema_hash)


def test_a_parquet_decimal_is_published_exactly(tmp_path):
    from decimal import Decimal

    path = tmp_path / "f.parquet"
    with duckdb.connect() as connection:
        connection.execute(f"COPY (SELECT 1 AS id, 1234567890123456.78::DECIMAL(18,2) AS amount) TO '{path}'")
    with DuckDBStore(":memory:") as store:
        assert deliver(store, PRICED, _FileSource(path, MONEY), _priced(1), at(19)).accepted
        assert store.read("t") == [{"id": 1, "amount": Decimal("1234567890123456.78")}]


def test_a_decimal_is_never_read_as_a_float(tmp_path):
    path = tmp_path / "f.parquet"
    with duckdb.connect() as connection:
        connection.execute(f"COPY (SELECT 1 AS id, 1.25::DECIMAL(18,2) AS amount) TO '{path}'")
    floats = {"id": "int", "amount": "float"}
    contract = replace(PRICED, schema_hash=schema_hash(floats))
    manifest = Manifest("t", "full", at(18), rows=1, schema_hash=contract.schema_hash)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, contract, _FileSource(path, floats), manifest, at(19))
        assert not receipt.accepted


@pytest.mark.parametrize(
    ("value", "accepted"), [("12.30", True), ("-5", True), ("1.234", False), ("1e3", False)]
)
def test_a_csv_decimal_is_read_exactly_or_refused(tmp_path, value, accepted):
    from decimal import Decimal

    path = tmp_path / "f.csv"
    path.write_text(f"id,amount\n1,{value}\n")
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, PRICED, _FileSource(path, MONEY), _priced(1), at(19))
        assert receipt.accepted is accepted
        assert not accepted or store.read("t") == [{"id": 1, "amount": Decimal(value)}]


@pytest.mark.parametrize(
    "columns,message",
    [
        ({}, "nonempty mapping"),
        ([], "nonempty mapping"),
        ({"id": "int", "ID": "int"}, "distinct ignoring case"),
    ],
)
def test_explicit_source_columns_are_nonempty_mappings_with_distinct_names(columns, message):
    from example_store import Rows

    with pytest.raises(InvalidContract, match=message):
        Rows([], columns)


def test_row_sources_have_no_delivered_bytes_to_digest_or_match_a_content_hash():
    from example_store import Rows

    source = Rows([{"id": 1, "v": "a"}], COLUMNS)
    with DuckDBStore(":memory:") as store:
        assert store.digest(source) is None
        manifest = Manifest("t", "full", at(18), rows=1, schema_hash=C.schema_hash, content_hash="a" * 64)
        receipt = deliver(store, C, source, manifest, at(19))
        check = next(c for c in receipt.checks if c.name == "content_hash")
        assert not check.passed and check.detail == "the source has no delivered bytes to hash"
        assert store.receipts() == (receipt,)


@pytest.mark.parametrize(
    "source,message",
    [
        (object(), "source must be a path"),
        ([1], "source rows must be mappings"),
        ([{"id": 1}, {"id": 2, "v": "a"}], "each source row must have the same columns"),
        ([{"id": 1, "v": object()}], "unsupported value"),
        ([{"id": 1, "v": datetime(2026, 1, 1)}], "unsupported value"),
        ([{"id": 1, "v": "a"}, {"id": 2, "v": 2}], "inconsistent types"),
        ([], "empty or all-null row columns"),
        ([{"id": 1, "v": None}], "empty or all-null row columns"),
    ],
)
def test_invalid_row_sources_are_recorded_as_rejections(source, message):
    manifest = Manifest("t", "full", at(18), schema_hash=C.schema_hash)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, C, source, manifest, at(19))
        assert not receipt.accepted
        assert any(message in check.detail for check in receipt.checks if not check.passed)
        assert store.receipts() == (receipt,)
        assert not store._candidates


def test_explicit_row_types_refuse_inconsistent_values():
    from example_store import Rows

    with DuckDBStore(":memory:") as store:
        manifest = Manifest("t", "full", at(18), schema_hash=C.schema_hash)
        receipt = deliver(store, C, Rows([{"id": "one", "v": "a"}], COLUMNS), manifest, at(19))
        assert not receipt.accepted
        assert any("inconsistent types" in check.detail for check in receipt.checks)


@pytest.mark.parametrize("rows", [[], [{"id": 1, "v": None}]])
def test_explicit_types_allow_empty_or_all_null_columns(rows):
    from example_store import Rows

    with DuckDBStore(":memory:") as store:
        manifest = Manifest("t", "full", at(18), rows=len(rows), schema_hash=C.schema_hash)
        assert deliver(store, C, Rows(rows, COLUMNS), manifest, at(19)).accepted
        assert store.read("t") == rows


def test_inferred_float_columns_accept_later_integers():
    columns = {"id": "int", "v": "float"}
    c = replace(C, schema_hash=schema_hash(columns))
    with DuckDBStore(":memory:") as store:
        manifest = Manifest("t", "full", at(18), rows=2, schema_hash=c.schema_hash)
        assert deliver(store, c, [{"id": 1, "v": 1.5}, {"id": 2, "v": 2}], manifest, at(19)).accepted
        assert published(store) == [(1, 1.5), (2, 2.0)]
        assert store.connection.execute("DESCRIBE t").fetchall()[1][1] == "DOUBLE"


def test_row_batches_flush_before_the_last_partial_batch():
    rows = [{"id": i, "v": str(i)} for i in range(1001)]
    from types import SimpleNamespace

    with DuckDBStore(":memory:") as store:
        connection = store.connection
        sizes = []

        def insert(sql, batch):
            sizes.append(len(batch))
            return connection.executemany(sql, batch)

        store.connection = SimpleNamespace(execute=connection.execute, executemany=insert)
        try:
            manifest = Manifest("t", "full", at(18), rows=len(rows), schema_hash=C.schema_hash)
            assert deliver(store, C, iter(rows), manifest, at(19)).accepted
        finally:
            store.connection = connection
        assert sizes == [1000, 1]
        assert store.read("t") == rows


def test_bound_column_schema_errors_without_a_schema_hash_are_recorded_as_rejections():
    c = replace(C, schema_hash=None)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, c, [{"id": "one", "v": "a"}], Manifest("t", "full", at(18)), at(19))
        assert not receipt.accepted
        assert any(
            "source columns differ from the contract's bound column types" in check.detail
            for check in receipt.checks
            if not check.passed
        )
        assert store.receipts() == (receipt,)


def test_arrow_tables_publish_their_rows_and_schema_then_unregister_the_input():
    pa = pytest.importorskip("pyarrow")
    with DuckDBStore(":memory:") as store:
        manifest = Manifest("t", "full", at(18), rows=1, schema_hash=C.schema_hash)
        assert deliver(store, C, pa.table({"id": [1], "v": ["a"]}), manifest, at(19)).accepted
        assert published(store) == [(1, "a")]
        assert [(r[0], r[1]) for r in store.connection.execute("DESCRIBE t").fetchall()] == [
            ("id", "BIGINT"),
            ("v", "VARCHAR"),
        ]
        assert not store.connection.execute(
            "SELECT view_name FROM duckdb_views() WHERE view_name LIKE '%_input'"
        ).fetchall()


def test_rows_carry_decimals_into_a_declared_decimal_column():
    from decimal import Decimal

    from example_store import Rows

    rows = Rows([{"id": 1, "amount": Decimal("0.10")}], MONEY)
    with DuckDBStore(":memory:") as store:
        assert deliver(store, PRICED, rows, _priced(1), at(19)).accepted
        assert store.read("t") == [{"id": 1, "amount": Decimal("0.10")}]


def test_an_empty_poll_with_typed_rows_is_a_delivery():
    from example_store import Rows

    from dogana import Shape

    contract = replace(C, shapes={"full": FULL, "changes": Shape.changes()})
    with DuckDBStore(":memory:") as store:
        full = Manifest("t", "full", at(18), rows=0, schema_hash=C.schema_hash)
        assert deliver(store, contract, Rows([], COLUMNS), full, at(19)).accepted
        poll = Manifest("t", "changes", at(19), since=at(18), rows=0, schema_hash=C.schema_hash)
        assert deliver(store, contract, Rows([], COLUMNS), poll, at(19)).accepted


def test_the_fallback_file_opener_refuses_a_swapped_file_and_closes_its_descriptor(tmp_path, monkeypatch):
    import os

    from example_store import _sources

    path = tmp_path / "data.csv"
    path.write_text("id,v\n1,a\n")
    opened = []
    original = os.open

    def swap(*args, **kwargs):
        descriptor = original(*args, **kwargs)
        opened.append(descriptor)
        path.rename(tmp_path / "original.csv")
        path.write_text("id,v\n2,b\n")
        return descriptor

    monkeypatch.setattr(_sources, "_BENEATH", False)
    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(InvalidContract, match="the delivered file changed while it was opened"):
        _sources._open(path, tmp_path)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_a_file_below_two_directories_is_opened_relative_to_its_root(tmp_path):
    from example_store import _sources

    if not _sources._BENEATH:
        pytest.skip("descriptor-relative traversal is unavailable")
    folder = tmp_path / "one" / "two"
    folder.mkdir(parents=True)
    path = folder / "data.csv"
    path.write_text("id,v\n1,a\n")
    declare(path, 1)
    with DuckDBStore(":memory:") as store:
        assert FileDrop(store, C, columns=COLUMNS, root=tmp_path).ingest(path, now=at(19)).accepted
        assert published(store) == [(1, "a")]


def test_unsupported_file_extensions_are_recorded_as_rejections(tmp_path):
    path = tmp_path / "data.txt"
    path.write_text("id,v\n1,a\n")
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, C, path, declare(path, 1), at(19))
        assert not receipt.accepted
        assert any("only CSV and Parquet paths" in check.detail for check in receipt.checks)
        assert store.receipts() == (receipt,)


def test_a_folder_swapped_for_a_link_after_the_root_check_is_refused_not_read(tmp_path, monkeypatch):
    import os

    from example_store import _sources

    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "in").mkdir(parents=True)
    (outside / "in").mkdir(parents=True)
    (root / "in" / "f.csv").write_text("id,v\n1,a\n")
    (outside / "in" / "f.csv").write_text("id,v\n7,secret\n")
    real, swapped = os.open, []

    def swap_then_open(*args, **kwargs):
        if not swapped:
            swapped.append(True)
            (root / "in").rename(root / "kept")
            (root / "in").symlink_to(outside / "in")
        return real(*args, **kwargs)

    monkeypatch.setattr(_sources.os, "open", swap_then_open)
    with pytest.raises(InvalidContract, match="cannot be opened"):
        _sources._open(root / "in" / "f.csv", root)


def test_a_fifo_is_refused_without_waiting_for_a_writer(tmp_path):
    import os

    from example_store._sources import _open

    fifo = tmp_path / "f.csv"
    os.mkfifo(fifo)
    with pytest.raises(InvalidContract, match="regular files"):
        _open(fifo)


@pytest.mark.protocol("store.1")
def test_a_missing_file_replayed_is_a_recorded_refusal(tmp_path):
    path = tmp_path / "f.csv"
    path.write_text("id,v\n1,a\n")
    manifest = declare(path, 1)
    with DuckDBStore(":memory:") as store:
        assert deliver(store, C, _FileSource(path, COLUMNS), manifest, at(19)).accepted
        path.unlink()
        assert deliver(store, C, _FileSource(path, COLUMNS), manifest, at(19)).outcome == "rejected"
        assert [r.outcome for r in store.receipts()] == ["accepted", "rejected"]


@pytest.mark.protocol("trust-model.4")
def test_a_corrupt_parquet_file_is_a_recorded_rejection(tmp_path):
    good = tmp_path / "good.parquet"
    with duckdb.connect() as connection:
        connection.execute(f"COPY (SELECT range AS id, 'value' AS v FROM range(1000)) TO '{good}'")
    data = bytearray(good.read_bytes())
    data[len(data) // 2 : len(data) // 2 + 64] = bytes(64)
    path = tmp_path / "f.parquet"
    path.write_bytes(bytes(data))
    manifest = declare(path, 1000)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, C, _FileSource(path, COLUMNS), manifest, at(19))
        assert not receipt.accepted
        assert store.receipts() == (receipt,)


@pytest.mark.protocol("trust-model.4")
@pytest.mark.parametrize("value", ["2026-10-18", "2026-10-18 12:00", "12:00+02:00", "2026-10-18T12"])
def test_a_csv_datetime_needs_a_date_a_time_and_an_offset(tmp_path, value):
    columns = {"id": "int", "at": "datetime"}
    contract = Contract("t", ("id",), {"full": FULL}, types={"id": "int"}, schema_hash=schema_hash(columns))
    path = tmp_path / "f.csv"
    path.write_text(f"id,at\n1,{value}\n")
    manifest = Manifest("t", "full", at(18), rows=1, schema_hash=contract.schema_hash)
    with DuckDBStore(":memory:") as store:
        assert not deliver(store, contract, _FileSource(path, columns), manifest, at(19)).accepted


@pytest.mark.protocol("trust-model.4")
def test_a_blank_line_is_an_empty_value_in_a_one_column_file(tmp_path):
    columns = {"id": "str"}
    contract = Contract("t", ("id",), {"full": FULL}, types={"id": "str"}, schema_hash=schema_hash(columns))
    path = tmp_path / "f.csv"
    path.write_bytes(b"id\r\nx\r\n\r\ny\r\n")
    assert _count_rows(path) == (3, 0)
    manifest = Manifest("t", "full", at(18), rows=3, schema_hash=contract.schema_hash)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, contract, _FileSource(path, columns), manifest, at(19))
        assert not any(c.name == "row_count" and not c.passed for c in receipt.checks)


@pytest.mark.parametrize("value", ["1.234", "1E+20", "NaN"])
def test_rows_never_round_a_decimal(value):
    from decimal import Decimal

    from example_store import Rows

    rows = Rows([{"id": 1, "amount": Decimal(value)}], MONEY)
    with DuckDBStore(":memory:") as store:
        assert not deliver(store, PRICED, rows, _priced(1), at(19)).accepted
