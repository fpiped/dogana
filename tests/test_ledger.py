from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from dogana import Claims, Contract, Group, InvalidContract, Ledger, Manifest, RejectedDelivery, Shape, State
from tests.support import FEED, FULL, SINCE, WINDOW, at, fold, part_of
from tests.support import accounting_contract as contract


def full(c, day=18, generation=None):
    return Ledger(c).apply(Manifest.single(c.table, "full", at(day), generation=generation))


def point(ledger, value, now=None, limit=timedelta.max):
    return ledger.claims(now or at(25), limit=limit, window=(value, value))


@pytest.mark.protocol("transitions.full", "claims.complete")
def test_nightly_full_exports_replace_the_entire_domain_with_current_evidence():
    c = contract(shapes={"full": FULL})
    ledger = full(c).apply(Manifest.single(c.table, "full", at(19)))
    claim = ledger.claims(at(19), limit=timedelta.max)
    assert (claim.state, claim.complete_as_of, claim.deletions_as_of) == (State.COMPLETE, at(19), at(19))
    assert len(ledger.regions) == 1


@pytest.mark.protocol("transitions.timestamp-upserts", "claims.patched", "ledger.meaning")
def test_weekly_full_plus_changed_since_without_deletes_stays_patched_until_sunday():
    c = contract(shapes={"full": FULL, "since": SINCE})
    ledger = full(c)
    for day in range(19, 25):
        ledger = ledger.apply(Manifest.single(c.table, "since", at(day), since=at(day - 1)))
        claim = ledger.claims(at(day), limit=timedelta.max)
        assert (claim.state, claim.complete_as_of, claim.deletions_as_of) == (State.PATCHED, at(day), at(18))
    ledger = ledger.apply(Manifest.single(c.table, "full", at(25)))
    assert ledger.claims(at(25), limit=timedelta.max).deletions_as_of == at(25)


@pytest.mark.protocol("transitions.window", "claims.1")
def test_weekly_full_plus_daily_thirty_day_windows_keeps_old_dates_outside_the_window():
    c = contract(window_type="date")
    ledger = full(c)
    for day in range(19, 25):
        low, high = (at(day) - timedelta(days=30)).date(), at(day).date()
        ledger = ledger.apply(Manifest.single(c.table, "daily", at(day), window=(low, high)))
        assert point(ledger, high, at(day)).deletions_as_of == at(day)
        assert ledger.claims(at(day), limit=timedelta.max).complete_as_of == at(18)
        assert ledger.claims(at(day), limit=timedelta.max).deletions_as_of == at(18)


@pytest.mark.protocol("ledger.structure", "ledger.accounting", "claims.unknown", "claims.1")
def test_sliding_daily_windows_split_earlier_regions_and_leave_unknown_history_visible():
    c = contract()
    ledger = Ledger(c).apply(Manifest.single(c.table, "daily", at(20), window=(1, 30)))
    ledger = ledger.apply(Manifest.single(c.table, "daily", at(21), window=(2, 31)))
    assert [(r.low, r.high, r.low_open, r.high_open) for r in ledger.regions] == [
        (None, 1, False, True),
        (1, 2, False, True),
        (2, 32, False, True),
        (32, None, False, False),
    ]
    assert point(ledger, 1).complete_as_of == at(20)
    assert point(ledger, 2).complete_as_of == at(21)
    assert ledger.claims(at(21), limit=timedelta.max).state is State.UNKNOWN
    assert ledger.claims(at(21), limit=timedelta.max).complete_as_of is None


@pytest.mark.protocol("transitions.window", "ledger.accounting")
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
        ledger = ledger.apply(Manifest.single(c.table, "daily", at(day), window=window))
        expected.update({n: at(day) for n in range(window[0], window[1] + 1)})
        for n in range(0, 32):
            assert point(ledger, n).complete_as_of == expected.get(n)
        assert all(a.touches(b) for a, b in zip(ledger.regions, ledger.regions[1:], strict=False))


