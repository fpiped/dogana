import ast
import json
from functools import partial
from pathlib import Path

import pytest

from dogana import Contract, Ledger, Manifest, SerializationError
from tests.support import SCHEMA, at, contract

# Accounting-only contracts: these tests mix int, str, date and datetime window domains on one table.
contract = partial(contract, schema=())


def test_contracts_manifests_and_ledgers_round_trip_without_losing_bound_types():
    c = contract()
    for window in [(1, 30), ("IT01", "IT01"), (at(18).date(), at(19).date()), (at(18), at(19))]:
        m = Manifest(c.table, "daily", at(20), window=window, rows=2, schema_hash=c.schema_hash)
        ledger = Ledger(c).apply(m)
        for value in (c, m, ledger):
            assert type(value).from_dict(value.to_dict()) == value
            assert type(value).from_json(value.to_json()) == value
            assert json.loads(value.to_json())["version"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"version": True},
        {"kind": "Unknown"},
        {"unknown": 1},
        {"key": ("missing",)},
    ],
)
def test_unsupported_or_invalid_wire_contracts_are_refused(change):
    value = contract(schema=SCHEMA).to_dict()
    value.update(change)
    with pytest.raises(SerializationError):
        Contract.from_dict(value)


def test_a_manifest_cannot_be_decoded_as_a_contract():
    with pytest.raises(SerializationError):
        Contract.from_json(Manifest("stock", "full", at(18)).to_json())


def test_the_core_has_no_reference_or_third_party_imports():
    import sys

    core = Path(__file__).parents[1] / "src" / "dogana"
    for source in core.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    assert not node.module.startswith("reference")
                    continue
                names = [node.module.split(".")[0]]
            else:
                continue
            assert set(names) <= sys.stdlib_module_names


def test_shape_names_that_match_wire_field_names_still_round_trip_as_mapping_keys():
    from tests.support import FULL

    c = contract(shapes={"kind": FULL, "version": FULL})
    assert Contract.from_json(c.to_json()) == c


@pytest.mark.parametrize("change", [{"table": 1}, {"generation": 3}, {"schema_hash": 4}, {"rows": True}])
def test_manifest_wire_types_are_checked_before_any_accounting(change):
    value = Manifest("stock", "full", at(18)).to_dict()
    value.update(change)
    with pytest.raises(SerializationError):
        Manifest.from_dict(value)


def test_the_core_runs_in_an_isolated_package_with_reference_package_removed(tmp_path):
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
from datetime import datetime, timezone
from dogana import Column, Contract, Shape, Manifest, Ledger, Gate, Check, State
shape = Shape(coverage='all', completeness='complete', deletions='absence', checkpoint='none')
c = Contract('stock', ('id',), {'full': shape}, schema=(Column('id', 'int'),))
m = Manifest('stock', 'full', datetime(2026, 10, 18, tzinfo=timezone.utc), rows=0, schema_hash=c.schema_hash)
checks = tuple(Check(r.name, True) for r in Gate.requirements(c, m))
receipt = Gate.evaluate(Ledger(c), m, checks, now=m.produced_at)
assert receipt.accepted
assert receipt.claims.state is State.COMPLETE
assert Ledger.from_json(receipt.ledger.to_json()) == receipt.ledger
assert 'dogana.reference' not in sys.modules
"""
    subprocess.run([sys.executable, "-B", "-S", "-c", program, str(tmp_path)], check=True, cwd=tmp_path)


@pytest.mark.parametrize("change", [{"keys": "12"}, {"tombstones": "12"}])
def test_strings_cannot_be_normalized_into_lists_of_named_keys(change):
    raw = Manifest("stock", "full", at(18)).to_dict()
    raw.update(change)
    with pytest.raises(SerializationError):
        Manifest.from_dict(raw)


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


def test_every_public_wire_value_is_hashable_and_round_trips():
    from dogana import (
        Action,
        ApplyPlan,
        Check,
        CheckRequirement,
        Column,
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
    values = (
        Column("id", "int"),
        FULL,
        c,
        m,
        Region(),
        receipt.ledger,
        receipt.claims.regions[0],
        receipt.claims,
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
        Column,
        Shape,
        Contract,
        Manifest,
        Region,
        Ledger,
        RegionClaim,
        type(receipt.claims),
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
            Wire.from_dict({"version": 1, "kind": kind})
    with pytest.raises(SerializationError, match="unknown wire kind"):
        Shape(coverage="all", completeness="complete", deletions="absence", checkpoint="none").to_dict()


def test_numeric_spellings_and_default_values_do_not_fork_identity():
    from dataclasses import replace
    from datetime import timedelta

    from dogana import Shape

    integer = Shape(coverage="all", completeness="complete", deletions="absence", checkpoint="none", cost=2)
    decimal = replace(integer, cost=2.0)
    assert integer == decimal
    assert hash(integer) == hash(decimal)
    assert integer.to_json() == decimal.to_json()
    assert integer.identity_json() == decimal.identity_json()
    assert integer.identity_dict()["cost"] == 2
    assert "cost" not in replace(integer, cost=1).identity_dict()
    c = Contract("stock", ("id",), {"full": integer})
    assert "freshness" not in c.identity_dict()
    assert "max_shrink_fraction" not in c.identity_dict()
    assert replace(c, max_shrink_fraction=None).identity_dict()["max_shrink_fraction"] is None
    assert replace(c, freshness=timedelta(seconds=2)).identity_dict()["freshness"] == {"$seconds": 2}


def test_identity_excludes_versions_recursively(monkeypatch):
    from dogana.wire import Wire

    value = Ledger(contract())
    identity = value.identity
    monkeypatch.setattr(Wire, "version", 2)
    assert value._document(identity=False)["version"] == 2
    assert value._document(identity=False)["contract"]["version"] == 2
    assert value.identity == identity


def test_key_set_order_and_timestamp_offsets_do_not_change_manifest_identity():
    from dataclasses import replace
    from datetime import timedelta, timezone

    m = Manifest("stock", "feed", at(18), keys=((2,), (10,)), tombstones=((3,), (1,)))
    other = replace(
        m,
        keys=tuple(reversed(m.keys)),
        tombstones=tuple(reversed(m.tombstones)),
        produced_at=at(18).astimezone(timezone(timedelta(hours=2))),
    )
    assert other.identity == m.identity
    assert m.identity_dict()["keys"] == [[10], [2]]
    assert m.identity_dict()["tombstones"] == [[1], [3]]
    assert m.identity_dict()["produced_at"] == {"$datetime": "2026-10-18T00:00:00+00:00"}


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


def test_removed_history_fields_are_refused_on_the_wire():
    from dogana import Region

    for value, field, extra in ((Ledger(contract()), "accepted", []), (Region(), "generation", None)):
        raw = value.to_dict()
        raw[field] = extra
        with pytest.raises(SerializationError, match="unknown fields"):
            type(value).from_dict(raw)


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


@pytest.mark.parametrize("value", [10**400, 2**53 + 1, -(2**53 + 1)])
def test_integer_identity_rejects_lossy_or_overflowing_binary64_values(value):
    from dogana.wire import canonical_json

    with pytest.raises(SerializationError, match="binary64"):
        canonical_json(value)


def test_wire_json_preserves_integer_values_while_identity_uses_ecmascript_numbers():
    value = Manifest("stock", "full", at(18), keys=((2**68,),))
    assert value.to_dict()["keys"] == [[2**68]]
    assert Manifest.from_json(value.to_json()) == value
    assert value.identity_dict()["keys"] == [[2**68]]
    assert '"keys":[[295147905179352830000]]' in value.identity_json()
