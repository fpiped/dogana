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
