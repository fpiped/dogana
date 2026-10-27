from datetime import timedelta

import pytest

from dogana import (
    Check,
    Claims,
    Contract,
    Gate,
    Group,
    InvalidContract,
    Ledger,
    Manifest,
    PlanningError,
    Reference,
    Shape,
    State,
    plan_together,
)
from tests.support import SINCE, at, contract

ORDERS = contract("orders")
LINES = contract("order_lines")
LINE = Reference("order_lines", ("order_id",), "orders")


def full(c, day):
    return Ledger(c).apply(Manifest(c.table, "full", at(day), rows=0, schema_hash=c.schema_hash))


def test_a_reference_names_distinct_columns_with_valid_identifiers():
    with pytest.raises(InvalidContract, match="distinct columns"):
        Reference("order_lines", (), "orders")
    with pytest.raises(InvalidContract, match="distinct columns"):
        Reference("order_lines", ("order_id", "order_id"), "orders")
    with pytest.raises(InvalidContract, match="identifiers"):
        Reference("order lines", ("order_id",), "orders")
    assert str(LINE) == "order_lines(order_id) references orders"


def test_a_group_names_each_table_once_and_references_only_its_tables():
    with pytest.raises(InvalidContract, match="each of its tables once"):
        Group((ORDERS, ORDERS))
    with pytest.raises(InvalidContract, match="each of its tables once"):
        Group(())
    with pytest.raises(InvalidContract, match="outside the group"):
        Group((LINES,), (LINE,))
    with pytest.raises(InvalidContract, match="one column per key column"):
        Group((ORDERS, LINES), (Reference("order_lines", ("order_id", "day"), "orders"),))
    with pytest.raises(InvalidContract, match="not in the group"):
        Group((ORDERS,)).contract("order_lines")
    assert Group([ORDERS, LINES], [LINE]).tables == ("orders", "order_lines")


def test_a_table_is_exact_only_when_every_region_holds_both_dates_at_one_instant():
    ledger = full(ORDERS, 18)
    assert ledger.claims(at(18)).exact_as_of == at(18)
    assert Ledger(ORDERS).claims(at(18)).exact_as_of is None
    patched = ledger.apply(Manifest("orders", "since", at(19), since=at(18), schema_hash=ORDERS.schema_hash))
    assert patched.claims(at(19)).exact_as_of is None
    window = (at(19).date(), at(19).date())
    mixed = ledger.apply(Manifest("orders", "daily", at(19), window=window, schema_hash=ORDERS.schema_hash))
    assert mixed.claims(at(19)).exact_as_of is None
    assert mixed.claims(at(19), window=window).exact_as_of == at(19)
    assert full(ORDERS, 18).claims(at(30)).state is State.EXPIRED
    assert full(ORDERS, 18).claims(at(30)).exact_as_of == at(18)


def test_two_exact_tables_share_an_instant_only_when_their_instants_are_equal():
    early, late = full(ORDERS, 18), full(LINES, 19)
    combined = Claims.combine(early.claims(at(19)), late.claims(at(19)))
    assert (combined.state, combined.complete_as_of, combined.deletions_as_of) == (
        State.COMPLETE,
        at(18),
        at(18),
    )
    assert combined.exact_as_of is None
    group = Group((ORDERS, LINES))
    assert group.claims({"orders": early, "order_lines": full(LINES, 18)}, at(19)).exact_as_of == at(18)
    with pytest.raises(InvalidContract, match="one ledger per table"):
        group.claims({"orders": early}, at(19))
    with pytest.raises(InvalidContract, match="one ledger per table"):
        group.claims({"orders": early, "order_lines": full(ORDERS, 18)}, at(19))
    with pytest.raises(InvalidContract, match="outside the group"):
        group.claims({"orders": early, "order_lines": late}, at(19), {"moves": (at(1).date(), at(2).date())})


def test_the_joint_check_needs_one_boundary_and_every_table_accepted_on_its_own():
    def alone(c, day, passed=True):
        manifest = Manifest(c.table, "full", at(day), rows=0, schema_hash=c.schema_hash)
        outcomes = tuple(Check(r.name, passed) for r in Gate.requirements(c, manifest))
        return Gate.evaluate(Ledger(c), manifest, outcomes, now=at(30))

    assert Gate.together((alone(ORDERS, 18), alone(LINES, 18))) == Check(
        "together", True, "orders, order_lines as of 2026-10-18T00:00:00+00:00"
    )
    assert Gate.together((alone(ORDERS, 18), alone(LINES, 19))) == Check(
        "together", False, "orders, order_lines carry different read boundaries"
    )
    assert Gate.together((alone(ORDERS, 18), alone(LINES, 18, passed=False))) == Check(
        "together", False, "refused with order_lines"
    )


def test_a_failed_joint_check_turns_an_acceptable_delivery_into_a_rejection():
    manifest = Manifest("orders", "full", at(18), rows=0, schema_hash=ORDERS.schema_hash)
    outcomes = tuple(Check(r.name, True) for r in Gate.requirements(ORDERS, manifest))
    refused = Gate.evaluate(
        Ledger(ORDERS), manifest, outcomes, now=at(30), joint=(Check("references", False, "1 rows"),)
    )
    assert not refused.accepted and refused.checks[-1] == Check("references", False, "1 rows")
    accepted = Gate.evaluate(Ledger(ORDERS), manifest, outcomes, now=at(30), joint=(Check("together", True),))
    assert accepted.accepted and accepted.checks[-1] == Check("together", True)
    with pytest.raises(InvalidContract, match="joint outcomes"):
        Gate.evaluate(Ledger(ORDERS), manifest, outcomes, now=at(30), joint=("together",))  # type: ignore[arg-type]


def test_planning_together_reads_every_table_at_one_boundary_past_all_evidence():
    group = Group((ORDERS, LINES))
    ledgers = {"orders": full(ORDERS, 18), "order_lines": full(LINES, 20)}
    requests = plan_together(group, ledgers, at(19))
    assert {r.until for r in requests.values()} == {at(20) + timedelta(microseconds=1)}
    after = {
        t: ledgers[t].apply(Manifest(t, r.shape, r.until, since=r.since, window=r.window))
        for t, r in requests.items()
    }
    assert group.claims(after, at(21)).exact_as_of == at(20) + timedelta(microseconds=1)
    assert plan_together(group, after, at(20)) == {}


def test_planning_together_covers_each_table_over_its_window_only():
    group = Group((ORDERS, LINES))
    ledgers = {"orders": full(ORDERS, 18), "order_lines": full(LINES, 18)}
    window = (at(19).date(), at(19).date())
    requests = plan_together(group, ledgers, at(19), windows={"order_lines": window})
    assert requests["order_lines"].window == window
    assert requests["orders"].window is None
    only_changes = contract("orders", shapes={"since": SINCE})
    with pytest.raises(PlanningError, match="orders: no admitted shape"):
        plan_together(
            Group((only_changes, LINES)),
            {"orders": Ledger(only_changes), "order_lines": ledgers["order_lines"]},
            at(19),
        )


def test_planning_together_reads_a_keyless_append_after_its_start():
    shapes = {"full": Shape.full(cost=100), "append": Shape.append(cost=1)}
    events = Contract("events", (), shapes, start_at=at(19))
    group = Group((ORDERS, events))
    requests = plan_together(group, {"orders": full(ORDERS, 18), "events": Ledger(events)}, at(19))
    assert {r.until for r in requests.values()} == {at(19) + timedelta(microseconds=1)}
