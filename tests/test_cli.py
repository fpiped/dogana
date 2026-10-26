import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from dogana import Checkpoint, Contract, Manifest, Shape
from dogana.cli import main
from tests.support import schema_hash

COLUMNS = {"id": "int", "day": "date", "value": "str"}
BOUNDARY = "2026-10-26T06:00:00Z"


def contract(**kwargs):
    defaults = dict(
        shapes={
            "full": Shape.full(),
            "daily": Shape.window(size=timedelta(days=30)),
            "changes": Shape.changes(),
            "feed": Shape.feed(),
        },
        window_column="day",
        types={"id": "int", "day": "date"},
        schema_hash=schema_hash(COLUMNS),
    )
    return Contract("orders", ("id",), **{**defaults, **kwargs})


@pytest.fixture
def files(tmp_path):
    (tmp_path / "orders.contract.json").write_text(contract().to_json())
    data = tmp_path / "orders.csv"
    data.write_text('id,day,value\n1,2026-10-25,"two\nlines"\n2,2026-10-26,b\n')
    return tmp_path, data


def run(tmp_path, *args):
    return main([*args, "--contract", str(tmp_path / "orders.contract.json")])


def written(data):
    return Manifest.from_json(Path(str(data) + ".json").read_text())


def test_a_manifest_counts_rows_hashes_bytes_and_lands_beside_the_file(files, capsys):
    tmp_path, data = files
    assert run(tmp_path, "manifest", str(data), "--shape", "full", "--produced-at", BOUNDARY) == 0
    assert capsys.readouterr().out == f"{data}.json\n"
    manifest = written(data)
    assert manifest.rows == 2
    assert manifest.content_hash == sha256(data.read_bytes()).hexdigest()
    assert manifest.schema_hash == contract().schema_hash
    assert manifest.produced_at == datetime(2026, 10, 26, 6, tzinfo=UTC)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "orders.contract.json",
        "orders.csv",
        "orders.csv.json",
    ]


def test_the_written_sidecar_is_what_the_receivers_file_drop_accepts(files):
    pytest.importorskip("duckdb")
    from dogana.reference.duckdb import DuckDBStore
    from dogana.reference.files import FileDrop

    tmp_path, data = files
    assert run(tmp_path, "manifest", str(data), "--shape", "full", "--produced-at", BOUNDARY) == 0
    with DuckDBStore(":memory:") as store:
        receipt = FileDrop(store, contract(), columns=COLUMNS).ingest(
            data, now=datetime(2026, 10, 27, tzinfo=UTC)
        )
        assert receipt.accepted, receipt.checks
        assert any(check.name == "content_hash" and check.passed for check in receipt.checks)


def test_an_existing_manifest_is_replaced_only_on_request(files, capsys):
    tmp_path, data = files
    command = ("manifest", str(data), "--shape", "full", "--produced-at", BOUNDARY)
    assert run(tmp_path, *command) == 0
    assert run(tmp_path, *command) == 1
    assert "exists; pass --force" in capsys.readouterr().err
    assert run(tmp_path, *command, "--force") == 0
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]


def test_standard_output_receives_the_manifest_and_no_file_is_written(files, capsys):
    tmp_path, data = files
    assert run(tmp_path, "manifest", str(data), "--shape", "full", "--produced-at", BOUNDARY, "-o", "-") == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "Manifest"
    assert not Path(str(data) + ".json").exists()


def test_windows_are_typed_by_the_window_column_or_derived_from_the_shape(files):
    tmp_path, data = files
    window = ("--window", "2026-10-01", "2026-10-26")
    assert run(tmp_path, "manifest", str(data), "--shape", "daily", "--produced-at", BOUNDARY, *window) == 0
    assert written(data).window == (date(2026, 10, 1), date(2026, 10, 26))
    assert run(tmp_path, "manifest", str(data), "--shape", "daily", "--produced-at", BOUNDARY, "--force") == 0
    assert written(data).window == (date(2026, 9, 27), date(2026, 10, 26))


@pytest.mark.parametrize(
    ("arguments", "status", "message"),
    [
        (("--shape", "full", "--window", "2026-10-01", "2026-10-26"), 2, "does not deliver a window"),
        (("--shape", "weekly"), 1, "admits no shape named 'weekly'"),
        (("--shape", "changes", "--since", "2026-10-25T06:00:00"), 1, "needs a UTC offset"),
        (("--shape", "changes", "--since", "yesterday"), 1, "not an ISO 8601 time"),
        (("--shape", "full", "--rows", "3", "--generation", "g1", "-o", "-", "--force"), 0, ""),
    ],
)
def test_arguments_are_refused_with_a_reason_and_a_status(files, capsys, arguments, status, message):
    tmp_path, data = files
    assert run(tmp_path, "manifest", str(data), "--produced-at", BOUNDARY, *arguments) == status
    assert message in capsys.readouterr().err


