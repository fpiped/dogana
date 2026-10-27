from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from dogana import Claims, Contract, InvalidContract, Ledger, Manifest, RejectedDelivery, State
from tests.support import FEED, FULL, SINCE, WINDOW, at
from tests.support import accounting_contract as contract


def full(c, day=18, generation=None):
    return Ledger(c).apply(Manifest(c.table, "full", at(day), generation=generation))


def point(ledger, value, now=None):
    return ledger.claims(now or at(25), window=(value, value))


def test_nightly_full_exports_replace_the_entire_domain_with_current_evidence():
    c = contract(shapes={"full": FULL})
    ledger = full(c).apply(Manifest(c.table, "full", at(19)))
    claim = ledger.claims(at(19))
    assert (claim.state, claim.complete_as_of, claim.deletions_as_of) == (State.COMPLETE, at(19), at(19))
    assert len(ledger.regions) == 1


def test_weekly_full_plus_changed_since_without_deletes_stays_patched_until_sunday():
    c = contract(shapes={"full": FULL, "since": SINCE})
    ledger = full(c)
    for day in range(19, 25):
        ledger = ledger.apply(Manifest(c.table, "since", at(day), since=at(day - 1)))
        claim = ledger.claims(at(day))
        assert (claim.state, claim.complete_as_of, claim.deletions_as_of) == (State.PATCHED, at(day), at(18))
    ledger = ledger.apply(Manifest(c.table, "full", at(25)))
    assert ledger.claims(at(25)).deletions_as_of == at(25)


def test_weekly_full_plus_daily_thirty_day_windows_keeps_old_dates_outside_the_window():
    c = contract(window_type="date")
    ledger = full(c)
    for day in range(19, 25):
        low, high = (at(day) - timedelta(days=30)).date(), at(day).date()
        ledger = ledger.apply(Manifest(c.table, "daily", at(day), window=(low, high)))
        assert point(ledger, high, at(day)).deletions_as_of == at(day)
        assert ledger.claims(at(day)).complete_as_of == at(18)
        assert ledger.claims(at(day)).deletions_as_of == at(18)


def test_sliding_daily_windows_split_earlier_regions_and_leave_unknown_history_visible():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(20), window=(1, 30)))
    ledger = ledger.apply(Manifest(c.table, "daily", at(21), window=(2, 31)))
    assert [(r.low, r.high, r.low_open, r.high_open) for r in ledger.regions] == [
        (None, 1, False, True),
        (1, 2, False, True),
        (2, 32, False, True),
        (32, None, False, False),
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
    assert point(ledger, 10).deletions_as_of == at(21)
    assert point(ledger, 1).state is State.UNKNOWN


def test_an_older_disjoint_window_can_establish_previously_unknown_history():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(21), window=(10, 20)))
    ledger = ledger.apply(Manifest(c.table, "daily", at(20), window=(1, 9)))
    assert point(ledger, 1).complete_as_of == at(20)
    assert point(ledger, 10).complete_as_of == at(21)


def test_repeated_window_deliveries_require_a_store_receipt_lookup_before_accounting():
    c = contract()
    first = Manifest(c.table, "daily", at(20), window=(1, 30))
    ledger = Ledger(c).apply(first)
    with pytest.raises(RejectedDelivery, match="later"):
        ledger.apply(first)
    ledger = ledger.apply(Manifest(c.table, "daily", at(21), window=(2, 31)))
    with pytest.raises(RejectedDelivery, match="later"):
        ledger.apply(first)
    assert not hasattr(ledger, "accepted")


def test_independent_plant_exports_include_unknown_plants_and_empty_decommissioned_plants():
    c = contract(window_type="str")
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(18), window=("IT01", "IT01"), rows=50))
    ledger = ledger.apply(Manifest(c.table, "daily", at(20), window=("DE01", "DE01"), rows=20))
    ledger = ledger.apply(Manifest(c.table, "daily", at(21), window=("IT01", "IT01"), rows=0))
    assert point(ledger, "IT01").deletions_as_of == at(21)
    assert point(ledger, "DE01").deletions_as_of == at(20)
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
        assert (claim.complete_as_of, claim.deletions_as_of) == (at(complete), at(valid))


def test_a_changed_only_window_cannot_patch_an_undelivered_part_of_its_coverage():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(18), window=(1, 10)))
    with pytest.raises(RejectedDelivery, match="known base throughout"):
        ledger.apply(Manifest(c.table, "recent", at(19), window=(5, 15), since=at(18)))


