from dataclasses import replace

import pytest

from dogana import Check, Gate, InconsistentPublication, Ledger, Manifest
from tests.support import at, contract


def publication(ledger, day):
    manifest = Manifest(ledger.contract.table, "full", at(day))
    checks = tuple(Check(r.name, True) for r in Gate.requirements(ledger.contract, manifest))
    return Gate.evaluate(ledger, manifest, checks)


def test_full_and_rejected_deliveries_rebuild_the_same_ledger_from_receipts():
    c = contract()
    first = publication(Ledger(c), 18)
    refused = Gate.evaluate(first.ledger, Manifest(c.table, "full", at(17)))
    second = publication(first.ledger, 19)
    assert Ledger.from_receipts(c, iter((first, refused, second))) == second.ledger
    assert Ledger.from_receipts(c, ()) == Ledger(c)


@pytest.mark.parametrize("problem", ["missing", "reordered", "duplicate", "foreign", "evidence"])
def test_a_broken_accepted_receipt_chain_cannot_rebuild_a_publication(problem):
    c = contract()
    first = publication(Ledger(c), 18)
    second = publication(first.ledger, 19)
    receipts = {
        "missing": (second,),
        "reordered": (second, first),
        "duplicate": (first, first),
        "foreign": (publication(Ledger(contract("other")), 18),),
        "evidence": (replace(first, ledger=replace(first.ledger, generation="invented")),),
    }[problem]
    with pytest.raises(InconsistentPublication):
        Ledger.from_receipts(c, receipts)