@pytest.mark.protocol("transitions.2")
def test_an_older_window_overlapping_newer_evidence_is_refused_as_a_whole():
    c = contract()
    ledger = Ledger(c).apply(Manifest.single(c.table, "daily", at(21), window=(10, 20)))
    before = ledger.to_json()
    with pytest.raises(RejectedDelivery, match="later"):
        ledger.apply(Manifest.single(c.table, "daily", at(20), window=(1, 15)))
    assert ledger.to_json() == before
    assert point(ledger, 10).deletions_as_of == at(21)
    assert point(ledger, 1).state is State.UNKNOWN


@pytest.mark.protocol("transitions.window", "ledger.accounting")
def test_an_older_disjoint_window_can_establish_previously_unknown_history():
    c = contract()
    ledger = Ledger(c).apply(Manifest.single(c.table, "daily", at(21), window=(10, 20)))
    ledger = ledger.apply(Manifest.single(c.table, "daily", at(20), window=(1, 9)))
    assert point(ledger, 1).complete_as_of == at(20)
    assert point(ledger, 10).complete_as_of == at(21)


@pytest.mark.protocol("algebraic-guarantees.laws", "ledger.structure")
def test_repeated_window_deliveries_require_a_store_receipt_lookup_before_accounting():
    c = contract()
    first = Manifest.single(c.table, "daily", at(20), window=(1, 30))
    ledger = Ledger(c).apply(first)
    with pytest.raises(RejectedDelivery, match="later"):
        ledger.apply(first)
    ledger = ledger.apply(Manifest.single(c.table, "daily", at(21), window=(2, 31)))
    with pytest.raises(RejectedDelivery, match="later"):
        ledger.apply(first)
    assert not hasattr(ledger, "accepted")


@pytest.mark.protocol("contract.domains", "claims.unknown")
def test_independent_plant_exports_include_unknown_plants_and_empty_decommissioned_plants():
    c = contract(window_type="str")
    ledger = Ledger(c).apply(Manifest.single(c.table, "daily", at(18), window=("IT01", "IT01"), rows=50))
    ledger = ledger.apply(Manifest.single(c.table, "daily", at(20), window=("DE01", "DE01"), rows=20))
    ledger = ledger.apply(Manifest.single(c.table, "daily", at(21), window=("IT01", "IT01"), rows=0))
    assert point(ledger, "IT01").deletions_as_of == at(21)
    assert point(ledger, "DE01").deletions_as_of == at(20)
    assert point(ledger, "US01").state is State.UNKNOWN
    assert ledger.claims(at(21), limit=timedelta.max).state is State.UNKNOWN


@pytest.mark.protocol("transitions.timestamp-upserts")
def test_changed_only_windows_split_only_their_intersection_with_known_regions():
    c = contract()
    ledger = full(c)
    ledger = ledger.apply(Manifest.single(c.table, "daily", at(19), window=(1, 10)))
    ledger = ledger.apply(Manifest.single(c.table, "recent", at(20), window=(5, 15), since=at(18)))
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


@pytest.mark.protocol("transitions.3")
def test_a_changed_only_window_cannot_patch_an_undelivered_part_of_its_coverage():
    c = contract()
    ledger = Ledger(c).apply(Manifest.single(c.table, "daily", at(18), window=(1, 10)))
    with pytest.raises(RejectedDelivery, match="known base throughout"):
        ledger.apply(Manifest.single(c.table, "recent", at(19), window=(5, 15), since=at(18)))


