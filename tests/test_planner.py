from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dogana import (
    Contract,
    Deletions,
    ExtractionRequest,
    InvalidContract,
    Ledger,
    Manifest,
    PlanningError,
    Region,
    Requirement,
    next_extraction,
    plan_delivery,
)
from tests.support import FEED, FULL, RECENT, SINCE, WINDOW, at
from tests.support import accounting_contract as contract


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
    c = contract(window_type="date")
    request = next_extraction(Ledger(c), "daily", at(20))
    assert request.interval == ((at(20) - timedelta(days=29)).date(), at(20).date())


def test_the_next_read_cannot_go_backwards_in_source_time():
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(20)))
    with pytest.raises(PlanningError, match="advance"):
        next_extraction(ledger, "since", at(19))


def test_a_tombstone_feed_can_advance_completeness_when_its_old_deletion_date_already_satisfies_the_reader():
    c = contract(shapes={"full": FULL, "since": SINCE, "feed": FEED})
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18), generation="g1"))
    ledger = ledger.apply(Manifest(c.table, "since", at(19), since=at(18), generation="g2"))
    request = plan_delivery(ledger, Requirement(c.table, at(20), at(18)))
    assert request.shape == "feed"


@pytest.mark.parametrize("window", [(0, 0), (at(18).date(), at(18).date()), (at(18), at(18))])
def test_a_minimum_requirement_can_use_a_snapshot_later_than_newer_local_evidence(window):
    c = contract(window_type=type(window[0]).__name__, shapes={"full": FULL, "daily": WINDOW})
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    ledger = ledger.apply(Manifest(c.table, "daily", at(20), window=window))
    request = plan_delivery(ledger, Requirement(c.table, at(19), at(19)))
    assert request.shape == "full"
    assert request.until == at(20) + timedelta(microseconds=1)
    after = ledger.apply(Manifest(request.table, request.shape, request.until))
    assert after.claims(request.until, limit=timedelta.max).deletions_as_of >= at(19)


def test_an_admissible_boundary_considers_only_the_chosen_shapes_coverage():
    c = contract(shapes={"full": FULL, "daily": WINDOW})
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    ledger = ledger.apply(Manifest(c.table, "daily", at(22), window=(1, 10)))
    request = plan_delivery(ledger, Requirement(c.table, at(19), at(19), (20, 30)))
    assert request.shape == "daily"
    assert request.until == at(19)


def test_deletion_date_can_require_a_later_read_even_when_completeness_is_already_sufficient():
    c = contract(shapes={"full": FULL, "since": SINCE})
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    ledger = ledger.apply(Manifest(c.table, "since", at(20), since=at(18)))
    request = plan_delivery(ledger, Requirement(c.table, at(19), at(19)))
    assert request.shape == "full"
    assert request.until == at(20) + timedelta(microseconds=1)


def test_a_delayed_date_window_covers_every_date_since_the_previous_window():
    c = contract(window_type="date", shapes={"daily": WINDOW})
    first = next_extraction(Ledger(c), "daily", at(19, 12))
    ledger = Ledger(c).apply(Manifest(c.table, first.shape, first.until, window=first.window))
    later = at(19, 12) + timedelta(days=30, hours=13)
    request = next_extraction(ledger, "daily", later)
    assert request.window == (at(20).date(), later.date())
    after = ledger.apply(Manifest(c.table, request.shape, request.until, window=request.window))
    assert (
        after.claims(later, limit=timedelta.max, window=(first.window[0], request.window[1])).complete_as_of
        is not None
    )


def test_datetime_windows_bridge_a_missed_run_from_the_first_uncovered_microsecond():
    daily = replace(WINDOW, window_size=timedelta(days=1))
    c = contract(window_type="datetime", shapes={"daily": daily})
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(19), window=(at(18), at(19))))
    request = next_extraction(ledger, "daily", at(21, 1))
    assert request.window == (at(19) + timedelta(microseconds=1), at(21, 1))
    after = ledger.apply(Manifest(c.table, "daily", request.until, window=request.window))
    assert (
        after.claims(request.until, limit=timedelta.max, window=(at(18), at(21, 1))).complete_as_of
        is not None
    )


