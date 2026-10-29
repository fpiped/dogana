import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from dogana import Checkpoint, Contract, Gate, Ledger, Manifest, Shape
from dogana.cli import main
from tests.support import part_of, revised, schema_hash

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
        types={"id": "int", "day": "date", "deleted": "bool"},
        schema_hash=schema_hash(COLUMNS),
        deleted_column="deleted",
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


def declared(data):
    return part_of(written(data))


FULL = ("--shape", "full", "--produced-at", BOUNDARY, "--rows", "2")


def test_a_manifest_declares_the_counts_hashes_the_bytes_and_lands_beside_the_file(files, capsys):
    tmp_path, data = files
    assert run(tmp_path, "manifest", str(data), *FULL) == 0
    assert capsys.readouterr().out == f"{data}.json\n"
    manifest = written(data)
    part = manifest.part("orders")
    assert (part.rows, part.tombstones) == (2, 0)
    assert part.content_hash == sha256(data.read_bytes()).hexdigest()
    assert part.schema_hash == contract().schema_hash
    assert manifest.produced_at == datetime(2026, 10, 26, 6, tzinfo=UTC)
    assert manifest.snapshot is None
    assert run(tmp_path, "manifest", str(data), *FULL, "--snapshot", "txn-42", "--force") == 0
    assert written(data).snapshot == "txn-42"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "orders.contract.json",
        "orders.csv",
        "orders.csv.json",
    ]


def test_the_rows_are_declared_by_the_sender_never_counted(files, capsys):
    tmp_path, data = files
    with pytest.raises(SystemExit) as exit:
        run(tmp_path, "manifest", str(data), "--shape", "full", "--produced-at", BOUNDARY)
    assert exit.value.code == 2
    assert "--rows" in capsys.readouterr().err
    data.write_text("not, a, table\n")
    assert run(tmp_path, "manifest", str(data), *FULL) == 0
    assert declared(data).rows == 2


def test_the_written_sidecar_is_what_the_example_file_drop_accepts(files):
    pytest.importorskip("duckdb")
    from example_store import DuckDBStore, FileDrop

    tmp_path, data = files
    assert run(tmp_path, "manifest", str(data), *FULL) == 0
    with DuckDBStore(":memory:") as store:
        receipt = FileDrop(store, contract(), columns=COLUMNS).ingest(
            data, now=datetime(2026, 10, 27, tzinfo=UTC)
        )
        assert receipt.accepted, receipt.checks
        assert any(check.name == "content_hash" and check.passed for check in receipt.checks)


def test_an_existing_manifest_is_replaced_only_on_request(files, capsys):
    tmp_path, data = files
    command = ("manifest", str(data), *FULL)
    assert run(tmp_path, *command) == 0
    assert run(tmp_path, *command) == 1
    assert "exists; pass --force" in capsys.readouterr().err
    assert run(tmp_path, *command, "--force") == 0
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]


def test_standard_output_receives_the_manifest_and_no_file_is_written(files, capsys):
    tmp_path, data = files
    assert run(tmp_path, "manifest", str(data), *FULL, "-o", "-") == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "Manifest"
    assert not Path(str(data) + ".json").exists()


def test_windows_are_typed_by_the_window_column_or_derived_from_the_shape(files):
    tmp_path, data = files
    daily = ("--shape", "daily", "--produced-at", BOUNDARY, "--rows", "2")
    window = ("--window", "2026-10-01", "2026-10-26")
    assert run(tmp_path, "manifest", str(data), *daily, *window) == 0
    assert declared(data).window == (date(2026, 10, 1), date(2026, 10, 26))
    assert run(tmp_path, "manifest", str(data), *daily, "--force") == 0
    assert declared(data).window == (date(2026, 9, 27), date(2026, 10, 26))