@pytest.mark.protocol("manifest.2", "transitions.6", "transitions.sequence")
@pytest.mark.parametrize(
    "since,through,accepted", [(10, 12, True), (9, 12, True), (9, 10, True), (11, 12, False), (8, 9, False)]
)
def test_sequence_reads_continue_at_or_before_the_held_position_without_gaps_or_rewinds(
    since, through, accepted
):
    from dogana import Checkpoint

    c = Contract(
        "moves",
        ("id",),
        {"full": FULL, "changes": Shape.feed(checkpoint=Checkpoint.SEQUENCE)},
        types={"id": "int", "deleted": "bool"},
        deleted_column="deleted",
    )
    ledger = full(c, generation=10)
    manifest = Manifest.single(c.table, "changes", at(19), since=since, generation=through)
    if accepted:
        after = ledger.apply(manifest)
        assert after.generation == through
        assert after.claims(at(19), limit=timedelta.max).exact_as_of == at(19)
    else:
        with pytest.raises(RejectedDelivery, match="sequence_chain: a sequence read must continue"):
            ledger.apply(manifest)


@pytest.mark.parametrize("problem", ["time", "sequence", "since", "append", "gap"])
def test_refused_deliveries_name_the_exact_guard_and_delivery_context(problem):
    from dogana import Checkpoint

    if problem == "sequence":
        c = Contract(
            "moves",
            ("id",),
            {"full": FULL, "changes": Shape.feed(checkpoint=Checkpoint.SEQUENCE)},
            types={"id": "int", "deleted": "bool"},
            deleted_column="deleted",
        )
        ledger = full(c, generation=10)
        manifest = Manifest.single(c.table, "changes", at(19), since=11, generation=12)
        detail = (
            "sequence_chain: a sequence read must continue the table's position without a gap; "
            "request a complete delivery"
        )
    elif problem == "append":
        c = Contract("events", (), {"log": SINCE}, start_at=at(1))
        ledger = Ledger(c).apply(Manifest.single(c.table, "log", at(18), since=at(1)))
        manifest = Manifest.single(c.table, "log", at(20), since=at(17))
        detail = "append_contiguous: append-only intervals must be contiguous without overlap"
    else:
        c = contract(shapes={"full": FULL, "since": SINCE})
        ledger = full(c)
        if problem == "time":
            manifest = Manifest.single(c.table, "full", at(18))
            detail = "monotone_time: delivery time must be later than every affected region"
        else:
            manifest = Manifest.single(
                c.table, "since", at(20), since=at(21) if problem == "since" else at(19)
            )
            detail = (
                "since_before_boundary: since is after the source read boundary"
                if problem == "since"
                else "no_gap_after_base: timestamp checkpoint leaves a gap after the base"
            )
    with pytest.raises(RejectedDelivery) as error:
        ledger.apply(manifest)
    part = part_of(manifest)
    assert str(error.value) == (
        f"table={c.table!r} produced_at={manifest.produced_at.isoformat()} "
        f"shape={part.shape!r} since={part.since} window={part.window!r}: {detail}"
    )


@pytest.mark.protocol("transitions.generation-tombstones-exact")
def test_a_change_feed_with_tombstones_keeps_a_clean_base_complete():
    c = contract(shapes={"full": FULL, "feed": FEED})
    ledger = full(c, generation="g1")
    ledger = ledger.apply(Manifest.single(c.table, "feed", at(19), since="g1", generation="g2", tombstones=1))
    assert ledger.generation == "g2"
    assert ledger.claims(at(19), limit=timedelta.max).state is State.COMPLETE
    assert ledger.claims(at(19), limit=timedelta.max).deletions_as_of == at(19)


@pytest.mark.protocol("transitions.5")
def test_a_source_restore_breaks_the_generation_chain_until_a_full_delivery_recovers_it():
    c = contract(shapes={"full": FULL, "feed": FEED})
    ledger = full(c, generation="g41")
    with pytest.raises(RejectedDelivery, match="generation chain"):
        ledger.apply(Manifest.single(c.table, "feed", at(19), since="backup-g3", generation="backup-g4"))
    ledger = ledger.apply(Manifest.single(c.table, "full", at(20), generation="restored-g1"))
    ledger = ledger.apply(
        Manifest.single(c.table, "feed", at(21), since="restored-g1", generation="restored-g2")
    )
    assert ledger.claims(at(21), limit=timedelta.max).deletions_as_of == at(21)


