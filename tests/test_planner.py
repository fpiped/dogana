from datetime import timedelta
from functools import partial

import pytest

from dogana import Ledger, Manifest, PlanningError, Requirement, next_extraction, plan_delivery
from tests.support import FEED, FULL, SCHEMA, SINCE, WINDOW, at, contract

# Accounting-only contracts: these tests mix int, str, date and datetime window domains on one table.
contract = partial(contract, schema=())


def test_deletions_known_today_choose_the_cheapest_complete_shape_for_a_patched_region():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    ledger = ledger.apply(Manifest(c.table, "since", at(19), since=at(18)))
    request = plan_delivery(ledger, Requirement(c.table, at(20), at(20), (10, 20)))
    assert request.shape == "daily"
    assert request.window == (10, 20)
    assert request.cost == WINDOW.cost
    assert plan_delivery(ledger, Requirement(c.table, at(20), at(20))).shape == "full"


def test_a_clean_generation_base_uses_a_cheap_tombstone_feed_to_advance_both_dates():
    c = contract(shapes={"full": FULL, "feed": FEED})
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18), generation="g1"))
    request = plan_delivery(ledger, Requirement(c.table, at(19), at(19)))
    assert (request.shape, request.since) == ("feed", "g1")


def test_a_reader_needing_only_completeness_can_choose_a_changed_since_read():
    c = contract(shapes={"full": FULL, "since": SINCE})
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    request = plan_delivery(ledger, Requirement(c.table, at(19)))
    assert request.shape == "since"
    assert request.interval == (at(18), at(19))


def test_an_already_satisfied_reader_needs_no_extraction():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    assert plan_delivery(ledger, Requirement(c.table, at(18), at(18))) is None


def test_an_unknown_base_demands_a_complete_read_and_a_contract_without_one_cannot_satisfy_it():
    c = contract()
    assert plan_delivery(Ledger(c), Requirement(c.table, at(18))).shape == "full"
    c = contract(shapes={"since": SINCE})
    with pytest.raises(PlanningError, match="no admitted"):
        plan_delivery(Ledger(c), Requirement(c.table, at(18)))


def test_the_next_changed_since_interval_starts_at_the_weakest_region_boundary():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    ledger = ledger.apply(Manifest(c.table, "daily", at(19), window=(1, 20)))
    assert next_extraction(ledger, "since", at(20)).interval == (at(18), at(20))
    assert next_extraction(ledger, "recent", at(20), window=(1, 20)).since == at(19)


def test_the_next_rolling_window_uses_the_declared_width_and_column_type():
    c = contract(schema=SCHEMA)
    request = next_extraction(Ledger(c), "daily", at(20))
    assert request.interval == ((at(20) - timedelta(days=29)).date(), at(20).date())


def test_the_next_read_cannot_go_backwards_in_source_time():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(20)))
    with pytest.raises(PlanningError, match="advance"):
        next_extraction(ledger, "since", at(19))


def test_a_tombstone_feed_can_advance_completeness_when_its_old_validity_already_satisfies_the_reader():
    c = contract(shapes={"full": FULL, "since": SINCE, "feed": FEED})
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18), generation="g1"))
    ledger = ledger.apply(Manifest(c.table, "since", at(19), since=at(18), generation="g2"))
    request = plan_delivery(ledger, Requirement(c.table, at(20), at(18)))
    assert request.shape == "feed"
