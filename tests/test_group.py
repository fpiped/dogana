from datetime import timedelta

import pytest

from dogana import (
    Check,
    Contract,
    Gate,
    Group,
    InvalidContract,
    Ledger,
    Manifest,
    Part,
    PlanningError,
    Shape,
    State,
    plan_together,
)
from tests.loader import Loader
from tests.support import FULL, SINCE, at, contract

ORDERS = contract("orders")
LINES = contract("order_lines")


def full(c, day):
    return Ledger(c).apply(Manifest.single(c.table, "full", at(day), rows=0, schema_hash=c.schema_hash))


@pytest.mark.protocol("tables-together.group")
def test_a_group_names_each_table_once_and_references_only_its_tables():
    with pytest.raises(InvalidContract, match="each of its tables once"):
        Group((ORDERS, ORDERS))
    with pytest.raises(InvalidContract, match="each of its tables once"):
        Group(())
    with pytest.raises(InvalidContract, match="each of its tables once"):
        Group((ORDERS, contract("ORDERS")))
    with pytest.raises(InvalidContract, match="not in the group"):
        Group((ORDERS,)).contract("order_lines")
    assert Group([ORDERS, LINES]).tables == ("orders", "order_lines")


@pytest.mark.protocol("tables-together.exact")
def test_a_table_is_exact_only_when_every_region_holds_both_dates_at_one_instant():
    ledger = full(ORDERS, 18)
    assert ledger.claims(at(18), limit=timedelta.max).exact_as_of == at(18)
    assert Ledger(ORDERS).claims(at(18), limit=timedelta.max).exact_as_of is None
    patched = ledger.apply(
        Manifest.single("orders", "since", at(19), since=at(18), schema_hash=ORDERS.schema_hash)
    )
    assert patched.claims(at(19), limit=timedelta.max).exact_as_of is None
    window = (at(19).date(), at(19).date())
    mixed = ledger.apply(
        Manifest.single("orders", "daily", at(19), window=window, schema_hash=ORDERS.schema_hash)
    )
    assert mixed.claims(at(19), limit=timedelta.max).exact_as_of is None
    assert mixed.claims(at(19), limit=timedelta.max, window=window).exact_as_of == at(19)
    assert full(ORDERS, 18).claims(at(30), limit=timedelta.max).state is State.COMPLETE
    assert full(ORDERS, 18).claims(at(30), limit=timedelta(days=7)).state is State.EXPIRED
    assert full(ORDERS, 18).claims(at(30), limit=timedelta(days=7)).exact_as_of == at(18)


@pytest.mark.protocol("claims.2", "tables-together.group")
def test_a_group_expires_by_the_readers_limit_and_judges_only_the_windows_it_is_given():
    ledgers = {"orders": full(ORDERS, 18), "order_lines": full(LINES, 18)}
    group = Group((ORDERS, LINES))
    assert group.claims(ledgers, at(30), limit=timedelta.max).state is State.COMPLETE
    week = group.claims(ledgers, at(30), limit=timedelta(days=7))
    assert (week.state, week.exact_as_of) == (State.EXPIRED, at(18))
    assert group.claims(ledgers, at(25), limit=timedelta(days=7)).state is State.COMPLETE
    day = (at(19).date(), at(19).date())
    later = ledgers["order_lines"].apply(
        Manifest.single("order_lines", "daily", at(19), window=day, schema_hash=LINES.schema_hash)
    )
    mixed = {**ledgers, "order_lines": later}
    assert group.claims(mixed, at(19), limit=timedelta.max).exact_as_of is None
    assert group.claims(mixed, at(19), limit=timedelta.max, windows={"order_lines": day}).exact_as_of is None
    both = group.claims(
        {"orders": full(ORDERS, 19), "order_lines": later},
        at(19),
        limit=timedelta.max,
        windows={"order_lines": day},
    )
    assert both.exact_as_of == at(19)


