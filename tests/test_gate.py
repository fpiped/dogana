from dataclasses import replace
from datetime import timedelta

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
from tests.support import FULL, SINCE, WINDOW, at, fold
from tests.support import accounting_contract as contract


def outcomes(c, m, fail=None):
    return tuple(Check(r.name, r.name != fail) for r in Gate.requirements(c, m))


@pytest.mark.protocol("checks.receipt")
@pytest.mark.parametrize("failure", ["row_count", "rows_in_window", "key_integrity"])
def test_a_failed_required_check_never_changes_the_published_ledger(failure):
    c = contract()
    ledger = Ledger(c)
    manifest = Manifest(c.table, "daily", at(18), window=(1, 30), rows=10, schema_hash=c.schema_hash)
    receipt = Gate.evaluate(ledger, manifest, outcomes(c, manifest, failure), now=at(25))
    assert not receipt.accepted
    assert receipt.ledger_identity == ledger.identity
    assert ledger.follow(receipt) is ledger
    assert receipt.publication_id is None
    assert receipt.transition is None
    assert Receipt.from_json(receipt.to_json()) == receipt


@pytest.mark.protocol("checks.answers")
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


@pytest.mark.protocol("checks.receipt", "checks.evaluation")
def test_a_receipt_records_the_manifest_checks_transition_claims_and_publication():
    c = contract()
    m = Manifest(c.table, "full", at(18), rows=2, schema_hash=c.schema_hash)
    receipt = Gate.evaluate(Ledger(c), m, outcomes(c, m), now=at(25))
    assert receipt.accepted
    assert receipt.manifest == m
    assert all(c.passed for c in receipt.checks)
    assert receipt.transition.regions == fold(receipt).regions == (Region(None, None, at(18), at(18)),)
    assert receipt.ledger_identity == Ledger(c).apply(m).identity
    assert fold(receipt).claims(receipt.received_at, limit=timedelta.max).deletions_as_of == at(18)
    assert Receipt.from_json(receipt.to_json()) == receipt
    changed = replace(m, rows=3)
    assert changed.identity != m.identity


@pytest.mark.protocol("checks.plans")
@pytest.mark.parametrize(
    "key,column,actions,message",
    [
        (("id", "id"), None, (), "duplicate plan key column"),
        (("id",), None, (("replace_window", (1, 2)),), "window actions need a window column"),
        ((), None, (("upsert", None),), "key actions need a key"),
        ((), None, (("tombstone", None),), "key actions need a key"),
    ],
)
def test_apply_plans_require_distinct_keys_and_the_columns_their_actions_use(key, column, actions, message):
    from dogana import Action, ApplyPlan

    with pytest.raises(InvalidContract, match=message):
        ApplyPlan("moves", key, column, tuple(Action(operation, window) for operation, window in actions))


@pytest.mark.parametrize("kind", ["requirement", "check", "transition"])
def test_checks_need_names_and_transitions_need_evidence(kind):
    from dogana import ApplyPlan, CheckRequirement, Transition

    with pytest.raises(InvalidContract, match="needs"):
        if kind == "requirement":
            CheckRequirement("")
        elif kind == "check":
            Check("", True)
        else:
            Transition(ApplyPlan("moves", ("id",), None, ()), ())


@pytest.mark.parametrize(
    "shape,kwargs,operations",
    [
        ("full", {}, [Operation.REPLACE_ALL]),
        ("daily", {"window": (1, 30)}, [Operation.REPLACE_WINDOW]),
        ("since", {"since": at(17)}, [Operation.UPSERT]),
        (
            "feed",
            {"since": "g1", "generation": "g2", "tombstones": 1},
            [Operation.UPSERT, Operation.TOMBSTONE],
        ),
    ],
)
def test_apply_plans_name_the_store_operations_without_accessing_rows(shape, kwargs, operations):
    c = contract()
    plan = ApplyPlan.build(c, Manifest(c.table, shape, at(18), **kwargs))
    assert [a.operation for a in plan.actions] == operations
    assert ApplyPlan.from_json(plan.to_json()) == plan


@pytest.mark.protocol("checks.plans")
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


