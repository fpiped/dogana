from dataclasses import replace

import pytest

from dogana import Gate, InconsistentPublication, Ledger, Manifest
from tests.support import at, contract, fold


def publication(ledger, day):
    manifest = ledger.contract.manifest("full", at(day))
    checks = ()
    return Gate.evaluate(ledger, manifest, checks, now=at(25))


@pytest.mark.protocol("checks.receipt-size", "receiver.recovery")
def test_full_and_rejected_deliveries_rebuild_the_same_ledger_from_receipts():
    c = contract()
    first = publication(Ledger(c), 18)
    refused = Gate.evaluate(fold(first), Manifest.single(c.table, "full", at(17)), now=at(25))
    second = publication(fold(first), 19)
    rebuilt = Ledger.from_receipts(c, iter((first, refused, second)))
    assert rebuilt == Ledger(c).apply(first.manifest).apply(second.manifest)
    assert rebuilt.identity == second.ledger_identity
    assert Ledger.from_receipts(c, ()) == Ledger(c)


@pytest.mark.parametrize(
    "problem,message",
    [
        ("missing_transition", "only accepted receipts contain a transition"),
        ("rejected_transition", "only accepted receipts contain a transition"),
        ("failed_check", "accepted receipts require passing checks"),
        ("future", "accepted receipts cannot attest a future source boundary"),
        ("rejected_parent", "rejected receipts retain their base publication"),
    ],
)
def test_decoded_receipts_keep_their_outcome_checks_clock_and_parent_consistent(problem, message):
    from dogana import InvalidContract, Receipt

    first = publication(Ledger(contract()), 18)
    document = first.to_dict()
    if problem == "missing_transition":
        document["transition"] = None
    elif problem == "rejected_transition":
        document["outcome"] = "rejected"
    elif problem == "failed_check":
        document["checks"][0]["passed"] = False
    elif problem == "future":
        document["received_at"] = {"$datetime": at(17).isoformat()}
    else:
        document.update(outcome="rejected", transition=None)
    with pytest.raises(InvalidContract, match=message):
        Receipt.from_dict(document)


@pytest.mark.parametrize(
    "problem,message",
    [
        ("parent", "accepted receipt has a different parent publication"),
        ("evidence", "accepted receipt breaks publication chain or ledger evidence"),
    ],
)
def test_following_a_forged_receipt_names_the_broken_parent_or_evidence(problem, message):
    c = contract()
    first = publication(Ledger(c), 18)
    receipt = (
        replace(first, base_publication_id="another")
        if problem == "parent"
        else replace(first, ledger_identity="forged")
    )
    with pytest.raises(InconsistentPublication) as error:
        Ledger(c).follow(receipt)
    assert str(error.value) == message


@pytest.mark.protocol("checks.receipt-size", "receiver.recovery")
@pytest.mark.parametrize("problem", ["missing", "reordered", "duplicate", "foreign", "evidence"])
def test_a_broken_accepted_receipt_chain_cannot_rebuild_a_publication(problem):
    c = contract()
    first = publication(Ledger(c), 18)
    second = publication(fold(first), 19)
    receipts = {
        "missing": (second,),
        "reordered": (second, first),
        "duplicate": (first, first),
        "foreign": (publication(Ledger(contract("other")), 18),),
        "evidence": (replace(first, ledger_identity=replace(fold(first), generation="invented").identity),),
    }[problem]
    with pytest.raises(InconsistentPublication):
        Ledger.from_receipts(c, receipts)


@pytest.mark.protocol("contract-evolution.compatibility")
def test_full_deliveries_rebuild_each_publication_under_its_original_policy():

    c = contract()
    first = publication(Ledger(c), 18)
    newer = replace(c, shapes={name: replace(shape, cost=shape.cost + 1) for name, shape in c.shapes.items()})
    second = publication(fold(first).with_contract(newer), 19)
    assert first.contract == c
    assert second.contract == newer
    rebuilt = Ledger.from_receipts(newer, (first, second))
    assert rebuilt == Ledger(c).apply(first.manifest).with_contract(newer).apply(second.manifest)
    assert rebuilt.identity == second.ledger_identity
    assert Ledger.from_receipts(c, (first, second)) == rebuilt.with_contract(c)
    assert second.base_publication_id == first.publication_id


@pytest.mark.protocol("receiver.1")
def test_replayed_full_delivery_receipt_retains_its_original_identity_and_receiving_time():
    first = publication(Ledger(contract()), 18)
    replay = replace(first, outcome="replayed")
    assert replay.identity == first.identity
    assert replay.received_at == first.received_at
    assert replay != first
    assert replay.to_json() != first.to_json()


@pytest.mark.protocol("checks.receipt-size")
def test_many_full_deliveries_keep_receipt_size_bounded_by_regions_not_history():
    from datetime import timedelta

    from dogana import Contract
    from tests.support import FULL

    c = Contract("stock", ("id",), {"full": FULL}, types={"id": "int"})
    ledger = Ledger(c)
    sizes = []
    for index in range(1000):
        boundary = at(18) + timedelta(seconds=index)
        manifest = Manifest.single(c.table, "full", boundary)
        checks = ()
        receipt = Gate.evaluate(ledger, manifest, checks, now=boundary)
        assert receipt.accepted
        ledger = ledger.follow(receipt)
        if index in (1, 999):
            sizes.append(len(receipt.to_json()))
    assert abs(sizes[1] - sizes[0]) < 100
    assert len(ledger.regions) == 1
    assert "accepted" not in ledger.to_dict()