@pytest.mark.protocol("tables-together.exact")
def test_two_exact_tables_share_an_instant_only_when_their_instants_are_equal():
    early, late = full(ORDERS, 18), full(LINES, 19)
    combined = Group((ORDERS, LINES)).claims(
        {"orders": early, "order_lines": late}, at(19), limit=timedelta.max
    )
    assert (combined.state, combined.complete_as_of, combined.deletions_as_of) == (
        State.COMPLETE,
        at(18),
        at(18),
    )
    assert combined.exact_as_of is None
    group = Group((ORDERS, LINES))
    assert group.claims(
        {"orders": early, "order_lines": full(LINES, 18)}, at(19), limit=timedelta.max
    ).exact_as_of == at(18)
    with pytest.raises(InvalidContract, match="one ledger per table"):
        group.claims({"orders": early}, at(19), limit=timedelta.max)
    with pytest.raises(InvalidContract, match="one ledger per table"):
        group.claims({"orders": early, "order_lines": full(ORDERS, 18)}, at(19), limit=timedelta.max)
    with pytest.raises(InvalidContract, match="outside the group"):
        group.claims(
            {"orders": early, "order_lines": late},
            at(19),
            limit=timedelta.max,
            windows={"moves": (at(1).date(), at(2).date())},
        )


@pytest.mark.protocol("claims.3")
def test_patched_movements_hide_valid_antijoin_answers_and_make_exact_counts_and_sums_wrong():
    store = Loader()
    items = contract("items", columns=None, shapes={"full": FULL})
    movements = contract("movements", columns=None, shapes={"full": FULL, "since": SINCE})
    source_items = [{"id": 1}, {"id": 2}, {"id": 3}]
    source_movements = [
        {"id": 10, "item_id": 1, "amount": 5},
        {"id": 20, "item_id": 2, "amount": 7},
    ]
    baseline = store.deliver(movements, source_movements, movements.manifest("full", at(18), rows=2), at(18))
    assert baseline.accepted

    deleted = source_movements.pop()
    inserted = {"id": 30, "item_id": 1, "amount": 3}
    source_movements.append(inserted)
    exact_items = store.deliver(items, source_items, items.manifest("full", at(19), rows=3), at(19))
    changed = store.deliver(
        movements, [inserted], movements.manifest("since", at(19), since=at(18), rows=1), at(19)
    )
    assert exact_items.accepted and changed.accepted
    assert store.ledger(items).claims(at(19), limit=timedelta.max).exact_as_of == at(19)
    claims = store.ledger(movements).claims(at(19), limit=timedelta.max)
    assert (claims.state, claims.complete_as_of, claims.deletions_as_of) == (
        State.PATCHED,
        at(19),
        at(18),
    )
    published_items, published_movements = store.read(items.table), store.read(movements.table)
    assert published_items == source_items
    assert deleted in published_movements and deleted not in source_movements
    assert all(movement in published_movements for movement in source_movements)

    def without_movements(items, movements):
        return {item["id"] for item in items if not any(m["item_id"] == item["id"] for m in movements)}

    def aggregates(movements):
        return len(movements), sum(m["amount"] for m in movements)

    answer = without_movements(published_items, published_movements)
    truth = without_movements(source_items, source_movements)
    assert answer == {3}
    assert truth == {2, 3}
    assert answer < truth
    assert aggregates(published_movements) == (3, 15)
    assert aggregates(source_movements) == (2, 8)

    repaired = store.deliver(movements, source_movements, movements.manifest("full", at(20), rows=2), at(20))
    assert repaired.accepted
    assert store.ledger(movements).claims(at(20), limit=timedelta.max).exact_as_of == at(20)
    assert without_movements(store.read(items.table), store.read(movements.table)) == truth
    assert aggregates(store.read(movements.table)) == aggregates(source_movements)


@pytest.mark.protocol("tables-together.delivery.2")
def test_the_joint_check_needs_one_read_and_every_table_accepted_on_its_own():
    def declared(**rows):
        parts = {t: Part(shape="full", rows=n, schema_hash=contract(t).schema_hash) for t, n in rows.items()}
        return Manifest(at(18), parts)

    def alone(read, c, passed=True):
        outcomes = () if passed else (Check("row_count", False),)
        return Gate.evaluate(Ledger(c), read, outcomes, now=at(30))

    read = declared(orders=0, order_lines=0)
    both = (alone(read, ORDERS), alone(read, LINES))
    passed = Gate.together(both)
    assert passed == Check(
        "together", True, f"order_lines, orders as of 2026-10-18T00:00:00+00:00, read {read.identity}"
    )
    assert Gate.together(tuple(reversed(both))) == passed
    other = declared(orders=1, order_lines=0)
    assert Gate.together((alone(other, ORDERS), both[1])) == Check(
        "together",
        False,
        f"receipts of different reads: order_lines {read.identity}, orders {other.identity}",
    )
    assert Gate.together((both[0], alone(read, LINES, passed=False))) == Check(
        "together", False, "refused with order_lines"
    )


