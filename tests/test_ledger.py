from datetime import timedelta

import pytest

from dogana import Claims, Contract, Ledger, Manifest, RejectedDelivery, State
from tests.support import FEED, FULL, SINCE, at, contract


def full(c, day=18, generation=None):
    return Ledger(c).apply(Manifest(c.table, "full", at(day), generation=generation))


def point(ledger, value, now=None):
    return ledger.claims(now or at(25), window=(value, value))


def test_nightly_full_exports_replace_the_entire_domain_with_current_evidence():
    c = contract(shapes={"full": FULL})
    ledger = full(c).apply(Manifest(c.table, "full", at(19)))
    claim = ledger.claims(at(19))
    assert (claim.state, claim.complete_as_of, claim.valid_as_of) == (State.COMPLETE, at(19), at(19))
    assert len(ledger.regions) == 1


def test_weekly_full_plus_changed_since_without_deletes_stays_patched_until_sunday():
    c = contract(shapes={"full": FULL, "since": SINCE})
    ledger = full(c)
    for day in range(19, 25):
        ledger = ledger.apply(Manifest(c.table, "since", at(day), since=at(day - 1)))
        claim = ledger.claims(at(day))
        assert (claim.state, claim.complete_as_of, claim.valid_as_of) == (State.PATCHED, at(day), at(18))
    ledger = ledger.apply(Manifest(c.table, "full", at(25)))
    assert ledger.claims(at(25)).valid_as_of == at(25)


def test_weekly_full_plus_daily_thirty_day_windows_keeps_old_dates_outside_the_window():
    c = contract()
    ledger = full(c)
    for day in range(19, 25):
        low, high = (at(day) - timedelta(days=30)).date(), at(day).date()
        ledger = ledger.apply(Manifest(c.table, "daily", at(day), window=(low, high)))
        assert point(ledger, high, at(day)).valid_as_of == at(day)
        assert ledger.claims(at(day)).complete_as_of == at(18)
        assert ledger.claims(at(day)).valid_as_of == at(18)


def test_sliding_daily_windows_split_earlier_regions_and_leave_unknown_history_visible():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(20), window=(1, 30)))
    ledger = ledger.apply(Manifest(c.table, "daily", at(21), window=(2, 31)))
    assert [(r.low, r.high, r.low_open, r.high_open) for r in ledger.regions] == [
        (None, 1, False, True),
        (1, 2, False, True),
        (2, 31, False, False),
        (31, None, True, False),
    ]
    assert point(ledger, 1).complete_as_of == at(20)
    assert point(ledger, 2).complete_as_of == at(21)
    assert ledger.claims(at(21)).state is State.UNKNOWN
    assert ledger.claims(at(21)).complete_as_of is None


@pytest.mark.parametrize(
    "windows",
    [
        [(1, 30), (10, 20)],
        [(10, 20), (1, 30)],
        [(1, 20), (10, 30)],
        [(10, 30), (1, 20)],
        [(1, 10), (10, 20)],
        [(1, 5), (10, 15)],
        [(5, 5), (1, 10), (5, 5)],
        [(1, 30), (1, 30)],
    ],
)
def test_shrinking_growing_partial_and_touching_windows_preserve_exact_coverage(windows):
    c = contract()
    ledger = Ledger(c)
    expected = {}
    for day, window in enumerate(windows, 18):
        ledger = ledger.apply(Manifest(c.table, "daily", at(day), window=window))
        expected.update({n: at(day) for n in range(window[0], window[1] + 1)})
        for n in range(0, 32):
            assert point(ledger, n).complete_as_of == expected.get(n)
        assert all(a.touches(b) for a, b in zip(ledger.regions, ledger.regions[1:], strict=False))


def test_an_older_window_overlapping_newer_evidence_is_refused_as_a_whole():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(21), window=(10, 20)))
    before = ledger.to_json()
    with pytest.raises(RejectedDelivery, match="later"):
        ledger.apply(Manifest(c.table, "daily", at(20), window=(1, 15)))
    assert ledger.to_json() == before
    assert point(ledger, 10).valid_as_of == at(21)
    assert point(ledger, 1).state is State.UNKNOWN


def test_an_older_disjoint_window_can_establish_previously_unknown_history():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(21), window=(10, 20)))
    ledger = ledger.apply(Manifest(c.table, "daily", at(20), window=(1, 9)))
    assert point(ledger, 1).complete_as_of == at(20)
    assert point(ledger, 10).complete_as_of == at(21)