def test_rolling_windows_use_coverage_bounds_rather_than_the_previous_read_time():
    daily = replace(WINDOW, window_size=timedelta(days=1))
    c = contract(window_type="datetime", shapes={"daily": daily})
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(20), window=(at(18), at(19))))
    assert next_extraction(ledger, "daily", at(22)).window[0] == at(19) + timedelta(microseconds=1)


def test_ordinary_rolling_overlap_is_preserved_when_runs_are_close_together():
    c = contract(window_type="date", shapes={"daily": WINDOW})
    ledger = Ledger(c).apply(
        Manifest(c.table, "daily", at(19), window=((at(19) - timedelta(days=29)).date(), at(19).date()))
    )
    request = next_extraction(ledger, "daily", at(20))
    assert request.window == ((at(20) - timedelta(days=29)).date(), at(20).date())


def test_a_date_domain_refuses_a_fractional_day_rolling_width_at_declaration():
    with pytest.raises(InvalidContract, match="whole number of days"):
        contract(window_type="date", shapes={"daily": replace(WINDOW, window_size=timedelta(hours=12))})


def test_an_integer_domain_refuses_a_duration_window_at_declaration():
    with pytest.raises(InvalidContract, match="date or datetime"):
        Contract("orders", ("id",), {"daily": WINDOW}, window_column="day", types={"id": "int", "day": "int"})


def test_an_explicit_window_keeps_its_bounds_and_must_match_the_declared_domain():
    c = contract(window_type="date")
    window = (at(18).date(), at(19).date())
    assert next_extraction(Ledger(c), "daily", at(20), window=window).window == window
    with pytest.raises(InvalidContract, match="types differ"):
        next_extraction(Ledger(c), "daily", at(20), window=(1, 10))


def test_a_partial_complete_keyless_window_prevents_an_overlapping_append_request():
    c = Contract(
        "events",
        (),
        {"full": FULL, "daily": replace(WINDOW, window_size=None), "append": SINCE},
        window_column="day",
        types={"day": "int"},
        start_at=at(17),
    )
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    ledger = ledger.apply(Manifest(c.table, "daily", at(19), window=(1, 10)))
    with pytest.raises(PlanningError, match="contiguous"):
        next_extraction(ledger, "append", at(20))
    assert plan_delivery(ledger, Requirement(c.table, at(20))).shape == "full"


def test_keyless_append_requests_start_at_retained_history_then_the_published_boundary():
    c = Contract("events", (), {"append": SINCE}, start_at=at(17))
    request = next_extraction(Ledger(c), "append", at(18))
    assert request.since == at(17)
    ledger = Ledger(c).apply(Manifest(c.table, "append", at(18), since=at(17)))
    assert next_extraction(ledger, "append", at(19)).since == at(18)


def test_a_complete_window_does_not_break_the_table_generation_checkpoint():
    c = contract(shapes={"full": FULL, "daily": WINDOW, "feed": FEED})
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18), generation="g1"))
    ledger = ledger.apply(Manifest(c.table, "daily", at(19), window=(1, 10)))
    assert next_extraction(ledger, "feed", at(20)).since == "g1"


def test_keyed_timestamp_lookback_is_subtracted_from_the_weakest_covered_completeness():
    c = contract(shapes={"full": FULL, "daily": WINDOW, "since": replace(SINCE, lookback=timedelta(hours=2))})
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    ledger = ledger.apply(Manifest(c.table, "daily", at(19), window=(1, 10)))
    request = next_extraction(ledger, "since", at(20))
    assert request.since == at(18) - timedelta(hours=2)
    assert plan_delivery(ledger, Requirement(c.table, at(20))).since == request.since


def test_a_windowed_timestamp_lookback_uses_only_covered_evidence():
    c = contract(
        shapes={"full": FULL, "daily": WINDOW, "recent": replace(RECENT, lookback=timedelta(hours=1))}
    )
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    ledger = ledger.apply(Manifest(c.table, "daily", at(19), window=(1, 10)))
    assert next_extraction(ledger, "recent", at(20), window=(1, 10)).since == at(19) - timedelta(hours=1)


