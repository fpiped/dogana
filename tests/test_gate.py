from dataclasses import replace

import pytest

from dogana import (
    Action,
    ApplyPlan,
    Check,
    CheckRequirement,
    Contract,
    Gate,
    InvalidContract,
    Ledger,
    Manifest,
    Operation,
    Receipt,
    Region,
    Transition,
)
from tests.support import FULL, SINCE, WINDOW, at
from tests.support import accounting_contract as contract


def outcomes(c, m, fail=None):
    return tuple(Check(r.name, r.name != fail) for r in Gate.requirements(c, m))


@pytest.mark.parametrize("failure", ["row_count", "rows_in_window", "key_integrity", "shrink_tolerance"])
def test_a_failed_required_check_never_changes_the_published_ledger(failure):
    c = contract(max_shrink_fraction=0.2)
    ledger = Ledger(c)
    manifest = Manifest(c.table, "daily", at(18), window=(1, 30), rows=10, schema_hash=c.schema_hash)
    receipt = Gate.evaluate(ledger, manifest, outcomes(c, manifest, failure), now=at(25))
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
    receipt = Gate.evaluate(Ledger(c), m, checks, now=at(25))
    assert not receipt.accepted
    assert any(c.name == "check_set" and not c.passed for c in receipt.checks)


def test_a_receipt_records_the_manifest_checks_transition_claims_and_publication():
    c = contract()
    m = Manifest(c.table, "full", at(18), rows=2, schema_hash=c.schema_hash)
    receipt = Gate.evaluate(Ledger(c), m, outcomes(c, m), now=at(25))
    assert receipt.accepted
    assert receipt.manifest == m
    assert all(c.passed for c in receipt.checks)
    assert receipt.transition.before == Ledger(c).regions
    assert receipt.transition.after == receipt.ledger.regions
    assert receipt.publication_id == receipt.ledger.publication_id
    assert receipt.claims.deletions_as_of == at(18)
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
    shapes = {"full": FULL, "plant": replace(WINDOW, window_size=None), "log": SINCE}
    c = Contract("receipts", (), shapes, window_column="id", types={"id": "int"}, start_at=at(1))
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
    receipt = Gate.evaluate(Ledger(c), Manifest(c.table, "surprise", at(18)), now=at(25))
    assert not receipt.accepted
    assert receipt.checks[0].name == "manifest"


def test_a_repeated_full_delivery_requires_the_store_receipt_lookup_before_gate_evaluation():
    c = contract()
    m = Manifest(c.table, "full", at(18), rows=2, schema_hash=c.schema_hash)
    first = Gate.evaluate(Ledger(c), m, outcomes(c, m), now=at(25))
    newer = Manifest(c.table, "full", at(19), rows=3, schema_hash=c.schema_hash)
    second = Gate.evaluate(first.ledger, newer, outcomes(c, newer), now=at(25))
    replay = Gate.evaluate(second.ledger, m, now=at(25))
    assert not replay.accepted
    assert replay.ledger is second.ledger
    assert replay.transition is None
    assert replay.publication_id == second.publication_id


def test_future_dated_full_delivery_is_refused_without_blocking_the_next_honest_delivery():
    c = contract()
    future = Manifest(c.table, "full", at(26))
    refused = Gate.evaluate(Ledger(c), future, outcomes(c, future), now=at(25))
    assert not refused.accepted
    assert any(check.name == "receiver_time" and not check.passed for check in refused.checks)
    assert refused.received_at == at(25)
    honest = Manifest(c.table, "full", at(25))
    accepted = Gate.evaluate(refused.ledger, honest, outcomes(c, honest), now=at(25))
    assert accepted.accepted


def test_delayed_full_delivery_receipt_records_expiry_at_receiver_time_and_source():
    from dogana import State

    c = contract()
    manifest = Manifest(c.table, "full", at(17))
    receipt = Gate.evaluate(Ledger(c), manifest, outcomes(c, manifest), now=at(25), source="stock.csv")
    assert receipt.accepted
    assert receipt.claims.state is State.EXPIRED
    assert receipt.received_at == at(25)
    assert receipt.source == "stock.csv"
    assert not receipt.replayed
    assert receipt.base_publication_id is None