@pytest.mark.protocol("transitions.3")
def test_a_feed_arriving_before_any_base_is_refused():
    c = contract()
    with pytest.raises(RejectedDelivery, match="known base"):
        Ledger(c).apply(Manifest.single(c.table, "feed", at(19), since="g1", generation="g2"))


@pytest.mark.protocol("transitions.generation-tombstones-patched")
def test_tombstones_after_an_interval_without_deletes_do_not_repair_earlier_missed_deletions():
    c = contract(shapes={"full": FULL, "since": SINCE, "feed": FEED})
    ledger = full(c, generation="g1")
    ledger = ledger.apply(Manifest.single(c.table, "since", at(19), since=at(18), generation="g2"))
    ledger = ledger.apply(Manifest.single(c.table, "feed", at(20), since="g2", generation="g3"))
    assert ledger.claims(at(20), limit=timedelta.max).state is State.PATCHED
    assert ledger.claims(at(20), limit=timedelta.max).deletions_as_of == at(18)


@pytest.mark.protocol("transitions.4", "manifest.5")
def test_a_timestamp_gap_cannot_raise_completeness_but_overlapping_upserts_can():
    c = contract(shapes={"full": FULL, "since": SINCE})
    ledger = full(c)
    with pytest.raises(RejectedDelivery, match="gap"):
        ledger.apply(Manifest.single(c.table, "since", at(20), since=at(19)))
    ledger = ledger.apply(Manifest.single(c.table, "since", at(20), since=at(17)))
    assert ledger.claims(at(20), limit=timedelta.max).complete_as_of == at(20)


@pytest.mark.protocol("transitions.full", "transitions.5")
def test_a_full_export_without_a_cursor_cannot_continue_an_old_feed_generation():
    c = contract(shapes={"full": FULL, "feed": FEED})
    ledger = full(c, generation="g1").apply(Manifest.single(c.table, "full", at(19)))
    assert ledger.generation is None
    with pytest.raises(RejectedDelivery, match="generation chain"):
        ledger.apply(Manifest.single(c.table, "feed", at(20), since="g1", generation="g2"))


@pytest.mark.protocol("transitions.3", "transitions.first-append", "transitions.later-append")
def test_append_only_logs_without_keys_start_at_declared_history_and_refuse_overlaps():
    c = Contract("events", (), {"log": SINCE}, start_at=at(1))
    ledger = Ledger(c).apply(Manifest.single(c.table, "log", at(18), since=at(1)))
    ledger = ledger.apply(Manifest.single(c.table, "log", at(19), since=at(18)))
    assert ledger.claims(at(19), limit=timedelta.max).deletions_as_of == at(19)
    for since in (at(17), at(19, 1)):
        with pytest.raises(RejectedDelivery, match="contiguous"):
            ledger.apply(Manifest.single(c.table, "log", at(20), since=since))


@pytest.mark.protocol("transitions.3", "transitions.full", "transitions.later-append")
def test_a_keyless_table_delivered_whole_proves_both_dates_and_restarts_its_append_history():
    c = Contract("receipts", (), {"full": FULL, "log": SINCE}, start_at=at(1))
    ledger = Ledger(c).apply(Manifest.single(c.table, "full", at(18)))
    assert ledger.claims(at(18), limit=timedelta.max).deletions_as_of == at(18)
    with pytest.raises(RejectedDelivery, match="contiguous"):
        ledger.apply(Manifest.single(c.table, "log", at(19), since=at(1)))
    ledger = ledger.apply(Manifest.single(c.table, "log", at(19), since=at(18)))
    assert ledger.claims(at(19), limit=timedelta.max).complete_as_of == at(19)