@pytest.mark.parametrize("lookback", [None, timedelta(0), timedelta(days=2)])
def test_timestamp_tombstones_can_repair_deletion_date_by_rereading_the_missing_deletions(lookback):
    tombstones = replace(SINCE, deletions=Deletions.TOMBSTONE, cost=1, lookback=lookback)
    c = contract(shapes={"full": FULL, "since": SINCE, "deletes": tombstones})
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    ledger = ledger.apply(Manifest(c.table, "since", at(19), since=at(18)))
    request = plan_delivery(ledger, Requirement(c.table, at(20), at(20)))
    assert request.shape == "deletes"
    assert request.since == min(at(18), at(19) - (lookback or timedelta(0)))
    after = ledger.apply(Manifest(c.table, request.shape, request.until, since=request.since))
    assert after.claims(request.until, limit=timedelta.max).deletions_as_of == at(20)


def test_sufficient_deletion_date_does_not_force_timestamp_tombstones_to_reread_older_history():
    c = contract(
        shapes={
            "full": FULL,
            "since": SINCE,
            "deletes": replace(SINCE, deletions=Deletions.TOMBSTONE, cost=1),
        }
    )
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    ledger = ledger.apply(Manifest(c.table, "since", at(19), since=at(18)))
    request = plan_delivery(ledger, Requirement(c.table, at(20), at(18)))
    assert (request.shape, request.since) == ("deletes", at(19))


def test_equal_cost_shapes_are_chosen_by_name():
    c = contract(shapes={"z": FULL, "a": FULL})
    assert plan_delivery(Ledger(c), Requirement(c.table, at(18))).shape == "a"


def test_an_unrepresentable_later_read_is_refused_without_overflow():
    c = contract(shapes={"full": FULL, "since": SINCE})
    latest = datetime.max.replace(tzinfo=UTC)
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    ledger = ledger.apply(Manifest(c.table, "since", latest, since=at(18)))
    with pytest.raises(PlanningError, match="no admitted"):
        plan_delivery(ledger, Requirement(c.table, at(19), at(19)))


def test_lookback_underflow_is_reported_as_an_unavailable_request():
    c = contract(shapes={"full": FULL, "since": replace(SINCE, lookback=timedelta(days=1))})
    ledger = Ledger(c).apply(Manifest(c.table, "full", datetime.min.replace(tzinfo=UTC)))
    with pytest.raises(PlanningError, match="lookback"):
        next_extraction(ledger, "since", at(20))
    assert plan_delivery(ledger, Requirement(c.table, at(20))).shape == "full"


@pytest.mark.parametrize(
    "field,value",
    [
        ("table", 1),
        ("table", "bad table"),
        ("complete_as_of", "today"),
        ("deletions_as_of", False),
        ("window", "ab"),
    ],
)
def test_requirement_constructors_reject_invalid_wire_values(field, value):
    values = {"table": "moves", "complete_as_of": at(18), field: value}
    with pytest.raises(InvalidContract):
        Requirement(**values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("table", 1),
        ("shape", 1),
        ("shape", ""),
        ("until", "today"),
        ("since", True),
        ("since", ""),
        ("since", at(19)),
        ("cost", True),
        ("cost", "1"),
        ("cost", -1),
        ("cost", float("nan")),
        ("cost", float("inf")),
        ("window", "ab"),
    ],
)
def test_request_constructors_reject_invalid_wire_values(field, value):
    values = {"table": "moves", "shape": "since", "until": at(18), field: value}
    with pytest.raises(InvalidContract):
        ExtractionRequest(**values)


def test_planner_values_normalize_dst_folds_containers_and_numeric_costs():
    zone = ZoneInfo("Europe/Rome")
    early = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    late = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    requirement = Requirement("moves", late, window=[early, late])
    request = ExtractionRequest("moves", "recent", late, [early, late], early, 1)
    assert requirement.window == (early.astimezone(UTC), late.astimezone(UTC))
    assert request.window == requirement.window
    assert request.until - request.since == timedelta(hours=1)
    assert request.cost == 1.0
    assert type(request.cost) is float
    for value in (requirement, request):
        assert type(value).from_json(value.to_json()) == value
        assert hash(type(value).from_json(value.to_json())) == hash(value)
    assert request.identity == ExtractionRequest("moves", "recent", late, (early, late), early, 1.0).identity