def test_a_change_feed_with_tombstones_keeps_a_clean_base_complete():
    c = contract(shapes={"full": FULL, "feed": FEED})
    ledger = full(c, generation="g1")
    ledger = ledger.apply(Manifest(c.table, "feed", at(19), since="g1", generation="g2", tombstones=1))
    assert ledger.generation == "g2"
    assert ledger.claims(at(19)).state is State.COMPLETE
    assert ledger.claims(at(19)).deletions_as_of == at(19)


def test_a_source_restore_breaks_the_generation_chain_until_a_full_delivery_recovers_it():
    c = contract(shapes={"full": FULL, "feed": FEED})
    ledger = full(c, generation="g41")
    with pytest.raises(RejectedDelivery, match="generation chain"):
        ledger.apply(Manifest(c.table, "feed", at(19), since="backup-g3", generation="backup-g4"))
    ledger = ledger.apply(Manifest(c.table, "full", at(20), generation="restored-g1"))
    ledger = ledger.apply(Manifest(c.table, "feed", at(21), since="restored-g1", generation="restored-g2"))
    assert ledger.claims(at(21)).deletions_as_of == at(21)


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
    assert ledger.claims(at(20)).deletions_as_of == at(18)


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
    assert ledger.claims(at(19)).deletions_as_of == at(19)
    for since in (at(17), at(19, 1)):
        with pytest.raises(RejectedDelivery, match="contiguous"):
            ledger.apply(Manifest(c.table, "log", at(20), since=since))


def test_a_keyless_table_delivered_whole_proves_both_dates_and_restarts_its_append_history():
    c = Contract("receipts", (), {"full": FULL, "log": SINCE}, start_at=at(1))
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    assert ledger.claims(at(18)).deletions_as_of == at(18)
    with pytest.raises(RejectedDelivery, match="contiguous"):
        ledger.apply(Manifest(c.table, "log", at(19), since=at(1)))
    ledger = ledger.apply(Manifest(c.table, "log", at(19), since=at(18)))
    assert ledger.claims(at(19)).complete_as_of == at(19)


def test_a_keyless_append_after_a_window_is_refused_until_a_full_delivery_aligns_the_boundaries():
    c = Contract(
        "events",
        (),
        {"full": FULL, "bucket": replace(WINDOW, window_size=None), "log": SINCE},
        window_column="id",
        types={"id": "int"},
        start_at=at(1),
    )
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18, 1)))
    ledger = ledger.apply(Manifest(c.table, "bucket", at(18, 2), window=(1, 1)))
    with pytest.raises(RejectedDelivery, match="contiguous"):
        ledger.apply(Manifest(c.table, "log", at(18, 3), since=at(18, 1)))
    ledger = ledger.apply(Manifest(c.table, "full", at(18, 3)))
    assert (
        ledger.apply(Manifest(c.table, "log", at(18, 4), since=at(18, 3))).claims(at(18, 4)).state
        is State.COMPLETE
    )


def test_an_empty_append_interval_cannot_carry_rows():
    c = Contract("events", (), {"log": SINCE}, start_at=at(1))
    with pytest.raises(InvalidContract, match="empty interval"):
        Manifest(c.table, "log", at(1), since=at(1), rows=1).check(c)
    assert Ledger(c).apply(Manifest(c.table, "log", at(1), since=at(1), rows=0)).publication_id


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
    assert (claims.state, claims.complete_as_of, claims.deletions_as_of) == (State.PATCHED, at(19), at(18))
    unknown = Claims.combine(claims, Ledger(contract("missing")).claims(at(21)))
    assert unknown.state is State.UNKNOWN
    assert unknown.complete_as_of is None
    assert len(unknown.in_state(State.UNKNOWN)) == 1


def test_window_deliveries_preserve_the_table_cursor_for_the_next_global_feed():
    c = contract()
    ledger = full(c, generation="g1")
    ledger = ledger.apply(Manifest(c.table, "daily", at(19), window=(1, 10)))
    assert ledger.generation == "g1"
    assert not hasattr(ledger.regions[0], "generation")
    ledger = ledger.apply(Manifest(c.table, "feed", at(20), since="g1", generation="g2"))
    assert ledger.generation == "g2"
    assert ledger.claims(at(20)).deletions_as_of == at(20)