@pytest.mark.protocol("transitions.3")
def test_a_keyless_append_after_a_window_is_refused_until_a_full_delivery_aligns_the_boundaries():
    c = Contract(
        "events",
        (),
        {"full": FULL, "bucket": replace(WINDOW, window_size=None), "log": SINCE},
        window_column="id",
        types={"id": "int"},
        start_at=at(1),
    )
    ledger = Ledger(c).apply(Manifest.single(c.table, "full", at(18, 1)))
    ledger = ledger.apply(Manifest.single(c.table, "bucket", at(18, 2), window=(1, 1)))
    with pytest.raises(RejectedDelivery, match="contiguous"):
        ledger.apply(Manifest.single(c.table, "log", at(18, 3), since=at(18, 1)))
    ledger = ledger.apply(Manifest.single(c.table, "full", at(18, 3)))
    assert (
        ledger.apply(Manifest.single(c.table, "log", at(18, 4), since=at(18, 3)))
        .claims(at(18, 4), limit=timedelta.max)
        .state
        is State.COMPLETE
    )


@pytest.mark.protocol("manifest.4")
def test_an_empty_append_interval_cannot_carry_rows():
    c = Contract("events", (), {"log": SINCE}, start_at=at(1))
    with pytest.raises(InvalidContract, match="empty interval"):
        Manifest.single(c.table, "log", at(1), since=at(1), rows=1).check(c)
    assert Ledger(c).apply(Manifest.single(c.table, "log", at(1), since=at(1), rows=0)).publication_id


@pytest.mark.protocol("claims.reading")
def test_a_reader_always_names_its_age_limit():
    ledger = full(contract())
    for read in (
        lambda: ledger.claims(at(25)),
        lambda: ledger.claims(at(25), timedelta(days=7)),
        lambda: Group((ledger.contract,)).claims({ledger.contract.table: ledger}, at(25)),
    ):
        with pytest.raises(TypeError):
            read()
    for invalid in (None, 7, timedelta(days=-1)):
        with pytest.raises(InvalidContract, match="nonnegative duration"):
            ledger.claims(at(25), limit=invalid)


@pytest.mark.protocol("contract-evolution.compatibility")
def test_a_new_deleted_column_cannot_keep_a_tables_history():
    ledger = full(contract())
    types = {**{k: v for k, v in ledger.contract.types.items() if k != "deleted"}, "erased": "bool"}
    with pytest.raises(InvalidContract, match="bound to the publication history"):
        ledger.with_contract(replace(ledger.contract, deleted_column="erased", types=types))


@pytest.mark.protocol("claims.reading", "claims.expired")
def test_the_freshness_limit_expires_each_region_at_its_own_boundary():
    ledger = full(contract())
    ledger = ledger.apply(Manifest.single(ledger.contract.table, "daily", at(24), window=(10, 20)))
    week = timedelta(days=7)
    assert ledger.claims(at(25), limit=week).state is State.COMPLETE
    assert ledger.claims(at(25, 1), limit=week).state is State.EXPIRED
    assert point(ledger, 15, at(25, 1), week).state is State.COMPLETE
    assert ledger.claims(at(31), limit=timedelta.max).state is State.COMPLETE


@pytest.mark.protocol("transitions.2")
@pytest.mark.parametrize("shape,kwargs", [("full", {}), ("since", {"since": at(17)})])
def test_a_manifest_produced_before_the_region_dates_cannot_change_rows_or_evidence(shape, kwargs):
    ledger = full(contract())
    with pytest.raises(RejectedDelivery, match="later"):
        ledger.apply(Manifest.single(ledger.contract.table, shape, at(17), **kwargs))


@pytest.mark.protocol("claims.1", "claims.2")
def test_a_surface_reading_three_tables_inherits_each_tables_weakest_region():
    stocks = full(contract("stock"), 20)
    invoices = full(contract("invoices"), 18)
    invoices = invoices.apply(Manifest.single("invoices", "since", at(21), since=at(18)))
    plants = full(contract("plants"), 19)
    ledgers = {ledger.contract.table: ledger for ledger in (stocks, invoices, plants)}
    claims = Group(tuple(ledger.contract for ledger in ledgers.values())).claims(
        ledgers, at(21), limit=timedelta.max
    )
    assert (claims.state, claims.complete_as_of, claims.deletions_as_of) == (State.PATCHED, at(19), at(18))
    unknown = Claims(claims.regions + Ledger(contract("missing")).claims(at(21), limit=timedelta.max).regions)
    assert unknown.state is State.UNKNOWN
    assert unknown.complete_as_of is None
    assert len(unknown.in_state(State.UNKNOWN)) == 1


