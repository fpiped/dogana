"""What the store publishes is what the sender hashed and counted: one read, one grammar."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import duckdb
import pytest

from dogana import Contract, InvalidContract, Manifest, deliver
from dogana.reference._sources import _count_rows, _FileSource
from dogana.reference.duckdb import DuckDBStore
from dogana.reference.files import FileDrop, FilePattern
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