@pytest.mark.parametrize(
    "windows",
    [
        ((1, 9), (10, 20)),
        ((at(18).date(), at(19).date()), (at(20).date(), at(21).date())),
        ((at(18), at(19)), (at(19) + timedelta(microseconds=1), at(20))),
    ],
)
def test_adjacent_discrete_windows_leave_no_phantom_unknown_region(windows):
    c = contract(window_type=type(windows[0][0]).__name__)
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(22), window=windows[0]))
    ledger = ledger.apply(Manifest(c.table, "daily", at(23), window=windows[1]))
    claim = ledger.claims(at(23), window=(windows[0][0], windows[1][1]))
    assert claim.state is State.COMPLETE
    assert claim.complete_as_of == at(22)
    assert len(claim.regions) == 2


def test_datetime_bounds_are_normalised_to_utc_so_a_dst_fold_is_two_instants():
    from zoneinfo import ZoneInfo

    rome = ZoneInfo("Europe/Rome")
    early = datetime(2026, 10, 25, 2, 0, tzinfo=rome, fold=0)
    late = datetime(2026, 10, 25, 2, 0, tzinfo=rome, fold=1)
    c = Contract(
        "ticks", ("id",), {"slot": WINDOW}, window_column="at", types={"id": "int", "at": "datetime"}
    )
    ledger = Ledger(c).apply(Manifest(c.table, "slot", at(26), window=(early, early)))
    assert ledger.claims(at(26), window=(early, early)).state is State.COMPLETE
    assert ledger.claims(at(26), window=(late, late)).state is State.UNKNOWN
    assert Manifest.from_json(Manifest(c.table, "slot", at(26), window=(late, late)).to_json()).window == (
        late.astimezone(UTC),
        late.astimezone(UTC),
    )


def test_window_bounds_must_have_the_window_columns_declared_type():

    c = Contract(
        "orders",
        ("id",),
        {"daily": WINDOW},
        window_column="day",
        types={"id": "int", "day": "date"},
    )
    with pytest.raises(InvalidContract, match="window bound types"):
        Manifest(c.table, "daily", at(19), window=("2026-10-01", "2026-10-19")).check(c)


@pytest.mark.parametrize("since,expected_valid", [(17, 20), (18, 20), (19, 18)])
def test_timestamp_tombstones_repair_patch_deletion_debt_only_when_the_checkpoint_covers_it(
    since, expected_valid
):
    from dogana import Shape

    tombstones = Shape(
        coverage="all", completeness="changed_only", deletions="tombstone", checkpoint="timestamp"
    )
    c = contract(shapes={"full": FULL, "since": SINCE, "deletes": tombstones})
    ledger = full(c).apply(Manifest(c.table, "since", at(19), since=at(18)))
    ledger = ledger.apply(Manifest(c.table, "deletes", at(20), since=at(since)))
    assert ledger.claims(at(20)).deletions_as_of == at(expected_valid)


def test_global_generation_tombstones_cannot_invent_evidence_for_skipped_deletions():
    from dogana import Shape

    feed = Shape(coverage="all", completeness="changed_only", deletions="none", checkpoint="generation")
    c = contract(shapes={"full": FULL, "patch": feed, "feed": FEED})
    ledger = full(c, generation="g1")
    ledger = ledger.apply(Manifest(c.table, "patch", at(19), since="g1", generation="g2"))
    ledger = ledger.apply(Manifest(c.table, "feed", at(20), since="g2", generation="g3"))
    assert ledger.claims(at(20)).deletions_as_of == at(18)


def test_typed_window_queries_refuse_a_different_domain_even_before_the_first_delivery():

    c = Contract(
        "stock",
        ("id",),
        {"daily": WINDOW},
        window_column="day",
        types={"id": "int", "day": "date"},
    )
    with pytest.raises(InvalidContract, match="window bound types"):
        Ledger(c).claims(at(18), window=(1, 10))


def test_dataclass_replacement_preserves_cuts_unless_regions_are_explicitly_replaced():
    from dataclasses import replace

    from dogana import Region

    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(19), window=(1, 9)))
    changed = replace(ledger, generation="next")
    assert changed.regions == ledger.regions
    assert changed.publication_id == ledger.publication_id
    assert changed.generation == "next"
    assert replace(ledger, regions=(Region(),)).regions == (Region(),)
    assert Ledger.from_json(changed.to_json()) == changed
    assert set(changed.to_dict()) == {
        "kind",
        "version",
        "contract",
        "regions",
        "generation",
        "publication_id",
    }


def test_equivalent_region_endpoint_spellings_have_the_same_ledger_evidence():
    from dogana import Region

    c = contract()
    closed = Ledger(
        c, (Region(high=1, high_open=True), Region(1, 9, at(19), at(19)), Region(low=9, low_open=True))
    )
    half_open = Ledger(
        c, (Region(high=1, high_open=True), Region(1, 10, at(19), at(19), high_open=True), Region(low=10))
    )
    assert closed == half_open
    assert closed.regions == half_open.regions