@pytest.mark.protocol("transitions.window")
def test_window_deliveries_preserve_the_table_cursor_for_the_next_global_feed():
    c = contract()
    ledger = full(c, generation="g1")
    ledger = ledger.apply(Manifest.single(c.table, "daily", at(19), window=(1, 10)))
    assert ledger.generation == "g1"
    assert not hasattr(ledger.regions[0], "generation")
    ledger = ledger.apply(Manifest.single(c.table, "feed", at(20), since="g1", generation="g2"))
    assert ledger.generation == "g2"
    assert ledger.claims(at(20), limit=timedelta.max).deletions_as_of == at(20)


@pytest.mark.protocol("contract.domains")
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
    ledger = Ledger(c).apply(Manifest.single(c.table, "daily", at(22), window=windows[0]))
    ledger = ledger.apply(Manifest.single(c.table, "daily", at(23), window=windows[1]))
    claim = ledger.claims(at(23), limit=timedelta.max, window=(windows[0][0], windows[1][1]))
    assert claim.state is State.COMPLETE
    assert claim.complete_as_of == at(22)
    assert len(claim.regions) == 2


@pytest.mark.protocol("contract.domains")
def test_datetime_bounds_are_normalised_to_utc_so_a_dst_fold_is_two_instants():
    from zoneinfo import ZoneInfo

    rome = ZoneInfo("Europe/Rome")
    early = datetime(2026, 10, 25, 2, 0, tzinfo=rome, fold=0)
    late = datetime(2026, 10, 25, 2, 0, tzinfo=rome, fold=1)
    c = Contract(
        "ticks", ("id",), {"slot": WINDOW}, window_column="at", types={"id": "int", "at": "datetime"}
    )
    ledger = Ledger(c).apply(Manifest.single(c.table, "slot", at(26), window=(early, early)))
    assert ledger.claims(at(26), limit=timedelta.max, window=(early, early)).state is State.COMPLETE
    assert ledger.claims(at(26), limit=timedelta.max, window=(late, late)).state is State.UNKNOWN
    assert Manifest.from_json(Manifest.single(c.table, "slot", at(26), window=(late, late)).to_json()).part(
        c.table
    ).window == (
        late.astimezone(UTC),
        late.astimezone(UTC),
    )


@pytest.mark.protocol("contract.domains")
def test_window_bounds_must_have_the_window_columns_declared_type():

    c = Contract(
        "orders",
        ("id",),
        {"daily": WINDOW},
        window_column="day",
        types={"id": "int", "day": "date"},
    )
    with pytest.raises(InvalidContract, match="window bound types"):
        Manifest.single(c.table, "daily", at(19), window=("2026-10-01", "2026-10-19")).check(c)


@pytest.mark.protocol(
    "transitions.timestamp-tombstones-covered", "transitions.timestamp-tombstones-uncovered"
)
@pytest.mark.parametrize("since,expected_valid", [(17, 20), (18, 20), (19, 18)])
def test_timestamp_tombstones_repair_patch_deletion_debt_only_when_the_checkpoint_covers_it(
    since, expected_valid
):
    from dogana import Shape

    tombstones = Shape(
        coverage="all", completeness="changed_only", deletions="tombstone", checkpoint="timestamp"
    )
    c = contract(shapes={"full": FULL, "since": SINCE, "deletes": tombstones})
    ledger = full(c).apply(Manifest.single(c.table, "since", at(19), since=at(18)))
    ledger = ledger.apply(Manifest.single(c.table, "deletes", at(20), since=at(since)))
    assert ledger.claims(at(20), limit=timedelta.max).deletions_as_of == at(expected_valid)


