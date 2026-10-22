from dataclasses import replace
from functools import partial

import pytest

from dogana import ApplyPlan, Check, Contract, Gate, Ledger, Manifest, Operation, Receipt
from tests.support import FULL, SINCE, WINDOW, at, contract

# Accounting-only contracts: these tests mix int, str, date and datetime window domains on one table.
contract = partial(contract, schema=())


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
def test_an_adapter_cannot_authorize_publication_with_an_incomplete_or_ambiguous_check_set(kind):
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


def test_a_keyless_complete_delivery_replaces_its_coverage_and_only_its_appends_append():
    shapes = {"full": FULL, "plant": WINDOW, "log": SINCE}
    c = Contract("receipts", (), shapes, window_column="id", start_at=at(1))
    whole = ApplyPlan.build(c, Manifest(c.table, "full", at(18)))
    plant = ApplyPlan.build(c, Manifest(c.table, "plant", at(18), window=(1, 1)))
    log = ApplyPlan.build(c, Manifest(c.table, "log", at(19), since=at(18)))
    assert [a.operation for a in whole.actions + plant.actions + log.actions] == [
        Operation.REPLACE_ALL,
        Operation.REPLACE_WINDOW,
        Operation.APPEND,
    ]
    assert "key_integrity" not in [r.name for r in Gate.requirements(c, Manifest(c.table, "full", at(18)))]


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