@pytest.mark.parametrize("date_window", [False, True])
def test_a_rolling_anchor_includes_an_open_previous_endpoint(date_window):
    c = contract(
        window_type="date" if date_window else "datetime",
        shapes={"daily": replace(WINDOW, window_size=timedelta(days=1))},
    )
    endpoint = at(19).date() if date_window else at(19)
    ledger = Ledger(
        c,
        (
            Region(high=endpoint, high_open=True, complete_as_of=at(19), deletions_as_of=at(19)),
            Region(low=endpoint),
        ),
    )
    assert next_extraction(ledger, "daily", at(22)).window[0] == endpoint


def test_a_rolling_anchor_ignores_evidence_beyond_its_window_end():
    c = contract(window_type="datetime", shapes={"daily": replace(WINDOW, window_size=timedelta(days=1))})
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(19), window=(at(18), at(19))))
    ledger = ledger.apply(Manifest(c.table, "daily", at(22), window=(at(25), at(26))))
    assert next_extraction(ledger, "daily", at(23)).window[0] == at(19) + timedelta(microseconds=1)


def test_a_rolling_anchor_uses_the_newest_covered_endpoint():
    c = contract(window_type="datetime", shapes={"daily": replace(WINDOW, window_size=timedelta(days=1))})
    ledger = Ledger(c).apply(Manifest(c.table, "daily", at(19), window=(at(18), at(19))))
    ledger = ledger.apply(Manifest(c.table, "daily", at(22), window=(at(21), at(22))))
    assert next_extraction(ledger, "daily", at(25)).window[0] == at(22) + timedelta(microseconds=1)


def test_a_whole_domain_base_does_not_extend_a_rolling_window_to_its_read_time():
    c = contract(
        window_type="datetime", shapes={"full": FULL, "daily": replace(WINDOW, window_size=timedelta(days=1))}
    )
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18)))
    assert next_extraction(ledger, "daily", at(25)).window == (at(24), at(25))


def test_a_one_date_window_includes_the_first_representable_date():
    c = contract(window_type="date", shapes={"daily": replace(WINDOW, window_size=timedelta(days=1))})
    request = next_extraction(Ledger(c), "daily", datetime.min.replace(tzinfo=UTC))
    assert request.window == (date.min, date.min)


def test_a_rolling_window_outside_the_datetime_range_is_refused():
    c = contract(window_type="datetime", shapes={"daily": WINDOW})
    with pytest.raises(PlanningError, match="range"):
        next_extraction(Ledger(c), "daily", datetime.min.replace(tzinfo=UTC))


@pytest.mark.parametrize("shape", ["full", "since", "feed"])
def test_every_shape_refuses_a_read_at_the_covered_evidence_boundary(shape):
    c = contract()
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(20), generation="g1"))
    with pytest.raises(PlanningError):
        next_extraction(ledger, shape, at(20))


def test_planning_propagates_programming_errors_instead_of_treating_them_as_impossible_reads(monkeypatch):
    c = contract(shapes={"full": FULL})

    def broken_apply(self, manifest):
        raise TypeError("broken transition")

    monkeypatch.setattr(Ledger, "apply", broken_apply)
    with pytest.raises(TypeError, match="broken transition"):
        plan_delivery(Ledger(c), Requirement(c.table, at(20)))


@pytest.mark.parametrize("required", [at(18), at(19)])
def test_an_initial_append_plan_can_advance_past_the_retained_history_start(required):
    c = Contract("events", (), {"append": SINCE}, start_at=at(18))
    request = plan_delivery(Ledger(c), Requirement(c.table, required, required))
    assert request.until == max(required, at(18) + timedelta(microseconds=1))
    assert request.since == at(18)
    after = Ledger(c).apply(Manifest(c.table, request.shape, request.until, since=request.since))
    assert after.claims(request.until, limit=timedelta.max).deletions_as_of >= required