def test_the_same_window_manifest_applied_twice_is_one_delivery_even_after_a_later_delivery():
    c = contract()
    first = Manifest(c.table, "daily", at(20), window=(1, 30))
    ledger = Ledger(c).apply(first)
    assert ledger.apply(first) is ledger
    ledger = ledger.apply(Manifest(c.table, "daily", at(21), window=(2, 31)))
    assert ledger.apply(first) is ledger
    assert len(ledger.accepted) == 2


def test_independent_plant_exports_include_unknown_plants_and_empty_decommissioned_plants():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(18), window=("IT01", "IT01"), rows=50))
    ledger = ledger.apply(Manifest(c.table, "daily", at(20), window=("DE01", "DE01"), rows=20))
    ledger = ledger.apply(Manifest(c.table, "daily", at(21), window=("IT01", "IT01"), rows=0))
    assert point(ledger, "IT01").valid_as_of == at(21)
    assert point(ledger, "DE01").valid_as_of == at(20)
    assert point(ledger, "US01").state is State.UNKNOWN
    assert ledger.claims(at(21)).state is State.UNKNOWN


def test_changed_only_windows_split_only_their_intersection_with_known_regions():
    c = contract()
    ledger = full(c)
    ledger = ledger.apply(Manifest(c.table, "daily", at(19), window=(1, 10)))
    ledger = ledger.apply(Manifest(c.table, "recent", at(20), window=(5, 15), since=at(18)))
    for value, complete, valid in [
        (0, 18, 18),
        (4, 19, 19),
        (5, 20, 19),
        (10, 20, 19),
        (15, 20, 18),
        (16, 18, 18),
    ]:
        claim = point(ledger, value)
        assert (claim.complete_as_of, claim.valid_as_of) == (at(complete), at(valid))


def test_a_changed_only_window_cannot_patch_an_undelivered_part_of_its_coverage():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(18), window=(1, 10)))
    with pytest.raises(RejectedDelivery, match="known base throughout"):
        ledger.apply(Manifest(c.table, "recent", at(19), window=(5, 15), since=at(18)))


def test_a_change_feed_with_tombstones_keeps_a_clean_base_complete():
    c = contract(shapes={"full": FULL, "feed": FEED})
    ledger = full(c, generation="g1")
    ledger = ledger.apply(Manifest(c.table, "feed", at(19), since="g1", generation="g2", tombstones=((7,),)))
    assert ledger.generation == "g2"
    assert ledger.claims(at(19)).state is State.COMPLETE
    assert ledger.claims(at(19)).valid_as_of == at(19)


def test_a_source_restore_breaks_the_generation_chain_until_a_full_delivery_recovers_it():
    c = contract(shapes={"full": FULL, "feed": FEED})
    ledger = full(c, generation="g41")
    with pytest.raises(RejectedDelivery, match="generation chain"):
        ledger.apply(Manifest(c.table, "feed", at(19), since="backup-g3", generation="backup-g4"))
    ledger = ledger.apply(Manifest(c.table, "full", at(20), generation="restored-g1"))
    ledger = ledger.apply(Manifest(c.table, "feed", at(21), since="restored-g1", generation="restored-g2"))
    assert ledger.claims(at(21)).valid_as_of == at(21)


def test_a_feed_arriving_before_any_base_is_refused():
    c = contract()
    with pytest.raises(RejectedDelivery, match="known base"):
        Ledger(c).apply(Manifest(c.table, "feed", at(19), since="g1", generation="g2"))


def test_tombstones_after_an_interval_without_deletes_do_not_repair_earlier_missed_deletions():
    c = contract(shapes={"full": FULL, "since": SINCE, "feed": FEED})
    ledger = full(c, generation="g1")
    ledger = ledger.apply(Manifest(c.table, "since", at(19), since=at(18), generation="g2"))
    ledger = ledger.apply(Manifest(c.table, "feed", at(20), since="g2", generation="g3"))
    assert ledger.claims(at(20)).state is State.PATCHED
    assert ledger.claims(at(20)).valid_as_of == at(18)


def test_a_timestamp_gap_cannot_raise_completeness_but_overlapping_upserts_can():
    c = contract(shapes={"full": FULL, "since": SINCE})
    ledger = full(c)
    with pytest.raises(RejectedDelivery, match="gap"):
        ledger.apply(Manifest(c.table, "since", at(20), since=at(19)))
    ledger = ledger.apply(Manifest(c.table, "since", at(20), since=at(17)))
    assert ledger.claims(at(20)).complete_as_of == at(20)


