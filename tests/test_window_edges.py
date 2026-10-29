from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dogana import Ledger, Manifest, Region
from tests.support import WINDOW, at
from tests.support import accounting_contract as contract


@pytest.mark.protocol("contract.domains")
@pytest.mark.parametrize(
    "low,unit", [(2**52, 1), (date(2026, 1, 1), timedelta(days=1)), (at(18), timedelta(microseconds=1))]
)
@pytest.mark.parametrize("low_open,high_open", [(False, False), (False, True), (True, False), (True, True)])
def test_open_and_closed_discrete_windows_clip_only_their_exact_points(low, unit, low_open, high_open):
    region = Region(low, low + 3 * unit, low_open=low_open, high_open=high_open)
    for offset in range(-1, 5):
        point = Region(low + offset * unit, low + offset * unit)
        expected = (0 if not low_open else 1) <= offset <= (3 if not high_open else 2)
        assert (region.intersection(point) is not None) is expected


@pytest.mark.protocol("contract.domains")
@pytest.mark.parametrize(
    "low,unit", [(2**52, 1), (date(2026, 1, 1), timedelta(days=1)), (at(18), timedelta(microseconds=1))]
)
def test_equal_adjacent_and_exactly_touching_windows_have_no_phantom_gap(low, unit):
    c = contract(window_type=type(low).__name__, shapes={"daily": WINDOW})
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(19), window=(low, low)))
    ledger = ledger.apply(Manifest(c.table, "daily", at(20), window=(low + unit, low + 3 * unit)))
    assert ledger.claims(at(20), limit=timedelta.max, window=(low, low + 3 * unit)).complete_as_of == at(19)
    ledger = ledger.apply(Manifest(c.table, "daily", at(21), window=(low + 3 * unit, low + 4 * unit)))
    assert ledger.claims(
        at(21), limit=timedelta.max, window=(low + 3 * unit, low + 3 * unit)
    ).deletions_as_of == at(21)
    assert ledger.claims(
        at(21), limit=timedelta.max, window=(low + 2 * unit, low + 2 * unit)
    ).deletions_as_of == at(20)


@pytest.mark.protocol("contract.domains")
def test_fall_back_window_bounds_are_utc_instants_not_equal_local_clock_readings():
    rome = ZoneInfo("Europe/Rome")
    low = datetime(2026, 10, 25, 2, 30, tzinfo=rome, fold=0)
    high = datetime(2026, 10, 25, 2, 30, tzinfo=rome, fold=1)
    c = contract(window_type="datetime", shapes={"daily": WINDOW})
    manifest = Manifest(c.table, "daily", at(26), window=(low, high))
    assert manifest.window == (low.astimezone(UTC), high.astimezone(UTC))
    ledger = Ledger(c).apply(manifest)
    assert ledger == Ledger.from_json(ledger.to_json())
    for offset in (0, 30, 60):
        point = low.astimezone(UTC) + timedelta(minutes=offset)
        assert ledger.claims(at(26), limit=timedelta.max, window=(point, point)).deletions_as_of == at(26)
    outside = low.astimezone(UTC) - timedelta(microseconds=1)
    assert ledger.claims(at(26), limit=timedelta.max, window=(outside, outside)).deletions_as_of is None


@pytest.mark.protocol("contract.domains")
def test_spring_forward_window_measures_elapsed_utc_time_across_the_local_clock_gap():
    rome = ZoneInfo("Europe/Rome")
    low = datetime(2026, 3, 29, 1, 30, tzinfo=rome)
    high = datetime(2026, 3, 29, 3, 30, tzinfo=rome)
    region = Region(low, high)
    assert region.high - region.low == timedelta(hours=1)
    assert region.low == datetime(2026, 3, 29, 0, 30, tzinfo=UTC)
    assert region.high == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)


@pytest.mark.protocol("contract.domains")
def test_adjacent_string_deliveries_cover_their_successor_without_a_phantom_gap():
    c = contract(window_type="str", shapes={"daily": WINDOW})
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(19), window=("a", "a")))
    ledger = ledger.apply(Manifest(c.table, "daily", at(20), window=("a\x00", "a\x00")))
    claims = ledger.claims(at(20), limit=timedelta.max, window=("a", "a\x00"))
    assert claims.complete_as_of == at(19)
    assert len(claims.regions) == 2


