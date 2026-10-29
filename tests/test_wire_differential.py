import copy
import json
from datetime import date, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path

import pytest
from hypothesis import Phase, given, settings
from hypothesis import strategies as st
from hypothesis_jsonschema import from_schema
from jsonschema import ValidationError

from dogana import (
    ExtractionRequest,
    Gate,
    InvalidContract,
    Ledger,
    Manifest,
    Region,
    Requirement,
    SerializationError,
    Shape,
)
from dogana.wire import Wire
from tests.support import accounting_contract as contract
from tests.support import at

DOCS = Path(__file__).parents[1] / "docs"
SCHEMAS = {
    schema["$id"]: schema
    for path in sorted((DOCS / "wire").glob("*.schema.json"))
    if (schema := json.loads(path.read_text()))
}
KINDS = sorted(schema["properties"]["kind"]["const"] for schema in SCHEMAS.values() if "properties" in schema)


def nodes(value, path=()):
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from nodes(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from nodes(item, (*path, index))


def replaced(document, path, value):
    if not path:
        return copy.deepcopy(value)
    result = copy.deepcopy(document)
    parent = result
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = copy.deepcopy(value)
    return result


def example_documents():
    for source in sorted((DOCS / "examples").glob("*.json")):
        document = json.loads(source.read_text())
        yield source.stem, document
        # Vector containers and identity forms are not wire documents. Exercise
        # their refusal too, and extract every complete wire vector they contain.
        if "kind" not in document:
            for path, value in nodes(document):
                if isinstance(value, dict) and "kind" in value and "version" in value:
                    yield f"{source.stem}/{'.'.join(map(str, path))}", value


EXAMPLES = list(example_documents())


@pytest.fixture(scope="module")
def encoded_documents():
    c = contract()
    manifest = Manifest.single(c.table, "full", at(18))
    checks = ()
    receipt = Gate.evaluate(Ledger(c), manifest, checks, now=at(18))
    ledger = Ledger(c).follow(receipt)
    claims = ledger.claims(at(19), limit=timedelta.max)
    values = [
        c,
        manifest,
        manifest.part(c.table),
        ledger,
        receipt,
        claims,
        claims.regions[0],
        receipt.transition,
        receipt.transition.plan,
        receipt.transition.plan.actions[0],
        receipt.checks[0],
        Region(),
        Requirement(c.table, at(19)),
        ExtractionRequest(c.table, "full", at(19)),
        Shape.changes(lookback=timedelta(microseconds=1)),
        Shape.window(size=timedelta(days=1)),
    ]
    for window in ((0, 1), ("a", "z"), (date(2026, 10, 18), date(2026, 10, 19)), (at(18), at(19))):
        values.append(Manifest.single(c.table, "daily", at(20), window=window))
    documents = [
        (f"encoder/{index}/{type(value).__name__}", value.to_dict()) for index, value in enumerate(values)
    ]
    assert {document["kind"] for _, document in documents} == set(KINDS)
    return documents


def schema_accepts(document, validate_wire):
    try:
        validate_wire(document)
    except ValidationError:
        return False
    return True


def loses_microseconds(document):
    for _, value in nodes(document):
        if isinstance(value, dict) and set(value) == {"$seconds"} and type(value["$seconds"]) in (int, float):
            seconds = Decimal(str(value["$seconds"]))
            if seconds.is_finite() and seconds * 1_000_000 != (seconds * 1_000_000).to_integral_value():
                return True
    return False


def semantic_refusal(error):
    # from_dict wraps constructor InvalidContract errors in SerializationError.
    # A structural refusal of a schema-valid document must still be reported.
    cause = error
    while cause is not None:
        if isinstance(cause, InvalidContract) and not isinstance(cause, SerializationError):
            return True
        cause = cause.__cause__
    return isinstance(error.__cause__, OverflowError) or str(error) in (
        "integer cannot be represented exactly as binary64",
        "number is outside the binary64 range",
    )


def discrepancy(document, validate_wire, *, via_json):
    valid = schema_accepts(document, validate_wire)
    raw = json.dumps(document, ensure_ascii=True, separators=(",", ":"))
    evidence = f"schema={'valid' if valid else 'invalid'}, {'from_json' if via_json else 'from_dict'}: {raw}"
    try:
        decoded = Wire.from_json(raw) if via_json else Wire.from_dict(copy.deepcopy(document))
    except InvalidContract as exc:
        # Cross-field constraints, date ordering, UTC overflow, exact binary64
        # integers and duration range are stricter than the structural schemas.
        # A duration the schema admits but that would round is refused by Versioned JSON rule 3.
        if valid and not semantic_refusal(exc) and not loses_microseconds(document):
            return f"{evidence}\nstructural refusal: {type(exc).__name__}: {exc}"
        return None
    except Exception as exc:
        return f"{evidence}\nraised {type(exc).__name__}: {exc}"
    if not valid:
        return f"{evidence}\naccepted as {type(decoded).__name__}"
    if loses_microseconds(document):
        return f"{evidence}\naccepted a duration that cannot represent integral microseconds"
    try:
        encoded = decoded.to_dict()
        validate_wire(encoded)
        again = Wire.from_dict(encoded)
        from_json = Wire.from_json(decoded.to_json())
        assert again == from_json == decoded, "round trip changed the value"
        assert again.identity == from_json.identity == decoded.identity, "round trip changed identity"
    except Exception as exc:
        return f"{evidence}\naccepted, then {type(exc).__name__}: {exc}"
    return None


def assert_agreement(document, validate_wire):
    errors = [
        error
        for via_json in (False, True)
        if (error := discrepancy(document, validate_wire, via_json=via_json))
    ]
    assert not errors, "\n".join(errors)


def mutations(document):
    for path, value in nodes(document):
        # One replacement from every other JSON type, at every value path.
        for replacement in (None, True, 0, 0.5, "text", [], {}):
            if type(replacement) is not type(value):
                yield "type", path, replaced(document, path, replacement)
        if isinstance(value, dict):
            yield "unknown", path, replaced(document, path, value | {"unexpected": None})
            yield "surrogate-key", path, replaced(document, path, value | {"\ud800": None})
            for field in value:
                yield (
                    "drop",
                    (*path, field),
                    replaced(document, path, {k: v for k, v in value.items() if k != field}),
                )
            if "kind" in value and "version" in value:
                for version in (3, 4, True, "5", 5.0):
                    yield "version", path, replaced(document, (*path, "version"), version)
                for kind in ("unknown", *KINDS):
                    if kind != value["kind"]:
                        yield "kind", path, replaced(document, (*path, "kind"), kind)
            for tag, alternatives in (
                ("$seconds", (-1, -1e-7, 1e-7, 1.5e-6, 1e100, -0.0, 1e-6, True)),
                (
                    "$datetime",
                    (
                        "2026-10-18T00:00:00.1234567+00:00",
                        "2026-10-18T00:00:00",
                        "2026-10-18T00:00:00Z",
                        "2026-10-18T00:00:00.123456Z",
                        "0001-01-01T00:00:00+23:59",
                    ),
                ),
                ("$date", ("2026-10-18T00:00:00", "2026-02-30")),
            ):
                if tag in value:
                    for alternative in alternatives:
                        yield tag, path, replaced(document, (*path, tag), alternative)
        elif isinstance(value, str) or value is None:
            # Includes all nonemptyText uses, nullable text and mapping values.
            for text in ("", "\ud800", "\udfff", "\ud800\udc00", "\U00010000"):
                yield "text", path, replaced(document, path, text)
        elif type(value) in (int, float):
            for number in (-0.0, 2**53, 2**53 + 1, 2**53 + 2, 2**68, -(2**53 + 1), True):
                yield "number", path, replaced(document, path, number)


@pytest.mark.parametrize("name,document", EXAMPLES, ids=[name for name, _ in EXAMPLES])
def test_documented_examples_and_vectors_agree_with_the_decoder(name, document, validate_wire):
    assert_agreement(document, validate_wire)


def test_every_kind_has_an_encoder_seed_that_round_trips(encoded_documents, validate_wire):
    for _, document in encoded_documents:
        validate_wire(document)
        assert_agreement(document, validate_wire)


@pytest.mark.parametrize("source", ("examples", "encoder"))
def test_mutations_at_every_path_agree_with_the_schemas(source, encoded_documents, validate_wire):
    errors = []
    for name, document in EXAMPLES if source == "examples" else encoded_documents:
        for mutation, path, changed in mutations(document):
            for via_json in (False, True):
                if error := discrepancy(changed, validate_wire, via_json=via_json):
                    errors.append(f"{name}: {mutation} at {path!r}\n{error}")
    assert not errors, "\n\n".join(errors)


def generation_schema(value):
    """Inline URN references for hypothesis-jsonschema's local resolver."""
    if isinstance(value, list):
        return [generation_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        urn, _, pointer = value["$ref"].partition("#")
        target = SCHEMAS[urn]
        for component in pointer.strip("/").split("/") if pointer else ():
            target = target[component]
        return generation_schema(target)
    result = {key: generation_schema(item) for key, item in value.items() if key not in ("$id", "$schema")}
    if result.get("type") == "array" and "maxItems" not in result:
        result["maxItems"] = max(2, result.get("minItems", 0))
    if result.get("type") == "object" and "properties" not in result:
        result["maxProperties"] = max(2, result.get("minProperties", 0))
    if result.get("type") == "string" and "maxLength" not in result:
        result["maxLength"] = 64 if "64" in result.get("pattern", "") else 12
    return result


@cache
def values_for(kind, field):
    return from_schema(generation_schema(SCHEMAS[f"urn:dogana:wire:5:{kind}"]["properties"][field]))


@pytest.mark.parametrize("kind", KINDS)
@settings(max_examples=12, phases=(Phase.generate,))
@given(data=st.data())
def test_schema_generated_fields_round_trip_or_raise_a_contract_error(
    kind, data, encoded_documents, validate_wire
):
    document = next(document for _, document in encoded_documents if document["kind"] == kind)
    targets = []
    for path, value in nodes(document):
        if not isinstance(value, dict) or "kind" not in value or "version" not in value:
            continue
        for field, schema in SCHEMAS[f"urn:dogana:wire:5:{value['kind']}"]["properties"].items():
            references = [
                item["$ref"] for _, item in nodes(schema) if isinstance(item, dict) and "$ref" in item
            ]
            if field not in ("kind", "version") and all(
                ref.startswith("urn:dogana:wire:5:types#") for ref in references
            ):
                targets.append(((*path, field), value["kind"], field))
    path, owner, field = data.draw(st.sampled_from(targets), label="property")
    value = data.draw(values_for(owner, field), label="schema value")
    assert_agreement(replaced(document, path, value), validate_wire)


@settings(max_examples=40, phases=(Phase.generate,))
@given(
    document=st.recursive(
        st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
        lambda item: st.lists(item, max_size=3) | st.dictionaries(st.text(), item, max_size=3),
        max_leaves=8,
    )
)
def test_arbitrary_json_never_escapes_as_an_unexpected_exception(document, validate_wire):
    assert_agreement(document, validate_wire)


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_nonstandard_number_tokens_are_refused_at_every_numeric_path(constant, encoded_documents):
    for _, document in [*EXAMPLES, *encoded_documents]:
        for path, value in nodes(document):
            if type(value) not in (int, float):
                continue
            changed = replaced(document, path, float(constant))
            raw = json.dumps(changed, allow_nan=True)
            with pytest.raises(InvalidContract):
                Wire.from_json(raw)
            with pytest.raises(InvalidContract):
                Wire.from_dict(changed)


@pytest.mark.parametrize("seconds", (1e-7, -1e-7, 1.5e-6))
def test_duration_decoding_refuses_to_round_away_fractional_microseconds(seconds, validate_wire):
    document = Shape.changes().to_dict()
    document["lookback"] = {"$seconds": seconds}
    assert schema_accepts(document, validate_wire) is (seconds >= 0)
    for decode in (Wire.from_dict, lambda value: Wire.from_json(json.dumps(value))):
        with pytest.raises(InvalidContract):
            decode(document)


def test_datetime_decoding_refuses_to_truncate_a_seventh_fractional_digit(validate_wire):
    document = Manifest.single("stock", "full", at(18)).to_dict()
    document["produced_at"] = {"$datetime": "2026-10-18T00:00:00.1234567+00:00"}
    assert not schema_accepts(document, validate_wire)
    assert_agreement(document, validate_wire)


@pytest.mark.parametrize("field", ("ledger_identity", "publication_id"))
def test_receipts_refuse_empty_identity_fields(field, validate_wire):
    document = json.loads((DOCS / "examples" / "receipt.json").read_text())
    document[field] = ""
    assert not schema_accepts(document, validate_wire)
    assert_agreement(document, validate_wire)


@pytest.mark.parametrize(
    "change",
    (
        {"version": 5.0},
        {"rows": -0.0},
        {"tombstones": -0.0},
        {"window": [-0.0, 1]},
        {"since": 0.0},
        {"generation": 0.0},
    ),
    ids=("version", "rows", "tombstones", "window", "since", "generation"),
)
def test_schema_integer_values_decode_regardless_of_their_json_number_spelling(change, validate_wire):
    document = Manifest.single("stock", "full", at(18)).to_dict()
    if "version" in change:
        document |= change
    else:
        document["parts"]["stock"] |= change
    validate_wire(document)
    assert_agreement(document, validate_wire)