@pytest.mark.protocol("transitions.1", "checks.evaluation")
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
    second = Gate.evaluate(fold(first), newer, outcomes(c, newer), now=at(25))
    replay = Gate.evaluate(fold(first, second), m, now=at(25))
    assert not replay.accepted
    assert replay.ledger_identity == second.ledger_identity
    assert replay.transition is None
    assert replay.publication_id == second.publication_id


@pytest.mark.protocol("transitions.7", "trust-model.5")
def test_future_dated_full_delivery_is_refused_without_blocking_the_next_honest_delivery():
    c = contract()
    future = Manifest(c.table, "full", at(26))
    refused = Gate.evaluate(Ledger(c), future, outcomes(c, future), now=at(25))
    assert not refused.accepted
    assert any(check.name == "receiver_time" and not check.passed for check in refused.checks)
    assert refused.received_at == at(25)
    honest = Manifest(c.table, "full", at(25))
    accepted = Gate.evaluate(Ledger(c).follow(refused), honest, outcomes(c, honest), now=at(25))
    assert accepted.accepted


@pytest.mark.protocol("checks.receipt", "claims.expired")
def test_delayed_full_delivery_receipt_records_expiry_at_receiver_time_and_source():
    from dogana import State

    c = contract()
    manifest = Manifest(c.table, "full", at(17))
    receipt = Gate.evaluate(Ledger(c), manifest, outcomes(c, manifest), now=at(25), source="stock.csv")
    assert receipt.accepted
    assert fold(receipt).claims(receipt.received_at, limit=timedelta(days=7)).state is State.EXPIRED
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


@pytest.mark.protocol("checks.errors")
def test_invalid_full_delivery_cannot_hide_an_unexpected_programming_error(monkeypatch):
    def broken_apply(self, manifest):
        raise TypeError("unexpected failure")

    c = contract()
    monkeypatch.setattr(Ledger, "apply", broken_apply)
    with pytest.raises(TypeError, match="unexpected failure"):
        Gate.preflight(Ledger(c), Manifest(c.table, "full", at(18)), now=at(18))


@pytest.mark.protocol("contract.cursor-column")
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


@pytest.mark.protocol("checks.evaluation")
def test_row_refusal_helper_retains_the_base_publication_and_receiver_clock():
    c = contract()
    base = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    receipt = Gate.reject(base, Manifest(c.table, "full", at(19)), (Check("row_count", False),), now=at(20))
    assert not receipt.accepted
    assert receipt.base_publication_id == receipt.publication_id == base.publication_id
    assert receipt.ledger_identity == base.identity
    assert base.follow(receipt) is base
    with pytest.raises(InvalidContract, match="failed check"):
        Gate.reject(base, receipt.manifest, (Check("row_count", True),), now=at(20))


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Check(1, True),
        lambda: Check("row_count", 1),
        lambda: CheckRequirement(1),
        lambda: Action("upsert", window=(1, 2)),
        lambda: Transition("plan", (Region(),)),
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
    transition = Transition(plan, [Region(complete_as_of=at(18), deletions_as_of=at(18))])
    assert Transition.from_json(transition.to_json()) == transition
    assert hash(transition) == hash(Transition.from_json(transition.to_json()))


@pytest.mark.protocol("checks.schema_hash", "contract.column-hash")
@pytest.mark.parametrize("digest", [None, "wrong"])
def test_a_store_cannot_pass_a_manifest_hash_that_differs_from_the_contract(digest):
    c = contract(schema_hash="opaque:columns")
    m = Manifest(c.table, "full", at(18), schema_hash=digest)
    receipt = Gate.evaluate(Ledger(c), m, outcomes(c, m), now=at(18))
    assert not receipt.accepted
    assert any(check.name == "schema_hash" and not check.passed for check in receipt.checks)


@pytest.mark.protocol("checks.schema_hash", "contract.column-hash")
def test_hashless_accounting_does_not_require_a_schema_check():
    c = contract()
    m = c.manifest("full", at(18))
    assert "schema_hash" not in {r.name for r in Gate.requirements(c, m)}
    assert Gate.evaluate(Ledger(c), m, outcomes(c, m), now=at(18)).accepted