def test_a_full_export_without_a_cursor_cannot_continue_an_old_feed_generation():
    c = contract(shapes={"full": FULL, "feed": FEED})
    ledger = full(c, generation="g1").apply(Manifest(c.table, "full", at(19)))
    assert ledger.generation is None
    with pytest.raises(RejectedDelivery, match="generation chain"):
        ledger.apply(Manifest(c.table, "feed", at(20), since="g1", generation="g2"))


def test_append_only_logs_without_keys_start_at_declared_history_and_refuse_overlaps():
    c = Contract("events", (), {"log": SINCE}, start_at=at(1))
    ledger = Ledger(c).apply(Manifest(c.table, "log", at(18), since=at(1)))
    ledger = ledger.apply(Manifest(c.table, "log", at(19), since=at(18)))
    assert ledger.claims(at(19)).valid_as_of == at(19)
    for since in (at(17), at(19, 1)):
        with pytest.raises(RejectedDelivery, match="contiguous"):
            ledger.apply(Manifest(c.table, "log", at(20), since=since))


def test_the_freshness_limit_expires_each_region_at_its_own_boundary():
    ledger = full(contract())
    ledger = ledger.apply(Manifest(ledger.contract.table, "daily", at(24), window=(10, 20)))
    assert ledger.claims(at(25)).state is State.COMPLETE
    assert ledger.claims(at(25, 1)).state is State.EXPIRED
    assert point(ledger, 15, at(25, 1)).state is State.COMPLETE


@pytest.mark.parametrize("shape,kwargs", [("full", {}), ("since", {"since": at(17)})])
def test_a_manifest_produced_before_the_region_dates_cannot_change_rows_or_evidence(shape, kwargs):
    ledger = full(contract())
    with pytest.raises(RejectedDelivery, match="later"):
        ledger.apply(Manifest(ledger.contract.table, shape, at(17), **kwargs))


def test_a_surface_reading_three_tables_inherits_each_tables_weakest_region():
    stocks = full(contract("stock"), 20)
    invoices = full(contract("invoices"), 18)
    invoices = invoices.apply(Manifest("invoices", "since", at(21), since=at(18)))
    plants = full(contract("plants"), 19)
    claims = Claims.combine(*(ledger.claims(at(21)) for ledger in (stocks, invoices, plants)))
    assert (claims.state, claims.complete_as_of, claims.valid_as_of) == (State.PATCHED, at(19), at(18))
    unknown = Claims.combine(claims, Ledger(contract("missing")).claims(at(21)))
    assert unknown.state is State.UNKNOWN
    assert unknown.complete_as_of is None
    assert len(unknown.in_state(State.UNKNOWN)) == 1


def test_a_generation_window_cannot_advance_a_region_whose_cursor_it_skipped():
    from dogana import Checkpoint, Completeness, Coverage, Deletions, Shape

    window_feed = Shape(
        Coverage.WINDOW, Completeness.CHANGED_ONLY, Deletions.TOMBSTONE, Checkpoint.GENERATION
    )
    c = contract(shapes={"full": FULL, "feed_window": window_feed})
    ledger = full(c, generation="g1")
    ledger = ledger.apply(
        Manifest(c.table, "feed_window", at(19), window=(1, 10), since="g1", generation="g2")
    )
    with pytest.raises(RejectedDelivery, match="generation chain"):
        ledger.apply(Manifest(c.table, "feed_window", at(20), window=(5, 20), since="g2", generation="g3"))
    ledger = ledger.apply(
        Manifest(c.table, "feed_window", at(20), window=(1, 10), since="g2", generation="g3")
    )
    assert point(ledger, 5).valid_as_of == at(20)
    assert point(ledger, 15).valid_as_of == at(18)


@pytest.mark.parametrize(
    "windows",
    [
        ((1, 9), (10, 20)),
        ((at(18).date(), at(19).date()), (at(20).date(), at(21).date())),
        ((at(18), at(19)), (at(19) + timedelta(microseconds=1), at(20))),
    ],
)
def test_adjacent_discrete_windows_leave_no_phantom_unknown_region(windows):
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(22), window=windows[0]))
    ledger = ledger.apply(Manifest(c.table, "daily", at(23), window=windows[1]))
    claim = ledger.claims(at(23), window=(windows[0][0], windows[1][1]))
    assert claim.state is State.COMPLETE
    assert claim.complete_as_of == at(22)
    assert len(claim.regions) == 2