@pytest.mark.parametrize(
    "table,shape,options",
    [
        ("other", "full", {}),
        ("moves", "missing", {}),
        ("moves", "full", {"window": (1, 9)}),
        ("moves", "since", {}),
        ("moves", "since", {"since": "cursor"}),
        ("moves", "since", {"since": at(20)}),
    ],
)
def test_accounting_refusals_name_the_delivery_and_its_bounds(table, shape, options):
    ledger = full(contract())
    manifest = Manifest(table, shape, at(19), **options)
    with pytest.raises(RejectedDelivery) as rejected:
        ledger.apply(manifest)
    detail = str(rejected.value)
    assert f"table={table!r}" in detail
    assert f"shape={shape!r}" in detail
    assert "produced_at=2026-10-19T00:00:00+00:00" in detail
    assert "since=" in detail and "window=" in detail


def test_an_accounting_window_with_the_wrong_scalar_domain_is_a_delivery_refusal():
    ledger = Ledger(contract()).apply(Manifest("moves", "daily", at(18), window=(1, 9)))
    with pytest.raises(RejectedDelivery, match="window=.*contract types"):
        ledger.apply(Manifest("moves", "daily", at(19), window=("a", "b")))


def test_keyless_append_preserves_or_replaces_an_issued_table_generation():
    c = Contract("events", (), {"log": SINCE}, start_at=at(1))
    ledger = Ledger(c).apply(Manifest(c.table, "log", at(18), since=at(1), generation="g1"))
    ledger = ledger.apply(Manifest(c.table, "log", at(19), since=at(18)))
    assert ledger.generation == "g1"
    ledger = ledger.apply(Manifest(c.table, "log", at(20), since=at(19), generation="g2"))
    assert ledger.generation == "g2"


def accepted_receipt(ledger, manifest):
    from dogana import Check, Gate

    checks = tuple(Check(r.name, True) for r in Gate.requirements(ledger.contract, manifest))
    receipt = Gate.evaluate(ledger, manifest, checks, now=at(25))
    assert receipt.accepted
    return receipt


def receipt_chain():
    c = contract()
    first = accepted_receipt(Ledger(c), Manifest(c.table, "full", at(18)))
    second = accepted_receipt(first.ledger, Manifest(c.table, "daily", at(19), window=(1, 9)))
    return c, first, second


@pytest.mark.parametrize("missing", [False, True])
def test_reconciliation_of_a_fresh_store_needs_no_receipts(missing):
    c = contract()
    current = None if missing else Ledger(c)
    for repair in (False, True):
        assert Ledger.reconcile(c, (), current, None, repair=repair) == Ledger(c)


def test_reconciliation_of_matching_evidence_does_not_require_repair():
    c, first, second = receipt_chain()
    for repair in (False, True):
        assert (
            Ledger.reconcile(c, iter((first, second)), second.ledger, second.publication_id, repair=repair)
            == second.ledger
        )


@pytest.mark.parametrize("current_kind", ["missing", "initial", "behind"])
def test_stamp_ahead_diagnosis_requires_repair_and_reconstructs_exactly_the_stamped_head(current_kind):
    from dogana.errors import StampAhead

    c, first, second = receipt_chain()
    current = {"missing": None, "initial": Ledger(c), "behind": first.ledger}[current_kind]
    with pytest.raises(StampAhead, match="ahead"):
        Ledger.reconcile(c, (first, second), current, second.publication_id, repair=False)
    assert Ledger.reconcile(c, (first, second), current, second.publication_id) == second.ledger


@pytest.mark.parametrize("stamp_kind", ["missing", "behind"])
@pytest.mark.parametrize("missing_ledger", [False, True])
def test_reconciliation_never_moves_the_rows_forward_to_a_later_receipt(stamp_kind, missing_ledger):
    from dogana.errors import LedgerAhead

    c, first, second = receipt_chain()
    stamp = None if stamp_kind == "missing" else first.publication_id
    for repair in (False, True):
        with pytest.raises(LedgerAhead):
            Ledger.reconcile(
                c, (first, second), None if missing_ledger else second.ledger, stamp, repair=repair
            )


