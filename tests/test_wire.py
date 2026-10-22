import ast
import json
from pathlib import Path

import pytest

from dogana import Contract, Ledger, Manifest, SerializationError
from tests.support import at, contract


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
    value = contract().to_dict()
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