def test_full_delivery_preflight_and_evaluation_require_receiver_time():
    c = contract()
    manifest = Manifest(c.table, "full", at(18))
    for call in (Gate.preflight, Gate.evaluate):
        with pytest.raises(TypeError, match="now"):
            call(Ledger(c), manifest)


def test_invalid_full_delivery_cannot_hide_an_unexpected_programming_error(monkeypatch):
    def broken_apply(self, manifest):
        raise TypeError("unexpected failure")

    c = contract()
    monkeypatch.setattr(Ledger, "apply", broken_apply)
    with pytest.raises(TypeError, match="unexpected failure"):
        Gate.preflight(Ledger(c), Manifest(c.table, "full", at(18)), now=at(18))


def test_timestamp_deliveries_request_cursor_interval_checks_only_when_declared():

    c = Contract(
        "events",
        ("id",),
        {"full": FULL, "since": SINCE},
        types={"id": "int", "changed_at": "datetime"},
        cursor_column="changed_at",
        schema_hash="declared-columns",
    )
    full_names = {r.name for r in Gate.requirements(c, Manifest(c.table, "full", at(18)))}
    patch_names = {r.name for r in Gate.requirements(c, Manifest(c.table, "since", at(19), since=at(18)))}
    assert "rows_in_interval" not in full_names
    assert "rows_in_interval" in patch_names
    assert "schema_hash" in full_names
    accounting = contract()
    assert "schema_hash" not in {
        r.name for r in Gate.requirements(accounting, Manifest(accounting.table, "full", at(18)))
    }


def test_row_refusal_helper_retains_the_base_publication_and_receiver_clock():
    c = contract()
    base = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    receipt = Gate.reject(base, Manifest(c.table, "full", at(19)), (Check("row_count", False),), now=at(20))
    assert not receipt.accepted
    assert receipt.base_publication_id == receipt.publication_id == base.publication_id
    assert receipt.ledger is base
    with pytest.raises(InvalidContract, match="failed check"):
        Gate.reject(base, receipt.manifest, (Check("row_count", True),), now=at(20))


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Check(1, True),
        lambda: Check("row_count", 1),
        lambda: CheckRequirement(1),
        lambda: Action("upsert", keys=("ab",)),
        lambda: Transition("regions", (), None),
        lambda: Region(low_open=1),
    ],
)
def test_malformed_delivery_values_are_refused_before_serialization(factory):
    with pytest.raises(InvalidContract):
        factory()


def test_delivery_transition_lists_normalize_to_hashable_roundtripping_values():
    from dogana import Region, Transition

    c = contract()
    plan = ApplyPlan.build(c, Manifest(c.table, "full", at(18)))
    transition = Transition([Region()], [Region(complete_as_of=at(18), deletions_as_of=at(18))], plan)
    assert Transition.from_json(transition.to_json()) == transition
    assert hash(transition) == hash(Transition.from_json(transition.to_json()))


@pytest.mark.parametrize("digest", [None, "wrong"])
def test_a_store_cannot_pass_a_manifest_hash_that_differs_from_the_contract(digest):
    c = contract(schema_hash="opaque:columns")
    m = Manifest(c.table, "full", at(18), schema_hash=digest)
    receipt = Gate.evaluate(Ledger(c), m, outcomes(c, m), now=at(18))
    assert not receipt.accepted
    assert any(check.name == "schema_hash" and not check.passed for check in receipt.checks)


def test_hashless_accounting_does_not_require_a_schema_check():
    c = contract()
    m = c.manifest("full", at(18))
    assert "schema_hash" not in {r.name for r in Gate.requirements(c, m)}
    assert Gate.evaluate(Ledger(c), m, outcomes(c, m), now=at(18)).accepted
