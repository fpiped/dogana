import copy
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dogana import Contract, InvalidContract, Ledger, Manifest, Receipt

EXAMPLES = Path(__file__).parents[1] / "docs" / "examples"


def objects(value, path=()):
    if isinstance(value, dict):
        if value.get("version") == 3 and "kind" in value:
            yield path
        for key, item in value.items():
            yield from objects(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from objects(item, (*path, index))


@pytest.mark.parametrize(
    "name,cls",
    [
        ("contract", Contract),
        ("manifest", Manifest),
        ("ledger", Ledger),
        ("receipt", Receipt),
    ],
)
@given(st.data())
def test_mutations_of_nested_wire_values_raise_public_contract_errors(name, cls, data):
    original = json.loads((EXAMPLES / f"{name}.json").read_text())
    value = copy.deepcopy(original)
    node = value
    for key in data.draw(st.sampled_from(list(objects(original))), label="path"):
        node = node[key]
    mutation = data.draw(st.sampled_from(("drop", "retype", "kind", "version")), label="mutation")
    field = data.draw(st.sampled_from([k for k in node if k not in ("version", "kind")]), label="field")
    if mutation == "drop":
        del node[field]
    elif mutation == "retype":
        node[field] = {"wrong": []}
    elif mutation == "kind":
        node["kind"] = "unknown"
    else:
        node["version"] = data.draw(st.sampled_from((0, 1, 2, True, "3")), label="version")
    with pytest.raises(InvalidContract):
        cls.from_json(json.dumps(value))


@given(st.floats(allow_nan=False, allow_infinity=False, width=64))
def test_canonical_numbers_round_trip_as_binary64(value):
    from dogana.wire import canonical_json

    encoded = canonical_json(value)
    assert float(encoded) == value
    assert canonical_json(float(encoded)) == encoded


@given(st.text(alphabet=st.characters(codec="utf-8")))
def test_canonical_unicode_preserves_scalars_without_normalization(value):
    from dogana.wire import canonical_json

    encoded = canonical_json(value)
    assert json.loads(encoded) == value
    assert encoded.encode("utf-8").decode("utf-8") == encoded


@given(st.integers(min_value=0xD800, max_value=0xDFFF))
def test_surrogates_in_values_and_property_names_are_refused(codepoint):
    from dogana import SerializationError
    from dogana.wire import canonical_json
    from tests.support import at

    text = chr(codepoint)
    for value in (text, {text: None}, {"nested": [text]}):
        with pytest.raises(SerializationError, match="Unicode scalar"):
            canonical_json(value)
    with pytest.raises(InvalidContract, match="Unicode scalar"):
        Manifest("stock", text, at(18))


@given(st.integers(min_value=2**53 + 1, max_value=2**64 - 1).filter(lambda n: float(n) != n))
def test_lossy_integer_counts_and_bounds_are_refused(value):
    from dogana.wire import canonical_json
    from tests.support import at

    with pytest.raises(InvalidContract, match="binary64"):
        canonical_json(value)
    for change in ({"rows": value}, {"tombstones": value}, {"window": (0, value)}):
        with pytest.raises(InvalidContract, match="binary64"):
            Manifest("stock", "full", at(18), **change)
        raw = Manifest("stock", "full", at(18)).to_dict()
        raw.update(json.loads(json.dumps(change)))
        with pytest.raises(InvalidContract, match="binary64"):
            Manifest.from_dict(raw)


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


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_numbers_are_refused(constant):
    from dogana import SerializationError

    with pytest.raises(SerializationError, match="invalid JSON number"):
        Manifest.from_json('{"rows":' + constant + "}")


def test_duplicate_json_fields_cannot_silently_replace_an_earlier_claim():
    from dogana import SerializationError

    raw = (EXAMPLES / "manifest.json").read_text()
    with pytest.raises(SerializationError, match="duplicate JSON key: rows"):
        Manifest.from_json(raw.replace('"rows":2', '"rows":1,"rows":2'))


def test_excessively_nested_json_raises_a_serialization_error():
    from dogana import SerializationError

    with pytest.raises(SerializationError):
        Manifest.from_json("[" * 2000 + "0" + "]" * 2000)


def test_recursive_python_containers_cannot_escape_wire_validation():
    from dogana import SerializationError
    from tests.support import at

    raw = Manifest("stock", "full", at(18)).to_dict()
    raw["keys"] = [raw]
    with pytest.raises(SerializationError):
        Manifest.from_dict(raw)
