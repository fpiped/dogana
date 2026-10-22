import json
from pathlib import Path

import pytest

from dogana import Contract, Ledger, Manifest, Receipt, Shape

EXAMPLES = Path(__file__).parents[1] / "docs" / "examples"


@pytest.mark.parametrize(
    "name,cls", [("contract", Contract), ("manifest", Manifest), ("ledger", Ledger), ("receipt", Receipt)]
)
def test_protocol_wire_examples_are_complete_versioned_round_trips(name, cls, validate_wire):
    raw = (EXAMPLES / f"{name}.json").read_text()
    validate_wire(json.loads(raw))
    value = cls.from_json(raw)
    assert cls.from_json(value.to_json()) == value
    assert json.loads(raw) == value.to_dict()


def test_the_protocol_inline_json_examples_are_valid_public_wire_values(validate_wire):
    import re

    text = (EXAMPLES.parent / "protocol.md").read_text()
    for raw in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL):
        validate_wire(json.loads(raw))
        cls = {"Shape": Shape, "Manifest": Manifest}[json.loads(raw)["kind"]]
        assert cls.from_json(raw).to_dict() == json.loads(raw)


def test_the_protocol_receipt_replays_to_the_documented_ledger():
    c = Contract.from_json((EXAMPLES / "contract.json").read_text())
    m = Manifest.from_json((EXAMPLES / "manifest.json").read_text())
    ledger = Ledger.from_json((EXAMPLES / "ledger.json").read_text())
    receipt = Receipt.from_json((EXAMPLES / "receipt.json").read_text())
    assert Ledger(c).apply(m) == ledger == receipt.ledger
    assert receipt.manifest == m


def test_example_identities_match_fixed_independently_specified_hashes():
    expected = json.loads((EXAMPLES / "identity-vectors.json").read_text())["examples"]
    assert {path.stem for path in EXAMPLES.glob("*.json")} == {*expected, "identity-vectors", "jcs-vectors"}
    for name, cls in (
        ("contract", Contract),
        ("manifest", Manifest),
        ("ledger", Ledger),
        ("receipt", Receipt),
    ):
        value = cls.from_json((EXAMPLES / f"{name}.json").read_text())
        assert value.identity == expected[name] == (EXAMPLES / f"{name}.identity").read_text().strip()


def test_identity_vectors_fix_canonical_bytes_and_hashes(validate_wire):
    from hashlib import sha256

    from dogana.wire import Wire

    vectors = json.loads((EXAMPLES / "identity-vectors.json").read_text())["vectors"]
    for vector in vectors:
        validate_wire(vector["wire"])
        value = Wire.from_dict(vector["wire"])
        assert value.identity_json() == vector["canonical"], vector["name"]
        assert value.identity == vector["sha256"], vector["name"]
        assert sha256(vector["canonical"].encode("utf-8")).hexdigest() == vector["sha256"]


@pytest.mark.parametrize("name", ["contract", "manifest", "ledger", "receipt"])
def test_wire_schemas_refuse_missing_unknown_and_invalid_version_fields(name, validate_wire):
    from jsonschema import ValidationError

    value = json.loads((EXAMPLES / f"{name}.json").read_text())
    for field in value:
        missing = {key: item for key, item in value.items() if key != field}
        with pytest.raises(ValidationError):
            validate_wire(missing)
    for change in ({"unknown": None}, {"version": True}, {"version": 2}):
        with pytest.raises(ValidationError):
            validate_wire(value | change)