@pytest.mark.protocol("transitions.generation-upserts", "transitions.generation-tombstones-patched")
def test_global_generation_tombstones_cannot_invent_evidence_for_skipped_deletions():
    from dogana import Shape

    feed = Shape(coverage="all", completeness="changed_only", deletions="none", checkpoint="generation")
    c = contract(shapes={"full": FULL, "patch": feed, "feed": FEED})
    ledger = full(c, generation="g1")
    ledger = ledger.apply(Manifest.single(c.table, "patch", at(19), since="g1", generation="g2"))
    ledger = ledger.apply(Manifest.single(c.table, "feed", at(20), since="g2", generation="g3"))
    assert ledger.claims(at(20), limit=timedelta.max).deletions_as_of == at(18)


@pytest.mark.protocol("contract.domains")
def test_typed_window_queries_refuse_a_different_domain_even_before_the_first_delivery():

    c = Contract(
        "stock",
        ("id",),
        {"daily": WINDOW},
        window_column="day",
        types={"id": "int", "day": "date"},
    )
    with pytest.raises(InvalidContract, match="window bound types"):
        Ledger(c).claims(at(18), limit=timedelta.max, window=(1, 10))


def test_dataclass_replacement_preserves_cuts_unless_regions_are_explicitly_replaced():
    from dataclasses import replace

    from dogana import Region

    c = contract()
    ledger = Ledger(c).apply(Manifest.single(c.table, "daily", at(19), window=(1, 9)))
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


@pytest.mark.parametrize(
    "dates,message",
    [
        (
            dict(complete_as_of=at(18), deletions_as_of=at(19)),
            "deletion evidence cannot be newer than completeness",
        ),
        (dict(complete_as_of=at(18)), "region dates must be present or absent together"),
        (dict(deletions_as_of=at(18)), "region dates must be present or absent together"),
    ],
)
def test_region_evidence_requires_both_dates_in_completeness_order(dates, message):
    from dogana import Region

    with pytest.raises(InvalidContract) as error:
        Region(**dates)
    assert str(error.value) == message


def test_known_evidence_normalizes_both_dates_to_utc():
    from datetime import timezone

    from dogana.ledger import Known

    local = at(18).astimezone(timezone(timedelta(hours=2)))
    known = Known(local, local - timedelta(hours=1))
    assert known.complete_as_of == at(18) and known.complete_as_of.tzinfo is UTC
    assert known.deletions_as_of == at(17, 23) and known.deletions_as_of.tzinfo is UTC
    assert not hasattr(known, "COMPLETE_AS_OF")


@pytest.mark.parametrize("limit", [-timedelta(microseconds=1), None, 1])
def test_region_states_refuse_invalid_age_limits(limit):
    from dogana import Region

    with pytest.raises(InvalidContract, match="the age limit must be a nonnegative duration"):
        Region().state(at(18), limit=limit)


@pytest.mark.parametrize("ends", [(1, 3), (3, 1), (1, None)])
def test_ledger_regions_must_partition_the_entire_domain_without_gaps_or_overlaps(ends):
    from dogana import Region

    left, right = ends
    regions = (Region(high=left), Region(low=right)) if right is not None else (Region(high=left),)
    with pytest.raises(InvalidContract) as error:
        Ledger(contract(shapes={"full": FULL}), regions)
    assert str(error.value) == "ledger regions must partition the entire ordered domain"


def test_adjacent_regions_with_equal_evidence_coalesce():
    from dogana import Region

    c = contract(shapes={"full": FULL})
    regions = (
        Region(high=2, high_open=True, complete_as_of=at(18), deletions_as_of=at(18)),
        Region(low=2, complete_as_of=at(18), deletions_as_of=at(18)),
    )
    assert Ledger(c, regions).regions == (Region(complete_as_of=at(18), deletions_as_of=at(18)),)


