import ast
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from dogana import Contract, InvalidContract, Ledger, Manifest, Receipt, SerializationError, Shape
from tests.support import COLUMNS, SINCE, at
from tests.support import accounting_contract as contract

EXAMPLES = Path(__file__).parents[1] / "docs" / "examples"


@pytest.mark.protocol("versioned-json.examples")
def test_contracts_manifests_and_ledgers_round_trip_without_losing_bound_types():
    for window in [(1, 30), ("IT01", "IT01"), (at(18).date(), at(19).date()), (at(18), at(19))]:
        c = contract(window_type=type(window[0]).__name__)
        m = Manifest(c.table, "daily", at(20), window=window, rows=2, schema_hash=c.schema_hash)
        ledger = Ledger(c).apply(m)
        for value in (c, m, ledger):
            assert type(value).from_dict(value.to_dict()) == value
            assert type(value).from_json(value.to_json()) == value
            assert json.loads(value.to_json())["version"] == 4


@pytest.mark.protocol("versioned-json.1", "versioned-json.6")
@pytest.mark.parametrize(
    "change",
    [
        {"version": 1},
        {"version": True},
        {"key": ("missing",)},
    ],
)
def test_unsupported_or_invalid_wire_contracts_are_refused(change):
    value = contract(columns=COLUMNS).to_dict()
    value.update(change)
    with pytest.raises(SerializationError):
        Contract.from_dict(value)


@pytest.mark.protocol("versioned-json.1", "versioned-json.6")
@pytest.mark.parametrize(
    "name,cls", [("contract", Contract), ("manifest", Manifest), ("ledger", Ledger), ("receipt", Receipt)]
)
def test_every_version_3_document_is_refused_naming_both_versions(name, cls):
    value = json.loads((EXAMPLES / f"{name}.json").read_text()) | {"version": 3}
    if name == "contract":
        value["max_shrink_fraction"] = 0.5
    with pytest.raises(SerializationError, match="unsupported wire version 3: this dogana reads version 4"):
        cls.from_dict(value)


@pytest.mark.protocol("versioned-json.3")
@pytest.mark.parametrize("seconds", [1e-7, -1e-7, -5, 1.5e-6, 1e100, float("inf"), float("nan")])
def test_a_duration_that_would_round_or_is_negative_is_refused_not_rounded(seconds):
    raw = replace(SINCE, lookback=timedelta(minutes=5)).to_dict()
    raw["lookback"] = {"$seconds": seconds}
    with pytest.raises(SerializationError, match="duration"):
        Shape.from_dict(raw)
    assert Shape.from_dict(raw | {"lookback": {"$seconds": 1.5}}).lookback == timedelta(seconds=1.5)


@pytest.mark.protocol("versioned-json.3")
@pytest.mark.parametrize(
    "text",
    [
        "2026-10-18T00:00:00.0000009+00:00",
        "2026-10-18T00:00:00",
        "2026-10-18 00:00:00+00:00",
        "2026-10-18T00:00:00+0000",
        "20261018T000000Z",
    ],
)
def test_a_datetime_outside_the_wire_grammar_is_refused_not_truncated(text):
    raw = Manifest("stock", "full", at(18)).to_dict()
    with pytest.raises(SerializationError, match="datetime tag"):
        Manifest.from_dict(raw | {"produced_at": {"$datetime": text}})
    zulu = Manifest.from_dict(raw | {"produced_at": {"$datetime": "2026-10-18T00:00:00Z"}})
    assert zulu.produced_at == at(18)


@pytest.mark.protocol("versioned-json.3")
@pytest.mark.parametrize("text", ["2026-10-18T00:00:00", "20261018", "2026-W42-1"])
def test_a_date_tag_with_more_than_a_date_is_refused(text):
    raw = Manifest("stock", "daily", at(18), window=(at(18).date(), at(18).date())).to_dict()
    raw["window"] = [{"$date": text}, {"$date": "2026-10-18"}]
    with pytest.raises(SerializationError, match="date tag"):
        Manifest.from_dict(raw)