def test_receipt_schema_distinguishes_all_outcomes_and_rejects_the_removed_booleans(validate_wire):
    from dataclasses import replace

    from jsonschema import ValidationError

    from dogana import Check, Claims, Region, RegionClaim, State

    accepted = Receipt.from_json((EXAMPLES / "receipt.json").read_text())
    rejected = replace(
        accepted,
        outcome="rejected",
        checks=(Check("row_count", False, "row count differs"),),
        transition=None,
        ledger=Ledger(accepted.ledger.contract),
        claims=Claims((RegionClaim(accepted.manifest.table, Region(), State.UNKNOWN),)),
        publication_id=None,
    )
    replayed = replace(accepted, outcome="replayed")
    for value in (accepted, rejected, replayed):
        raw = value.to_dict()
        validate_wire(raw)
        assert Receipt.from_dict(raw) == value
        for change in ({"accepted": value.accepted}, {"replayed": value.replayed}, {"outcome": "unknown"}):
            with pytest.raises(ValidationError):
                validate_wire(raw | change)
    assert accepted.identity == replayed.identity != rejected.identity
    with pytest.raises(ValidationError):
        validate_wire(accepted.to_dict() | {"transition": None})
    with pytest.raises(ValidationError):
        validate_wire(rejected.to_dict() | {"transition": accepted.transition.to_dict()})


def test_rfc8785_binary64_vectors_fix_ecmascript_number_spelling():
    import struct

    from dogana import SerializationError
    from dogana.wire import canonical_json

    vectors = json.loads((EXAMPLES / "jcs-vectors.json").read_text())
    for vector in vectors["numbers"]:
        value = struct.unpack(">d", bytes.fromhex(vector["binary64"]))[0]
        assert canonical_json(value) == vector["canonical"], vector["binary64"]
        if value.is_integer():
            assert canonical_json(int(value)) == vector["canonical"]
    for bits in vectors["invalid_numbers"]:
        with pytest.raises(SerializationError, match="finite"):
            canonical_json(struct.unpack(">d", bytes.fromhex(bits))[0])


def test_jcs_vectors_fix_utf8_bytes_utf16_order_and_string_escapes():
    from hashlib import sha256

    from dogana.wire import canonical_json

    vectors = json.loads((EXAMPLES / "jcs-vectors.json").read_text())["values"]
    for vector in vectors:
        encoded = canonical_json(vector["value"])
        assert encoded == vector["canonical"], vector["name"]
        assert sha256(encoded.encode("utf-8")).hexdigest() == vector["sha256"]


def test_the_protocol_receipt_matches_the_gate_decision_at_receiver_time():
    from dogana import Gate

    contract = Contract.from_json((EXAMPLES / "contract.json").read_text())
    receipt = Receipt.from_json((EXAMPLES / "receipt.json").read_text())
    names = {requirement.name for requirement in Gate.requirements(contract, receipt.manifest)}
    outcomes = tuple(check for check in receipt.checks if check.name in names)
    assert (
        Gate.evaluate(
            Ledger(contract), receipt.manifest, outcomes, now=receipt.received_at, source=receipt.source
        )
        == receipt
    )


def test_external_column_hash_keeps_the_original_ordered_pair_bytes():
    from hashlib import sha256

    columns = {"id": "int", "value": "str"}
    encoded = json.dumps(list(columns.items()), ensure_ascii=True, separators=(",", ":")).encode("ascii")
    assert encoded == b'[["id","int"],["value","str"]]'
    expected = "14054a7d708a1c718d4aaa9a7e35fb5bdccb8c97ac31f25b778ac0af5c1d3ad5"
    assert sha256(encoded).hexdigest() == expected
    contract = Contract.from_json((EXAMPLES / "contract.json").read_text())
    manifest = Manifest.from_json((EXAMPLES / "manifest.json").read_text())
    assert contract.schema_hash == manifest.schema_hash == expected
    assert contract.types == {"id": "int"}


@pytest.mark.parametrize("digest", ["\ud800", "\udfff", "hash\udc80"])
def test_contract_schema_hash_rejects_surrogates_in_wire_schema_and_decoder(digest, validate_wire):
    from jsonschema import ValidationError

    from dogana import SerializationError

    raw = Contract.from_json((EXAMPLES / "contract.json").read_text()).to_dict()
    raw["schema_hash"] = digest
    with pytest.raises(ValidationError):
        validate_wire(raw)
    with pytest.raises(SerializationError):
        Contract.from_dict(raw)
