import json
from pathlib import Path

import pytest

from dogana import Contract, Ledger, Manifest, Receipt, Shape

EXAMPLES = Path(__file__).parents[1] / "docs" / "examples"


@pytest.mark.parametrize(
    "name,cls", [("contract", Contract), ("manifest", Manifest), ("ledger", Ledger), ("receipt", Receipt)]
)
def test_protocol_wire_examples_are_complete_versioned_round_trips(name, cls):
    raw = (EXAMPLES / f"{name}.json").read_text()
    value = cls.from_json(raw)
    assert cls.from_json(value.to_json()) == value
    assert json.loads(raw) == value.to_dict()


def test_the_protocol_inline_json_examples_are_valid_public_wire_values():
    import re

    text = (EXAMPLES.parent / "protocol.md").read_text()
    for raw in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL):
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
    assert {path.stem for path in EXAMPLES.glob("*.json")} == {*expected, "identity-vectors"}
    for name, cls in (
        ("contract", Contract),
        ("manifest", Manifest),
        ("ledger", Ledger),
        ("receipt", Receipt),
    ):
        value = cls.from_json((EXAMPLES / f"{name}.json").read_text())
        assert value.identity == expected[name] == (EXAMPLES / f"{name}.identity").read_text().strip()


def test_identity_vectors_fix_canonical_bytes_and_hashes():
    from hashlib import sha256

    from dogana.wire import Wire

    vectors = json.loads((EXAMPLES / "identity-vectors.json").read_text())["vectors"]
    for vector in vectors:
        value = Wire.from_dict(vector["wire"])
        assert value.identity_json() == vector["canonical"], vector["name"]
        assert value.identity == vector["sha256"], vector["name"]
        assert sha256(vector["canonical"].encode("utf-8")).hexdigest() == vector["sha256"]


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