@pytest.mark.protocol("tables-together.delivery.3")
def test_a_failed_joint_check_turns_an_acceptable_delivery_into_a_rejection():
    manifest = Manifest.single("orders", "full", at(18), rows=0, schema_hash=ORDERS.schema_hash)
    outcomes = ()
    refused = Gate.evaluate(
        Ledger(ORDERS), manifest, outcomes, now=at(30), joint=(Check("references", False, "1 rows"),)
    )
    assert not refused.accepted and refused.checks[-1] == Check("references", False, "1 rows")
    accepted = Gate.evaluate(Ledger(ORDERS), manifest, outcomes, now=at(30), joint=(Check("together", True),))
    assert accepted.accepted and accepted.checks[-1] == Check("together", True)
    with pytest.raises(InvalidContract, match="joint outcomes"):
        Gate.evaluate(Ledger(ORDERS), manifest, outcomes, now=at(30), joint=("together",))  # type: ignore[arg-type]


@pytest.mark.protocol("tables-together.planning")
def test_planning_together_reads_every_table_at_one_boundary_past_all_evidence():
    group = Group((ORDERS, LINES))
    ledgers = {"orders": full(ORDERS, 18), "order_lines": full(LINES, 20)}
    requests = plan_together(group, ledgers, at(19))
    assert {r.until for r in requests.values()} == {at(20) + timedelta(microseconds=1)}
    after = {
        t: ledgers[t].apply(
            Manifest.single(
                t,
                r.shape,
                r.until,
                since=r.since,
                window=r.window,
                schema_hash=ledgers[t].contract.schema_hash,
            )
        )
        for t, r in requests.items()
    }
    assert group.claims(after, at(21), limit=timedelta.max).exact_as_of == at(20) + timedelta(microseconds=1)
    assert plan_together(group, after, at(20)) == {}


@pytest.mark.protocol("tables-together.planning")
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


@pytest.mark.protocol("tables-together.planning")
def test_planning_together_reads_a_keyless_append_after_its_start():
    shapes = {"full": Shape.full(cost=100), "append": Shape.append(cost=1)}
    events = Contract("events", (), shapes, start_at=at(19))
    group = Group((ORDERS, events))
    requests = plan_together(group, {"orders": full(ORDERS, 18), "events": Ledger(events)}, at(19))
    assert {r.until for r in requests.values()} == {at(19) + timedelta(microseconds=1)}


@pytest.mark.protocol(
    "tables-together.delivery",
    "tables-together.delivery.1",
    "tables-together.delivery.3",
    "tables-together.references",
)
def test_a_group_is_accepted_whole_or_rejected_whole():
    group = Group((ORDERS, LINES))
    ledgers = {t: Ledger(group.contract(t)) for t in group.tables}

    def read(day, *tables):
        parts = {t: Part(shape="full", rows=0, schema_hash=contract(t).schema_hash) for t in tables}
        return Manifest(at(day), parts)

    both = read(18, "orders", "order_lines")
    receipts = group.apply(ledgers, both, now=at(30))
    assert all(r.accepted for r in receipts.values())
    assert {r.read_identity for r in receipts.values()} == {both.identity}
    assert receipts["orders"].manifest == both.only("orders")
    after = {t: ledgers[t].follow(receipts[t]) for t in group.tables}
    assert group.claims(after, at(30), limit=timedelta.max).exact_as_of == at(18)
    failed = group.apply(ledgers, both, now=at(30), outcomes={"orders": (Check("row_count", False),)})
    assert not any(r.accepted for r in failed.values())
    assert all(r.checks[-1] == Check("together", False, "refused with orders") for r in failed.values())
    partial = group.apply(ledgers, read(19, "orders"), now=at(30))
    assert set(partial) == {"orders"} and not partial["orders"].accepted
    assert partial["orders"].checks[-1] == Check("together", False, "the read declares no order_lines")
    wider = group.apply(ledgers, read(19, "orders", "order_lines", "moves"), now=at(30))
    assert set(wider) == set(group.tables) and all(r.accepted for r in wider.values())
    with pytest.raises(InvalidContract, match="declares none of the group's tables"):
        group.apply(ledgers, read(19, "moves"), now=at(30))
    for keyed in ({"Orders": (Check("row_count", False),)}, {"moves": ()}):
        with pytest.raises(InvalidContract, match="outcomes name tables the read does not declare"):
            group.apply(ledgers, both, now=at(30), outcomes=keyed)
    with pytest.raises(InvalidContract, match="sources name tables the read does not declare"):
        group.apply(ledgers, read(19, "orders"), now=at(30), sources={"order_lines": "lines.csv"})
    with pytest.raises(InvalidContract, match="one ledger per table"):
        group.apply({"orders": ledgers["orders"]}, both, now=at(30))