@pytest.mark.protocol("checks.receipt")
@pytest.mark.parametrize("field", ["ledger_identity", "publication_id"])
def test_a_receipt_never_carries_an_empty_identity(field):
    receipt = Receipt.from_json((EXAMPLES / "receipt.json").read_text())
    with pytest.raises(InvalidContract, match="never empty"):
        replace(receipt, **{field: ""})


@pytest.mark.protocol("versioned-json.2")
def test_a_manifest_cannot_be_decoded_as_a_contract():
    with pytest.raises(SerializationError):
        Contract.from_json(Manifest("stock", "full", at(18)).to_json())


ENTRY_POINTS = {"cli.py", "__main__.py"}


@pytest.mark.protocol("boundaries.package")
def test_the_core_imports_only_the_standard_library():
    import sys

    core = Path(__file__).parents[1] / "src" / "dogana"
    for source in core.glob("*.py"):
        if source.name in ENTRY_POINTS:
            continue
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                names = [node.module.split(".")[0]]
            else:
                continue
            assert set(names) <= sys.stdlib_module_names


def test_the_command_line_sits_above_the_core_and_imports_no_third_party_package():
    import subprocess
    import sys

    core = Path(__file__).parents[1] / "src" / "dogana"
    for source in core.glob("*.py"):
        if source.name in ENTRY_POINTS:
            continue
        imported = {
            node.module
            for node in ast.walk(ast.parse(source.read_text()))
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not imported & {"cli", "dogana.cli"}, source.name
    third_party = "[m for m in sys.modules if m.split('.')[0] in ('duckdb', 'pyarrow')]"
    loaded = subprocess.run(
        [sys.executable, "-c", f"import sys, dogana.cli; print({third_party})"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout == "[]\n"


@pytest.mark.protocol("boundaries.package")
def test_the_package_reads_no_data_file():
    package = Path(__file__).parents[1] / "src" / "dogana"
    readers = {"csv", "duckdb", "pyarrow", "pandas", "polars", "sqlite3", "example_store"}
    for source in package.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module is not None and not node.level:
                names = {node.module.split(".")[0]}
            else:
                continue
            assert not names & readers, source.name


def test_the_example_store_pins_the_core_it_is_tested_with():
    import tomllib

    root = Path(__file__).parents[1]
    core = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    example = tomllib.loads((root / "examples" / "duckdb" / "pyproject.toml").read_text())["project"]
    assert f"dogana=={core['version']}" in example["dependencies"]
    assert example["version"] == core["version"]


def test_shape_names_that_match_wire_field_names_still_round_trip_as_mapping_keys():
    from tests.support import FULL

    c = contract(shapes={"kind": FULL, "version": FULL})
    assert Contract.from_json(c.to_json()) == c


def test_utc_normalization_refuses_an_offset_that_underflows_the_datetime_range():
    from datetime import datetime, timedelta, timezone

    from dogana.wire import utc

    with pytest.raises(InvalidContract, match="timestamp is outside the UTC datetime range"):
        utc(datetime.min.replace(tzinfo=timezone(timedelta(hours=1))))


@pytest.mark.parametrize(
    "value,message", [((), "unsupported JSON value: tuple"), ({1: "a"}, "mapping keys must be strings")]
)
def test_canonical_json_refuses_values_outside_its_json_domain(value, message):
    from dogana.wire import canonical_json

    with pytest.raises(SerializationError, match=message):
        canonical_json(value)


def test_canonical_json_refuses_recursive_containers():
    from dogana.wire import canonical_json

    value = []
    value.append(value)
    with pytest.raises(SerializationError, match="JSON containers are recursive or too deeply nested"):
        canonical_json(value)


@pytest.mark.parametrize(
    "value,message",
    [
        ({1: "a"}, "mapping keys must be strings"),
        (float("inf"), "numbers must be finite"),
        (float("nan"), "numbers must be finite"),
        (object(), "unsupported value type: object"),
    ],
)
def test_wire_encoding_refuses_invalid_mapping_keys_numbers_and_objects(value, message):
    from dogana.wire import _encode

    with pytest.raises(SerializationError, match=message):
        _encode(value, identity=False)


def test_wire_decoding_refuses_non_string_python_mapping_keys():
    with pytest.raises(SerializationError, match="mapping keys must be strings"):
        Contract.from_dict({1: "a"})


@pytest.mark.protocol("versioned-json.2")
@pytest.mark.parametrize("change", [{"table": 1}, {"generation": True}, {"schema_hash": 4}, {"rows": True}])
def test_manifest_wire_types_are_checked_before_any_accounting(change):
    value = Manifest("stock", "full", at(18)).to_dict()
    value.update(change)
    with pytest.raises(SerializationError):
        Manifest.from_dict(value)


@pytest.mark.protocol("boundaries.package")
def test_the_core_runs_alone_from_a_copy_of_its_modules(tmp_path):
    import subprocess
    import sys

    source = Path(__file__).parents[1] / "src" / "dogana"
    isolated = tmp_path / "dogana"
    isolated.mkdir()
    for module in source.glob("*.py"):
        (isolated / module.name).write_text(module.read_text())
    program = """
import sys
sys.path.insert(0, sys.argv[1])
from datetime import datetime, timedelta, timezone
from dogana import Contract, Shape, Manifest, Ledger, Gate, Check, State
shape = Shape(coverage='all', completeness='complete', deletions='absence', checkpoint='none')
c = Contract('stock', ('id',), {'full': shape}, types={'id': 'int'})
m = Manifest('stock', 'full', datetime(2026, 10, 18, tzinfo=timezone.utc), rows=0, schema_hash=c.schema_hash)
checks = tuple(Check(r.name, True) for r in Gate.requirements(c, m))
receipt = Gate.evaluate(Ledger(c), m, checks, now=m.produced_at)
assert receipt.accepted
ledger = Ledger(c).follow(receipt)
assert ledger.claims(receipt.received_at, limit=timedelta.max).state is State.COMPLETE
assert Ledger.from_json(ledger.to_json()) == ledger
"""
    subprocess.run([sys.executable, "-B", "-S", "-c", program, str(tmp_path)], check=True, cwd=tmp_path)


@pytest.mark.protocol("versioned-json.2")
@pytest.mark.parametrize("change", [{"keys": "12"}, {"tombstones": "12"}])
def test_strings_cannot_be_normalized_into_lists_of_named_keys(change):
    raw = Manifest("stock", "full", at(18)).to_dict()
    raw.update(change)
    with pytest.raises(SerializationError):
        Manifest.from_dict(raw)


@pytest.mark.protocol("versioned-json.2")
def test_arrays_cannot_be_normalized_into_shape_mappings():
    raw = contract().to_dict()
    raw["shapes"] = list(raw["shapes"].items())
    with pytest.raises(SerializationError):
        Contract.from_dict(raw)


@pytest.mark.parametrize("name", ["$date", "$datetime", "$seconds"])
def test_scalar_tag_names_remain_valid_shape_names(name):
    from tests.support import FULL

    c = contract(shapes={name: FULL})
    assert Contract.from_json(c.to_json()) == c


@pytest.mark.protocol("versioned-json.examples")
def test_every_public_wire_value_is_hashable_and_round_trips():
    from dogana import (
        Action,
        ApplyPlan,
        Check,
        CheckRequirement,
        ExtractionRequest,
        Gate,
        Receipt,
        Region,
        RegionClaim,
        Requirement,
        Shape,
        Transition,
    )
    from tests.support import FULL

    c = contract()
    m = Manifest(c.table, "full", at(18))
    checks = tuple(Check(r.name, True) for r in Gate.requirements(c, m))
    receipt = Gate.evaluate(Ledger(c), m, checks, now=at(18))
    ledger = Ledger(c).follow(receipt)
    claims = ledger.claims(receipt.received_at, limit=timedelta.max)
    values = (
        FULL,
        c,
        m,
        Region(),
        ledger,
        claims.regions[0],
        claims,
        receipt.transition.plan.actions[0],
        receipt.transition.plan,
        CheckRequirement("row_count"),
        checks[0],
        receipt.transition,
        receipt,
        Requirement(c.table, at(19)),
        ExtractionRequest(c.table, "full", at(19)),
    )
    assert {type(value) for value in values} == {
        Shape,
        Contract,
        Manifest,
        Region,
        Ledger,
        RegionClaim,
        type(claims),
        Action,
        ApplyPlan,
        CheckRequirement,
        Check,
        Transition,
        Receipt,
        Requirement,
        ExtractionRequest,
    }
    for value in values:
        decoded = type(value).from_json(value.to_json())
        assert decoded == value
        assert hash(decoded) == hash(value)
        assert decoded.identity == value.identity


@pytest.mark.protocol("versioned-json.2")
def test_subclasses_cannot_replace_a_registered_wire_kind():
    from dogana import Shape as ProtocolShape
    from dogana.wire import Wire
    from tests.support import FULL

    class Shape(ProtocolShape):
        pass

    class PrivateValue(Wire):
        pass

    assert type(ProtocolShape.from_json(FULL.to_json())) is ProtocolShape
    for kind in ("PrivateValue", "Wire"):
        with pytest.raises(SerializationError, match="unknown wire kind"):
            Wire.from_dict({"version": 4, "kind": kind})
    with pytest.raises(SerializationError, match="unknown wire kind"):
        Shape(coverage="all", completeness="complete", deletions="absence", checkpoint="none").to_dict()


@pytest.mark.protocol("versioned-json.4")
def test_numeric_spellings_and_default_values_do_not_fork_identity():
    from dataclasses import replace

    from dogana import Shape

    integer = Shape(coverage="all", completeness="complete", deletions="absence", checkpoint="none", cost=2)
    decimal = replace(integer, cost=2.0)
    assert integer == decimal
    assert hash(integer) == hash(decimal)
    assert integer.to_json() == decimal.to_json()
    assert integer.identity_json() == decimal.identity_json()
    assert integer.identity_dict()["cost"] == 2
    assert "cost" not in replace(integer, cost=1).identity_dict()
    c = Contract("stock", ("id",), {"full": integer}, types={"id": "int"})
    assert "start_at" not in c.identity_dict()


@pytest.mark.protocol("versioned-json.4")
def test_identity_excludes_versions_recursively(monkeypatch):
    from dogana.wire import Wire

    value = Ledger(contract())
    identity = value.identity
    monkeypatch.setattr(Wire, "version", 4)
    assert value._document(identity=False)["version"] == 4
    assert value._document(identity=False)["contract"]["version"] == 4
    assert value.identity == identity


@pytest.mark.protocol("versioned-json.4")
def test_timestamp_offsets_do_not_change_manifest_identity():
    from dataclasses import replace
    from datetime import timedelta, timezone

    m = Manifest("stock", "feed", at(18), tombstones=3)
    other = replace(m, produced_at=at(18).astimezone(timezone(timedelta(hours=2))))
    assert other.identity == m.identity
    assert m.identity_dict()["tombstones"] == 3
    assert m.identity_dict()["produced_at"] == {"$datetime": "2026-10-18T00:00:00+00:00"}


@pytest.mark.protocol("versioned-json.4")
def test_replay_outcome_preserves_the_original_receipt_identity_and_clock():
    from dataclasses import replace

    from dogana import Check, Gate, Receipt

    c = contract()
    m = Manifest(c.table, "full", at(18))
    checks = tuple(Check(r.name, True) for r in Gate.requirements(c, m))
    original = Gate.evaluate(Ledger(c), m, checks, now=at(19), source="stock.csv")
    replay = replace(original, outcome="replayed")
    assert replay.identity == original.identity
    assert replay.received_at == original.received_at
    assert replay.to_dict()["outcome"] == "replayed"
    assert replay.identity_dict()["outcome"] == "accepted"
    assert "accepted" not in replay.to_dict()
    assert "replayed" not in replay.to_dict()
    assert Receipt.from_json(replay.to_json()) == replay
    assert replace(original, source="corrected.csv").identity != original.identity
    assert replace(original, received_at=at(20)).identity != original.identity


@pytest.mark.protocol("versioned-json.2")
def test_removed_history_fields_are_refused_on_the_wire():
    from dogana import Region

    for value, field, extra in ((Ledger(contract()), "accepted", []), (Region(), "generation", None)):
        raw = value.to_dict()
        raw[field] = extra
        with pytest.raises(SerializationError, match="unknown fields"):
            type(value).from_dict(raw)


@pytest.mark.protocol("versioned-json.2")
def test_new_default_fields_remain_required_on_the_wire():
    from dogana import Shape

    for value, field in (
        (contract(), "cursor_column"),
        (Manifest("stock", "full", at(18)), "content_hash"),
        (Shape(coverage="all", completeness="complete", deletions="absence", checkpoint="none"), "lookback"),
    ):
        raw = value.to_dict()
        del raw[field]
        with pytest.raises(SerializationError, match=f"missing fields.*{field}"):
            type(value).from_dict(raw)


@pytest.mark.parametrize(
    "field,value",
    [("coverage", "sometimes"), ("completeness", "partial"), ("deletions", "delete"), ("checkpoint", "date")],
)
def test_invalid_vocabulary_names_the_property_and_value(field, value):
    import re

    from dogana import InvalidContract, Shape

    arguments = dict(coverage="all", completeness="complete", deletions="absence", checkpoint="none")
    original = Shape(**arguments).to_dict()
    arguments[field] = value
    message = re.escape(f"Shape.{field}={value!r}")
    with pytest.raises(InvalidContract, match=message):
        Shape(**arguments)
    original[field] = value
    with pytest.raises(SerializationError, match=message):
        Shape.from_dict(original)


@pytest.mark.protocol("versioned-json.5")
@pytest.mark.parametrize("value", [10**400, 2**53 + 1, -(2**53 + 1)])
def test_integer_identity_rejects_lossy_or_overflowing_binary64_values(value):
    from dogana.wire import canonical_json

    with pytest.raises(SerializationError, match="binary64"):
        canonical_json(value)


@pytest.mark.protocol("versioned-json.5")
def test_wire_json_preserves_integer_values_while_identity_uses_ecmascript_numbers():
    value = Manifest("stock", "daily", at(18), window=(0, 2**68))
    assert value.to_dict()["window"] == [0, 2**68]
    assert Manifest.from_json(value.to_json()) == value
    assert value.identity_dict()["window"] == [0, 2**68]
    assert '"window":[0,295147905179352830000]' in value.identity_json()


@pytest.mark.parametrize("name", ["kind", "version"])
def test_type_mapping_column_names_cannot_be_mistaken_for_wire_metadata(name):
    from dogana import Shape

    c = Contract("events", (name,), {"full": Shape.full()}, types={name: "str"})
    assert Contract.from_json(c.to_json()) == c


@pytest.mark.protocol("versioned-json.2")
def test_removed_schema_and_column_wire_kind_are_refused():
    import dogana
    from dogana.wire import Wire

    assert not hasattr(dogana, "Column")
    with pytest.raises(SerializationError, match="unknown fields"):
        Contract.from_dict(contract().to_dict() | {"schema": []})
    with pytest.raises(SerializationError, match="unknown wire kind"):
        Wire.from_dict({"kind": "Column", "version": 4, "name": "id", "type": "int"})