def test_claims_require_at_least_one_region():
    with pytest.raises(InvalidContract) as error:
        Claims(())
    assert str(error.value) == "claims need at least one region"


def test_ledger_regions_must_share_one_scalar_domain():
    from dogana import Region

    with pytest.raises(InvalidContract) as error:
        Ledger(contract(shapes={"full": FULL}), (Region(high=1), Region(low="a")))
    assert str(error.value) == "ledger regions must share one scalar domain"


@pytest.mark.parametrize("field", ["generation", "publication_id"])
def test_ledger_positions_and_publication_identities_cannot_be_empty(field):
    with pytest.raises(InvalidContract, match="generation and publication identities must be nonempty"):
        Ledger(contract(), **{field: ""})


def test_window_queries_follow_the_window_domain_and_do_not_borrow_the_key_type():
    from dogana import Region

    c = Contract("moves", ("XXXX",), {"full": FULL}, types={"XXXX": "int"})
    assert Ledger(c).covered(("a", "z")) == (Region("a", "z\x00", high_open=True),)
    ledger = Ledger(
        c, (Region(high=2, high_open=True), Region(low=2, complete_as_of=at(18), deletions_as_of=at(18)))
    )
    with pytest.raises(InvalidContract, match="window bound types differ from the ledger domain"):
        ledger.covered(("a", "z"))
    with pytest.raises(InvalidContract, match="window bound types differ from the contract types"):
        Ledger(contract()).covered(("a", "z"))


@pytest.mark.protocol("contract.domains")
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
    manifest = Manifest.single(table, shape, at(19), **options)
    with pytest.raises(RejectedDelivery) as rejected:
        ledger.apply(manifest)
    detail = str(rejected.value)
    assert f"table={ledger.contract.table!r}" in detail
    assert "produced_at=2026-10-19T00:00:00+00:00" in detail
    if table == ledger.contract.table:
        assert f"shape={shape!r}" in detail and "since=" in detail and "window=" in detail
    else:
        assert f"declares {table}: the read has no part for table {ledger.contract.table!r}" in detail


def test_an_accounting_window_with_the_wrong_scalar_domain_is_a_delivery_refusal():
    ledger = Ledger(contract()).apply(Manifest.single("moves", "daily", at(18), window=(1, 9)))
    with pytest.raises(RejectedDelivery, match="window=.*contract types"):
        ledger.apply(Manifest.single("moves", "daily", at(19), window=("a", "b")))


@pytest.mark.protocol("transitions.first-append", "transitions.later-append")
def test_keyless_append_preserves_or_replaces_an_issued_table_generation():
    c = Contract("events", (), {"log": SINCE}, start_at=at(1))
    ledger = Ledger(c).apply(Manifest.single(c.table, "log", at(18), since=at(1), generation="g1"))
    ledger = ledger.apply(Manifest.single(c.table, "log", at(19), since=at(18)))
    assert ledger.generation == "g1"
    ledger = ledger.apply(Manifest.single(c.table, "log", at(20), since=at(19), generation="g2"))
    assert ledger.generation == "g2"


def accepted_receipt(ledger, manifest):
    from dogana import Gate

    checks = ()
    receipt = Gate.evaluate(ledger, manifest, checks, now=at(25))
    assert receipt.accepted
    return receipt


def receipt_chain():
    c = contract()
    first = accepted_receipt(Ledger(c), Manifest.single(c.table, "full", at(18)))
    second = accepted_receipt(fold(first), Manifest.single(c.table, "daily", at(19), window=(1, 9)))
    return c, first, second


@pytest.mark.protocol("ledger.accounting")
def test_apply_takes_a_manifest_and_says_where_rows_go():
    c = Contract("t", key=("id",), types={"id": "int"}, shapes={"full": Shape.full()})
    with pytest.raises(TypeError, match="takes a Manifest, not list: the ledger reasons about declarations"):
        Ledger(c).apply([{"id": 1}])