@pytest.mark.protocol("manifest.7")
def test_a_snapshot_token_names_the_read_and_is_part_of_its_identity():
    parts = {"orders": Part(shape="full", rows=0, schema_hash=ORDERS.schema_hash)}
    plain, named = Manifest(at(18), parts), Manifest(at(18), parts, snapshot="txn-42")
    assert plain.identity != named.identity
    assert named.only("orders") == named and named.snapshot == "txn-42"
    with pytest.raises(InvalidContract, match="nonempty"):
        Manifest(at(18), parts, snapshot="")


@pytest.mark.protocol("tables-together.reaching-p-p")
def test_later_deliveries_keep_a_common_instant_only_where_each_table_returns_to_equal_dates():
    orders = contract("orders", shapes={"full": FULL, "changes": Shape.changes(deletions="tombstone")})
    lines = contract("order_lines", shapes={"full": FULL, "since": SINCE})
    group = Group((orders, lines))
    ledgers = {
        t: Ledger(c).apply(Manifest.single(t, "full", at(18), rows=0, schema_hash=c.schema_hash))
        for t, c in (("orders", orders), ("order_lines", lines))
    }
    changes = Manifest(
        at(20),
        {
            "orders": Part(shape="changes", since=at(18), schema_hash=orders.schema_hash),
            "order_lines": Part(shape="since", since=at(18), schema_hash=lines.schema_hash),
        },
    )
    receipts = group.apply(ledgers, changes, now=at(30))
    assert all(r.accepted for r in receipts.values())
    after = {t: ledgers[t].follow(receipts[t]) for t in group.tables}
    assert group.claims(after, at(30), limit=timedelta.max).exact_as_of is None
    assert after["orders"].claims(at(30), limit=timedelta.max).exact_as_of == at(20)
    again = Manifest(
        at(21),
        {
            "orders": Part(shape="changes", since=at(20), schema_hash=orders.schema_hash),
            "order_lines": Part(shape="full", rows=0, schema_hash=lines.schema_hash),
        },
    )
    receipts = group.apply(after, again, now=at(30))
    exact = {t: after[t].follow(receipts[t]) for t in group.tables}
    assert group.claims(exact, at(30), limit=timedelta.max).exact_as_of == at(21)


@pytest.mark.protocol("tables-together.planning")
def test_planning_together_ignores_start_at_once_a_keyless_table_has_evidence():
    shapes = {"full": Shape.full(cost=100), "append": Shape.append(cost=1)}
    events = Contract("events", (), shapes, start_at=at(25))
    proven = Ledger(events).apply(Manifest.single("events", "full", at(18), rows=0))
    requests = plan_together(Group((ORDERS, events)), {"orders": full(ORDERS, 18), "events": proven}, at(19))
    assert {r.until for r in requests.values()} == {at(19)}


@pytest.mark.protocol("tables-together.planning")
def test_planning_together_keeps_the_earliest_boundary_when_a_full_read_is_cheaper_than_the_first_append():
    shapes = {"full": Shape.full(cost=1), "append": Shape.append(cost=100)}
    events = Contract("events", (), shapes, start_at=at(25))
    requests = plan_together(
        Group((ORDERS, events)), {"orders": full(ORDERS, 18), "events": Ledger(events)}, at(19)
    )
    assert {(r.shape, r.until) for r in requests.values()} == {("full", at(19))}