def test_a_feed_enumerates_its_keys_from_the_file_and_its_tombstones_from_another(files):
    tmp_path, data = files
    deleted = tmp_path / "deleted.csv"
    deleted.write_text("id\n7\n")
    arguments = ("--shape", "feed", "--since", "g1", "--generation", "g2", "--tombstones", str(deleted))
    assert run(tmp_path, "manifest", str(data), "--produced-at", BOUNDARY, *arguments) == 0
    manifest = written(data)
    assert (manifest.keys, manifest.tombstones, manifest.since, manifest.generation) == (
        ((1,), (2,)),
        ((7,),),
        "g1",
        "g2",
    )


def test_sequence_positions_are_integers(tmp_path):
    c = Contract(
        "events",
        ("id",),
        {"full": Shape.full(), "changes": Shape.changes(checkpoint=Checkpoint.SEQUENCE)},
        types={"id": "int"},
    )
    (tmp_path / "orders.contract.json").write_text(c.to_json())
    data = tmp_path / "events.csv"
    data.write_text("id\n1\n")
    arguments = ("--shape", "changes", "--since", "100", "--generation", "150")
    assert run(tmp_path, "manifest", str(data), "--produced-at", BOUNDARY, *arguments) == 0
    assert (written(data).since, written(data).generation) == (100, 150)


def test_a_file_dogana_cannot_count_needs_its_rows(tmp_path, capsys):
    (tmp_path / "orders.contract.json").write_text(contract().to_json())
    data = tmp_path / "orders.jsonl"
    data.write_text('{"id": 1}\n')
    assert run(tmp_path, "manifest", str(data), "--shape", "full", "--produced-at", BOUNDARY) == 2
    assert "pass --rows" in capsys.readouterr().err
    assert (
        run(tmp_path, "manifest", str(data), "--shape", "full", "--produced-at", BOUNDARY, "--rows", "1") == 0
    )


def test_a_missing_file_is_named(files, capsys):
    tmp_path, _ = files
    missing = tmp_path / "missing.csv"
    assert (
        run(tmp_path, "manifest", str(missing), "--shape", "full", "--produced-at", BOUNDARY, "--rows", "0")
        == 1
    )
    assert f"{missing}: no such file" in capsys.readouterr().err


def test_check_accepts_a_matching_file_and_names_what_changed(files, capsys):
    tmp_path, data = files
    assert run(tmp_path, "manifest", str(data), "--shape", "full", "--produced-at", BOUNDARY) == 0
    capsys.readouterr()
    assert run(tmp_path, "check", str(data)) == 0
    assert capsys.readouterr().out == f"{data}: a valid 'full' delivery for 'orders'\n"
    data.write_text("id,day,value\n1,2026-10-25,x\n")
    assert run(tmp_path, "check", str(data)) == 1
    error = capsys.readouterr().err
    assert "rows: the manifest declares 2" in error and "content_hash: the manifest declares" in error


def test_the_module_and_the_console_script_run_the_same_command():
    module = subprocess.run(
        [sys.executable, "-m", "dogana", "--version"], capture_output=True, text=True, check=True
    )
    script = Path(sys.executable).with_name("dogana")
    assert module.stdout.startswith("dogana ")
    if script.exists():
        assert (
            subprocess.run([script, "--version"], capture_output=True, text=True, check=True).stdout
            == module.stdout
        )


def test_parquet_rows_and_keys_are_read_through_the_duckdb_extra(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    (tmp_path / "orders.contract.json").write_text(contract().to_json())
    data = tmp_path / "orders.parquet"
    duckdb.connect().execute(
        "COPY (SELECT * FROM (VALUES (3, DATE '2026-10-25', 'c'), (4, DATE '2026-10-26', 'd')) "
        f"t(id, day, value)) TO '{data}' (FORMAT parquet)"
    )
    arguments = ("--shape", "feed", "--since", "g1", "--generation", "g2")
    assert run(tmp_path, "manifest", str(data), "--produced-at", BOUNDARY, *arguments) == 0
    assert (written(data).rows, written(data).keys) == (2, ((3,), (4,)))
    assert run(tmp_path, "check", str(data)) == 0


def test_check_compares_only_the_hash_of_a_file_it_cannot_count(tmp_path, capsys):
    (tmp_path / "orders.contract.json").write_text(contract().to_json())
    data = tmp_path / "orders.jsonl"
    data.write_text('{"id": 1}\n')
    assert (
        run(tmp_path, "manifest", str(data), "--shape", "full", "--produced-at", BOUNDARY, "--rows", "1") == 0
    )
    assert run(tmp_path, "check", str(data)) == 0
    assert "rows not compared" in capsys.readouterr().err
