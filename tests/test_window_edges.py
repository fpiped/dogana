from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dogana import Column, Contract, Ledger, Manifest, Region, Shape
from tests.support import WINDOW, at, contract


@pytest.mark.parametrize(
    "low,unit", [(10**100, 1), (date(2026, 1, 1), timedelta(days=1)), (at(18), timedelta(microseconds=1))]
)
@pytest.mark.parametrize("low_open,high_open", [(False, False), (False, True), (True, False), (True, True)])
def test_open_and_closed_discrete_windows_clip_only_their_exact_points(low, unit, low_open, high_open):
    region = Region(low, low + 3 * unit, low_open=low_open, high_open=high_open)
    for offset in range(-1, 5):
        point = Region(low + offset * unit, low + offset * unit)
        expected = (0 if not low_open else 1) <= offset <= (3 if not high_open else 2)
        assert (region.intersection(point) is not None) is expected


@pytest.mark.parametrize(
    "low,unit", [(10**100, 1), (date(2026, 1, 1), timedelta(days=1)), (at(18), timedelta(microseconds=1))]
)
def test_equal_adjacent_and_exactly_touching_windows_have_no_phantom_gap(low, unit):
    c = contract(shapes={"daily": WINDOW}, schema=())
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(19), window=(low, low)))
    ledger = ledger.apply(Manifest(c.table, "daily", at(20), window=(low + unit, low + 3 * unit)))
    assert ledger.claims(at(20), window=(low, low + 3 * unit)).complete_as_of == at(19)
    ledger = ledger.apply(Manifest(c.table, "daily", at(21), window=(low + 3 * unit, low + 4 * unit)))
    assert ledger.claims(at(21), window=(low + 3 * unit, low + 3 * unit)).valid_as_of == at(21)
    assert ledger.claims(at(21), window=(low + 2 * unit, low + 2 * unit)).valid_as_of == at(20)


def test_fall_back_window_bounds_are_utc_instants_not_equal_local_clock_readings():
    rome = ZoneInfo("Europe/Rome")
    low = datetime(2026, 10, 25, 2, 30, tzinfo=rome, fold=0)
    high = datetime(2026, 10, 25, 2, 30, tzinfo=rome, fold=1)
    c = contract(shapes={"daily": WINDOW}, schema=())
    manifest = Manifest(c.table, "daily", at(26), window=(low, high))
    assert manifest.window == (low.astimezone(UTC), high.astimezone(UTC))
    ledger = Ledger(c).apply(manifest)
    assert ledger == Ledger.from_json(ledger.to_json())
    for offset in (0, 30, 60):
        point = low.astimezone(UTC) + timedelta(minutes=offset)
        assert ledger.claims(at(26), window=(point, point)).valid_as_of == at(26)
    outside = low.astimezone(UTC) - timedelta(microseconds=1)
    assert ledger.claims(at(26), window=(outside, outside)).valid_as_of is None


def test_distinct_fall_back_datetime_keys_are_not_mistaken_for_duplicates():
    rome = ZoneInfo("Europe/Rome")
    keys = tuple((datetime(2026, 10, 25, 2, 30, tzinfo=rome, fold=fold),) for fold in (0, 1))
    c = Contract(
        "events",
        ("time",),
        {"feed": Shape("keys", "changed_only", "none", "timestamp")},
        schema=(Column("time", "datetime"),),
    )
    manifest = Manifest(c.table, "feed", at(26), since=at(25), keys=keys)
    manifest.check(c)
    assert manifest.keys[0] != manifest.keys[1]