@pytest.mark.parametrize(
    "problem", ["missing", "gap", "reordered", "duplicate", "foreign", "unrelated_stamp"]
)
def test_reconciliation_refuses_an_unprovable_stamp(problem):
    from dogana.errors import StampAhead

    c, first, second = receipt_chain()
    other = contract("other")
    foreign = accepted_receipt(Ledger(other), Manifest(other.table, "full", at(18)))
    receipts = {
        "missing": (),
        "gap": (second,),
        "reordered": (second, first),
        "duplicate": (first, first),
        "foreign": (foreign,),
        "unrelated_stamp": (first, second),
    }[problem]
    stamp = "unrelated" if problem == "unrelated_stamp" else second.publication_id
    for repair in (False, True):
        with pytest.raises(StampAhead):
            Ledger.reconcile(c, receipts, None, stamp, repair=repair)


@pytest.mark.parametrize("problem", ["unrelated_head", "wrong_evidence", "initial_with_evidence"])
def test_reconciliation_refuses_current_evidence_that_does_not_belong_to_the_chain(problem):
    from dataclasses import replace

    from dogana.errors import StampAhead

    c, first, second = receipt_chain()
    current = {
        "unrelated_head": replace(first.ledger, publication_id="unrelated"),
        "wrong_evidence": replace(second.ledger, generation="invented"),
        "initial_with_evidence": replace(first.ledger, publication_id=None),
    }[problem]
    for repair in (False, True):
        with pytest.raises(StampAhead):
            Ledger.reconcile(c, (first, second), current, second.publication_id, repair=repair)


def test_reconstruction_checks_historical_region_spellings_by_evidence():
    from dataclasses import replace

    from dogana import Receipt, Region

    c = contract()
    first = accepted_receipt(Ledger(c), Manifest(c.table, "daily", at(18), window=(1, 9)))
    historical = (Region(high=1, high_open=True), Region(1, 9, at(18), at(18)), Region(low=9, low_open=True))
    first = replace(first, transition=replace(first.transition, after=historical))
    second = accepted_receipt(first.ledger, Manifest(c.table, "daily", at(19), window=(5, 12)))
    second = replace(second, transition=replace(second.transition, before=historical))
    receipts = tuple(Receipt.from_json(receipt.to_json()) for receipt in (first, second))
    assert Ledger.from_receipts(c, receipts) == second.ledger
    assert Ledger.reconcile(c, receipts, None, second.publication_id) == second.ledger


def test_a_store_verified_retained_head_certifies_a_consistent_read_without_loading_history():
    c, first, second = receipt_chain()

    def unavailable_history():
        raise AssertionError("consistent reads must not load the receipt prefix")
        yield first

    assert (
        Ledger.reconcile(
            c,
            unavailable_history(),
            second.ledger,
            second.publication_id,
            repair=False,
            retained_head=second,
        )
        == second.ledger
    )


def test_a_lone_retained_head_cannot_replace_full_history_during_repair():
    from dogana.errors import StampAhead

    c, first, second = receipt_chain()
    with pytest.raises(StampAhead, match="receipt chain"):
        Ledger.reconcile(c, (second,), second.ledger, second.publication_id, retained_head=second)
    with pytest.raises(StampAhead, match="receipt chain"):
        Ledger.reconcile(
            c, (second,), first.ledger, second.publication_id, repair=False, retained_head=second
        )


@pytest.mark.parametrize("problem", ["stale", "parent", "manifest", "evidence", "rejected"])
def test_a_retained_head_must_match_the_rows_cached_ledger_and_its_own_hash(problem):
    from dataclasses import replace

    from dogana import Gate
    from dogana.errors import StampAhead

    c, first, second = receipt_chain()
    head = {
        "stale": first,
        "parent": replace(second, base_publication_id="unrelated"),
        "manifest": replace(second, manifest=replace(second.manifest, produced_at=at(20))),
        "evidence": replace(second, ledger=replace(second.ledger, generation="invented")),
        "rejected": Gate.evaluate(second.ledger, Manifest(c.table, "full", at(17)), now=at(25)),
    }[problem]
    with pytest.raises(StampAhead, match="retained accepted head"):
        Ledger.reconcile(c, (), second.ledger, second.publication_id, repair=False, retained_head=head)


def test_a_retained_head_uses_its_historical_contract_for_the_publication_hash():
    from dataclasses import replace

    c, first, second = receipt_chain()
    evolved = replace(c, freshness=timedelta(days=1))
    current = second.ledger.with_contract(evolved)
    assert (
        Ledger.reconcile(
            evolved,
            (),
            current,
            second.publication_id,
            repair=False,
            retained_head=second,
        )
        == current
    )