@pytest.mark.protocol("contract.domains")
@pytest.mark.parametrize(
    "low,high", [(date.min, date.max), (datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC))]
)
def test_full_representable_window_delivery_leaves_no_unknown_exterior(low, high):
    c = contract(window_type=type(low).__name__, shapes={"daily": WINDOW})
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(19), window=(low, high)))
    assert ledger.claims(at(19), limit=timedelta.max).complete_as_of == at(19)
    assert len(ledger.regions) == 1
    assert Ledger.from_json(ledger.to_json()) == ledger


@pytest.mark.protocol("contract.domains")
def test_first_string_partition_delivery_leaves_no_unknown_region_below_the_empty_string():
    c = contract(window_type="str", shapes={"daily": WINDOW})
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(19), window=("", "")))
    assert len(ledger.regions) == 2
    assert ledger.regions[0].complete_as_of == at(19)


@pytest.mark.parametrize("endpoint", ["low", "high"])
def test_unbounded_region_endpoints_cannot_be_open(endpoint):
    from dogana import InvalidContract

    with pytest.raises(InvalidContract, match="unbounded endpoints cannot be open"):
        Region(**{f"{endpoint}_open": True})


def test_singleton_strings_do_not_intersect_their_successor():
    assert Region("a", "a").intersection(Region("a\x00", "a\x00")) is None
    assert Region("a", "a").touches(Region("a\x00", "a\x00"))


def test_intersecting_a_known_region_preserves_its_evidence():
    left = Region(1, 5, at(19), at(18))
    part = left.intersection(Region(2, 6, at(20), at(20)))
    assert part is not None
    assert (part.low, part.high, part.high_open) == (2, 6, True)
    assert (part.complete_as_of, part.deletions_as_of) == (at(19), at(18))


@pytest.mark.protocol("contract.domains")
@pytest.mark.parametrize(
    "region",
    [
        dict(high="", high_open=True),
        dict(low="a", high="a\x00", low_open=True, high_open=True),
        dict(high=date.min, high_open=True),
        dict(low=date.max, low_open=True),
    ],
)
def test_delivery_regions_cannot_contain_an_empty_scalar_interval(region):
    from dogana import InvalidContract

    with pytest.raises(InvalidContract, match="empty"):
        Region(**region)


@pytest.mark.protocol("contract.domains")
def test_delivery_regions_only_touch_their_ordered_successor():
    left, right = Region(1, 9), Region(10, 20)
    assert left.touches(right)
    assert not right.touches(left)


@pytest.mark.protocol("contract.domains")
@pytest.mark.parametrize("value", [2**53, 2**100, -(2**100)])
def test_integer_successor_cuts_need_not_be_representable_as_wire_numbers(value):
    c = contract(shapes={"daily": WINDOW})
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(19), window=(value, value)))
    assert ledger.claims(at(19), limit=timedelta.max, window=(value, value)).complete_as_of == at(19)
    assert ledger.claims(at(19), limit=timedelta.max).complete_as_of is None
    assert len(ledger.regions) == 3
    assert Ledger.from_json(ledger.to_json()) == ledger
    known = ledger.regions[1]
    assert known.high == value and not known.high_open
    assert ledger.regions[2].low == value and ledger.regions[2].low_open


@pytest.mark.protocol("contract.domains")
@pytest.mark.parametrize("maximum", [date.max, datetime.max.replace(tzinfo=UTC)])
def test_delivery_at_the_last_scalar_preserves_the_preceding_unknown_domain(maximum):
    c = contract(window_type=type(maximum).__name__, shapes={"daily": WINDOW})
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(19), window=(maximum, maximum)))
    assert len(ledger.regions) == 2
    assert ledger.regions[0].complete_as_of is None
    assert ledger.regions[1].complete_as_of == at(19)
    assert ledger.claims(at(19), limit=timedelta.max, window=(maximum, maximum)).complete_as_of == at(19)
    assert Ledger.from_json(ledger.to_json()) == ledger
