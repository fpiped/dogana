from dataclasses import replace

import pytest

from dogana import ApplyPlan, Check, Gate, Ledger, Manifest, Operation, Receipt
from tests.support import at, contract


def outcomes(c, m, fail=None):
    return tuple(Check(r.name, r.name != fail) for r in Gate.requirements(c, m))


@pytest.mark.parametrize(
    "failure", ["row_count", "schema_hash", "rows_in_window", "key_integrity", "shrink_tolerance"]
)
def test_a_failed_required_check_never_changes_the_published_ledger(failure):
    c = contract(max_shrink_fraction=0.2)
    ledger = Ledger(c)
    manifest = Manifest(c.table, "daily", at(18), window=(1, 30), rows=10, schema_hash=c.schema_hash)
    receipt = Gate.evaluate(ledger, manifest, outcomes(c, manifest, failure))
    assert not receipt.accepted
    assert receipt.ledger is ledger
    assert receipt.publication_id is None
    assert receipt.transition is None
    assert Receipt.from_json(receipt.to_json()) == receipt


@pytest.mark.parametrize("kind", ["missing", "duplicate", "unexpected"])
def test_a_store_cannot_authorize_publication_with_an_incomplete_or_ambiguous_check_set(kind):
    c = contract()
    m = Manifest(c.table, "full", at(18))
    checks = outcomes(c, m)
    checks = (
        checks[:-1]
        if kind == "missing"
        else checks + (checks[0] if kind == "duplicate" else Check("extra", True),)
    )
    receipt = Gate.evaluate(Ledger(c), m, checks)
    assert not receipt.accepted
    assert any(c.name == "check_set" and not c.passed for c in receipt.checks)


def test_a_receipt_records_the_manifest_checks_transition_claims_and_publication():
    c = contract()
    m = Manifest(c.table, "full", at(18), rows=2, schema_hash=c.schema_hash)
    receipt = Gate.evaluate(Ledger(c), m, outcomes(c, m))
    assert receipt.accepted
    assert receipt.manifest == m
    assert all(c.passed for c in receipt.checks)
    assert receipt.transition.before == Ledger(c).regions
    assert receipt.transition.after == receipt.ledger.regions
    assert receipt.publication_id == receipt.ledger.publication_id
    assert receipt.claims.valid_as_of == at(18)
    assert Receipt.from_json(receipt.to_json()) == receipt
    changed = replace(m, rows=3)
    assert changed.identity != m.identity


@pytest.mark.parametrize(
    "shape,kwargs,operations",
    [
        ("full", {}, [Operation.REPLACE_ALL]),
        ("daily", {"window": (1, 30)}, [Operation.REPLACE_WINDOW]),
        ("since", {"since": at(17)}, [Operation.UPSERT]),
        (
            "feed",
            {"since": "g1", "generation": "g2", "tombstones": ((1,),)},
            [Operation.UPSERT, Operation.TOMBSTONE],
        ),
    ],
)
def test_apply_plans_name_the_store_operations_without_accessing_rows(shape, kwargs, operations):
    c = contract()
    plan = ApplyPlan.build(c, Manifest(c.table, shape, at(18), **kwargs))
    assert [a.operation for a in plan.actions] == operations
    assert ApplyPlan.from_json(plan.to_json()) == plan


def test_an_unadmitted_manifest_still_leaves_a_rejected_receipt():
    c = contract()
    receipt = Gate.evaluate(Ledger(c), Manifest(c.table, "surprise", at(18)))
    assert not receipt.accepted
    assert receipt.checks[0].name == "manifest"


def test_a_gate_replay_has_no_row_operations_even_after_a_newer_delivery():
    c = contract()
    m = Manifest(c.table, "full", at(18), rows=2, schema_hash=c.schema_hash)
    first = Gate.evaluate(Ledger(c), m, outcomes(c, m))
    newer = Manifest(c.table, "full", at(19), rows=3, schema_hash=c.schema_hash)
    second = Gate.evaluate(first.ledger, newer, outcomes(c, newer))
    replay = Gate.evaluate(second.ledger, m)
    assert replay.accepted
    assert replay.ledger is second.ledger
    assert replay.transition.plan.actions == ()
    assert replay.publication_id == second.publication_id
