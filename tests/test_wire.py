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


def test_the_core_has_no_adapter_or_third_party_imports():
    import sys

    core = Path(__file__).parents[1] / "src" / "dogana"
    for source in core.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    assert not node.module.startswith("adapters")
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


def test_the_core_runs_in_an_isolated_package_with_every_adapter_removed(tmp_path):
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
shape = Shape('all', 'complete', 'absence', 'none')
c = Contract('stock', ('id',), {'full': shape}, schema=(Column('id', 'int'),))
m = Manifest('stock', 'full', datetime(2026, 10, 18, tzinfo=timezone.utc), rows=0, schema_hash=c.schema_hash)
checks = tuple(Check(r.name, True) for r in Gate.requirements(c, m))
receipt = Gate.evaluate(Ledger(c), m, checks)
assert receipt.accepted
assert receipt.claims.state is State.COMPLETE
assert Ledger.from_json(receipt.ledger.to_json()) == receipt.ledger
assert 'dogana.adapters' not in sys.modules
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