@pytest.mark.parametrize(
    ("arguments", "status", "message"),
    [
        (("--shape", "full", "--window", "2026-10-01", "2026-10-26"), 2, "does not deliver a window"),
        (("--shape", "weekly"), 1, "admits no shape named 'weekly'"),
        (("--shape", "changes", "--since", "2026-10-25T06:00:00"), 1, "needs a UTC offset"),
        (("--shape", "changes", "--since", "yesterday"), 1, "not an ISO 8601 time"),
        (("--shape", "feed", "--since", "g1", "--generation", "g2"), 2, "pass --tombstones"),
        (("--shape", "full", "--generation", "g1", "-o", "-", "--force"), 0, ""),
    ],
)
def test_arguments_are_refused_with_a_reason_and_a_status(files, capsys, arguments, status, message):
    tmp_path, data = files
    assert (
        run(tmp_path, "manifest", str(data), "--produced-at", BOUNDARY, "--rows", "2", *arguments) == status
    )
    assert message in capsys.readouterr().err


def test_a_tombstone_shape_declares_its_tombstones(files):
    tmp_path, data = files
    arguments = ("--shape", "feed", "--since", "g1", "--generation", "g2", "--rows", "1", "--tombstones", "2")
    assert run(tmp_path, "manifest", str(data), "--produced-at", BOUNDARY, *arguments) == 0
    part = written(data).part("orders")
    assert (part.rows, part.tombstones, part.since, part.generation) == (1, 2, "g1", "g2")


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
    arguments = ("--shape", "changes", "--since", "100", "--generation", "150", "--rows", "1")
    assert run(tmp_path, "manifest", str(data), "--produced-at", BOUNDARY, *arguments) == 0
    assert (declared(data).since, declared(data).generation) == (100, 150)


def test_a_missing_file_is_named(files, capsys):
    tmp_path, _ = files
    missing = tmp_path / "missing.csv"
    assert run(tmp_path, "manifest", str(missing), *FULL) == 1
    assert f"{missing}: no such file" in capsys.readouterr().err


def test_check_accepts_the_bytes_the_manifest_names_and_refuses_others(files, capsys):
    tmp_path, data = files
    assert run(tmp_path, "manifest", str(data), *FULL) == 0
    capsys.readouterr()
    assert run(tmp_path, "check", str(data)) == 0
    assert capsys.readouterr().out == (
        f"{data}: the manifest fits the contract and names these bytes; the rows are checked on delivery\n"
    )
    data.write_text("id,day,value\n1,2026-10-25,x\n")
    assert run(tmp_path, "check", str(data)) == 1
    assert "content_hash: the manifest declares" in capsys.readouterr().err


def test_check_refuses_a_schema_hash_the_gate_would_refuse(files, capsys):
    tmp_path, data = files
    manifest = contract().manifest(
        "full",
        datetime(2026, 10, 26, 6, tzinfo=UTC),
        rows=2,
        content_hash=sha256(data.read_bytes()).hexdigest(),
    )
    (tmp_path / "orders.csv.json").write_text(revised(manifest, schema_hash="0" * 64).to_json())
    assert run(tmp_path, "check", str(data)) == 1
    assert "manifest column hash differs from the contract's" in capsys.readouterr().err


def test_check_refuses_a_manifest_without_a_hash_and_a_missing_file(files, capsys):
    tmp_path, data = files
    manifest = contract().manifest("full", datetime(2026, 10, 26, 6, tzinfo=UTC), rows=2)
    (tmp_path / "orders.csv.json").write_text(manifest.to_json())
    assert run(tmp_path, "check", str(data)) == 1
    assert "declares no content_hash" in capsys.readouterr().err
    data.unlink()
    assert run(tmp_path, "check", str(data)) == 1
    assert f"{data}: no such file" in capsys.readouterr().err


def test_claims_require_a_window_column_before_parsing_window_bounds(tmp_path, capsys):
    c = contract(shapes={"full": Shape.full()}, window_column=None, types={"id": "int"}, deleted_column=None)
    path = tmp_path / "ledger.json"
    path.write_text(Ledger(c).to_json())
    assert main(["claims", str(path), "--window", "1", "2"]) == 2
    assert "table orders has no window column" in capsys.readouterr().err


def test_full_table_claims_label_all_rows(tmp_path, capsys):
    path = tmp_path / "ledger.json"
    path.write_text(Ledger(contract()).to_json())
    assert main(["claims", str(path), "--at", BOUNDARY]) == 0
    assert capsys.readouterr().out.startswith("orders all rows: unknown;")


