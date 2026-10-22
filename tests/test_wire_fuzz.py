import copy
import json
import random
from pathlib import Path

import pytest

from dogana import Contract, InvalidContract, Ledger, Manifest, Receipt

EXAMPLES = Path(__file__).parents[1] / "docs" / "examples"


def objects(value, path=()):
    if isinstance(value, dict):
        if value.get("version") == 1 and "kind" in value:
            yield path
        for key, item in value.items():
            yield from objects(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from objects(item, (*path, index))


def test_seeded_mutations_of_wire_examples_fail_only_with_public_contract_errors():
    rng = random.Random(1729)
    for name, cls in (
        ("contract", Contract),
        ("manifest", Manifest),
        ("ledger", Ledger),
        ("receipt", Receipt),
    ):
        original = json.loads((EXAMPLES / f"{name}.json").read_text())
        paths = list(objects(original))
        for _ in range(80):
            value = copy.deepcopy(original)
            node = value
            for key in rng.choice(paths):
                node = node[key]
            mutation = rng.choice(("drop", "retype", "kind", "version"))
            field = rng.choice([k for k in node if k not in ("version", "kind")])
            if mutation == "drop":
                del node[field]
            elif mutation == "retype":
                node[field] = {"wrong": []}
            elif mutation == "kind":
                node["kind"] = "unknown"
            else:
                node["version"] = rng.choice((0, 2, True, "1"))
            with pytest.raises(InvalidContract):
                cls.from_json(json.dumps(value))


@pytest.mark.parametrize("seconds", [float("inf"), float("-inf"), 10**100, 1e100])
def test_overflowing_wire_durations_use_the_public_serialization_error(seconds):
    raw = Contract.from_json((EXAMPLES / "contract.json").read_text()).to_dict()
    raw["freshness"] = {"$seconds": seconds}
    with pytest.raises(InvalidContract):
        Contract.from_json(json.dumps(raw))


@pytest.mark.parametrize("name,cls", [("manifest", Manifest), ("ledger", Ledger), ("receipt", Receipt)])
def test_naive_nested_wire_timestamps_never_escape_as_a_runtime_error(name, cls):
    raw = (EXAMPLES / f"{name}.json").read_text().replace("+00:00", "")
    with pytest.raises(InvalidContract):
        cls.from_json(raw)
