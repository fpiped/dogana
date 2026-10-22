from datetime import date, datetime, timedelta

import pytest

from dogana import Checkpoint, Completeness, Contract, Coverage, Deletions, Ledger, Manifest, RejectedDelivery, Shape, State

LIMIT = timedelta(days=7)
FULL = Shape(Coverage.ALL, Completeness.COMPLETE, Deletions.ABSENCE, Checkpoint.NONE)
WINDOW = Shape(Coverage.WINDOW, Completeness.COMPLETE, Deletions.ABSENCE, Checkpoint.NONE)
SINCE = Shape(Coverage.ALL, Completeness.CHANGED_ONLY, Deletions.NONE, Checkpoint.TIMESTAMP)
SINCE_WINDOW = Shape(Coverage.WINDOW, Completeness.CHANGED_ONLY, Deletions.NONE, Checkpoint.TIMESTAMP)
FEED = Shape(Coverage.KEYS, Completeness.CHANGED_ONLY, Deletions.TOMBSTONE, Checkpoint.GENERATION)

STOCK = Contract("stock", ("id",), {"full": FULL})
INVOICES = Contract("invoices", ("id",), {"full": FULL, "since": SINCE})
ORDERS = Contract("orders", ("id",), {"full": FULL, "feed": FEED})
MOVES = Contract("moves", ("id",), {"full": FULL, "daily": WINDOW, "recent": SINCE_WINDOW}, window_column="day")


def at(day: int, hour: int = 3) -> datetime:
    return datetime(2026, 10, day, hour)


def d(day: int, month: int = 10) -> date:
    return date(2026, month, day)


def test_nightly_full_export_is_complete_and_covers_everything():
    ledger = Ledger(STOCK).apply(Manifest("stock", "full", at(20)))
    assert ledger.claims(at(21), LIMIT) == {
        "state": State.COMPLETE, "complete_as_of": at(20), "valid_as_of": at(20), "covers_all": True}


def test_rows_changed_since_without_a_delete_signal_patch_but_do_not_validate():
    ledger = Ledger(INVOICES).apply(Manifest("invoices", "full", at(18)))
    ledger = ledger.apply(Manifest("invoices", "since", at(19), since=at(18)))
    ledger = ledger.apply(Manifest("invoices", "since", at(20), since=at(19)))
    assert ledger.claims(at(20, 9), LIMIT) == {
        "state": State.PATCHED, "complete_as_of": at(20), "valid_as_of": at(18), "covers_all": True}


def test_the_weekly_full_resets_validity_after_a_week_of_patches():
    ledger = Ledger(INVOICES).apply(Manifest("invoices", "full", at(18)))
    for day in range(19, 25):
        ledger = ledger.apply(Manifest("invoices", "since", at(day), since=at(day - 1)))
    ledger = ledger.apply(Manifest("invoices", "full", at(25)))
    assert ledger.claims(at(25, 9), LIMIT) == {
        "state": State.COMPLETE, "complete_as_of": at(25), "valid_as_of": at(25), "covers_all": True}


def test_a_changed_only_delivery_needs_a_base():
    with pytest.raises(RejectedDelivery):
        Ledger(INVOICES).apply(Manifest("invoices", "since", at(19), since=at(18)))


def test_a_feed_from_the_generation_we_hold_is_complete_and_from_any_other_is_refused():
    ledger = Ledger(ORDERS).apply(Manifest("orders", "full", at(18), generation="g41"))
    ledger = ledger.apply(Manifest("orders", "feed", at(19), since="g41", generation="g42"))
    assert ledger.generation == "g42"
    assert ledger.claims(at(19, 9), LIMIT) == {
        "state": State.COMPLETE, "complete_as_of": at(19), "valid_as_of": at(19), "covers_all": True}
    with pytest.raises(RejectedDelivery):
        ledger.apply(Manifest("orders", "feed", at(20), since="g41", generation="g43"))


def test_a_daily_window_splits_the_earlier_window_and_keeps_its_dates():
    ledger = Ledger(MOVES).apply(Manifest("moves", "daily", at(20), window=(d(21, 9), d(20))))
    ledger = ledger.apply(Manifest("moves", "daily", at(21), window=(d(22, 9), d(21))))
    assert [(r.low, r.high, r.low_open, r.high_open, r.complete_as_of) for r in ledger.regions] == [
        (d(21, 9), d(22, 9), False, True, at(20)),
        (d(22, 9), d(21), False, False, at(21)),
    ]
    assert ledger.covers_all() is False
    assert ledger.claims(at(21, 9), LIMIT)["state"] is State.UNKNOWN


def test_the_weekly_full_absorbs_the_windows_and_a_later_window_cuts_it_in_three():
    ledger = Ledger(MOVES).apply(Manifest("moves", "daily", at(20), window=(d(21, 9), d(20))))
    ledger = ledger.apply(Manifest("moves", "full", at(25)))
    assert [(r.low, r.high) for r in ledger.regions] == [(None, None)]
    ledger = ledger.apply(Manifest("moves", "daily", at(26), window=(d(27, 9), d(26))))
    assert [(r.low, r.high, r.low_open, r.high_open, r.complete_as_of) for r in ledger.regions] == [
        (None, d(27, 9), False, True, at(25)),
        (d(27, 9), d(26), False, False, at(26)),
        (d(26), None, True, False, at(25)),
    ]
    assert ledger.covers_all() is True
    assert ledger.claims(at(26, 9), LIMIT) == {
        "state": State.COMPLETE, "complete_as_of": at(25), "valid_as_of": at(25), "covers_all": True}


def test_a_window_of_changed_rows_patches_only_the_regions_it_crosses():
    ledger = Ledger(MOVES).apply(Manifest("moves", "full", at(20)))
    ledger = ledger.apply(Manifest("moves", "daily", at(21), window=(d(1), d(21))))
    ledger = ledger.apply(Manifest("moves", "recent", at(22), window=(d(15), d(22)), since=at(21)))
    assert [r.state(at(22, 9), LIMIT) for r in ledger.regions] == [State.COMPLETE, State.PATCHED, State.PATCHED]


def test_a_region_older_than_the_limit_is_expired_whatever_its_shape():
    ledger = Ledger(STOCK).apply(Manifest("stock", "full", at(10)))
    assert ledger.claims(at(20), LIMIT)["state"] is State.EXPIRED