@pytest.mark.parametrize(
    "region,span",
    [
        (dict(low=1, high=5, low_open=True, high_open=True), "[2, 4]"),
        (dict(low="a", high="z", low_open=True, high_open=True), "(a, z)"),
    ],
)
def test_claim_labels_close_discrete_integer_bounds_and_preserve_open_string_bounds(region, span):
    from dogana import Region
    from dogana.cli import _span

    assert _span(Region(**region)) == span


def test_window_manifests_without_a_duration_require_explicit_bounds(files, capsys):
    tmp_path, data = files
    c = contract(shapes={"daily": Shape.window()}, types={"id": "int", "day": "date"}, deleted_column=None)
    (tmp_path / "orders.contract.json").write_text(c.to_json())
    assert (
        run(tmp_path, "manifest", str(data), "--shape", "daily", "--produced-at", BOUNDARY, "--rows", "2")
        == 2
    )
    assert "needs --window LOW HIGH" in capsys.readouterr().err


def test_claims_prints_each_region_and_the_instant_the_table_is_exact_at(tmp_path, capsys):
    c = contract()
    manifest = c.manifest(
        "daily", datetime(2026, 10, 26, 6, tzinfo=UTC), window=(date(2026, 9, 27), date(2026, 10, 26)), rows=2
    )
    checks = ()
    receipt = Gate.evaluate(Ledger(c), manifest, checks, now=datetime(2026, 10, 26, 7, tzinfo=UTC))
    path = tmp_path / "orders.ledger.json"
    path.write_text(Ledger(c).follow(receipt).to_json())
    assert (
        main(["claims", str(path), "--at", "2026-10-26T08:00:00Z", "--window", "2026-10-01", "2026-10-02"])
        == 0
    )
    assert capsys.readouterr().out == (
        "orders [2026-10-01, 2026-10-02]: complete; complete as of 2026-10-26T06:00:00+00:00; "
        "deletions as of 2026-10-26T06:00:00+00:00\n"
        "orders: complete; exact as of 2026-10-26T06:00:00+00:00\n"
    )
    assert main(["claims", str(path), "--at", "2026-10-26T08:00:00Z"]) == 0
    assert capsys.readouterr().out == (
        "orders (-inf, 2026-09-26]: unknown; complete as of unknown; deletions as of unknown\n"
        "orders [2026-09-27, 2026-10-26]: complete; complete as of 2026-10-26T06:00:00+00:00; "
        "deletions as of 2026-10-26T06:00:00+00:00\n"
        "orders [2026-10-27, +inf): unknown; complete as of unknown; deletions as of unknown\n"
        "orders: unknown; exact at no single instant\n"
    )
    window = ["--window", "2026-10-01", "2026-10-02"]
    assert main(["claims", str(path), "--at", "2026-10-28T06:00:00Z", "--limit-days", "2", *window]) == 0
    assert capsys.readouterr().out.endswith("orders: complete; exact as of 2026-10-26T06:00:00+00:00\n")
    assert main(["claims", str(path), "--at", "2026-10-28T06:00:01Z", "--limit-days", "2", *window]) == 0
    assert capsys.readouterr().out.endswith("orders: expired; exact as of 2026-10-26T06:00:00+00:00\n")
    for days in ("-1", "-1e-12", "inf", "nan", "1e308"):
        # Joined with "=", so that argparse on every supported Python reads "-1e-12" as the value.
        assert main(["claims", str(path), f"--limit-days={days}"]) == 2
    assert main(["claims", str(path), "--at", "2030-01-01T00:00:00Z", *window]) == 0
    assert capsys.readouterr().out.endswith("orders: complete; exact as of 2026-10-26T06:00:00+00:00\n")


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


def test_a_written_manifest_is_readable_by_others_and_never_replaces_one_silently(files):
    tmp_path, data = files
    assert run(tmp_path, "manifest", str(data), *FULL) == 0
    sidecar = tmp_path / "orders.csv.json"
    assert sidecar.stat().st_mode & 0o044 == 0o044
    assert run(tmp_path, "manifest", str(data), *FULL) == 1
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
