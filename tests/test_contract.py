from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from itertools import product

import pytest

from dogana import (
    Checkpoint,
    Column,
    Completeness,
    Contract,
    Coverage,
    Deletions,
    InvalidContract,
    Manifest,
    Shape,
)
from tests.support import FEED, FULL, SINCE, WINDOW, at, contract


def test_the_twelve_semantic_shapes_and_no_other_are_admitted():
    valid = []
    for properties in product(Coverage, Completeness, Deletions, Checkpoint):
        try:
            valid.append(Shape(*properties))
        except InvalidContract:
            pass
    assert len(valid) == 12
    assert not any(s.coverage is Coverage.WINDOW and s.checkpoint is Checkpoint.GENERATION for s in valid)


@pytest.mark.parametrize(
    "properties",
    [
        ("all", "complete", "absence", "timestamp"),
        ("all", "complete", "tombstone", "none"),
        ("all", "changed_only", "absence", "timestamp"),
        ("all", "changed_only", "none", "none"),
        ("keys", "complete", "absence", "none"),
        ("window", "changed_only", "none", "generation"),
        ("window", "changed_only", "tombstone", "generation"),
    ],
)
def test_a_source_cannot_declare_a_contradictory_shape(properties):
    with pytest.raises(InvalidContract):
        Shape(*properties)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"shapes": {}},
        {"shapes": {"daily": WINDOW}},
        {"window_column": "day"},
        {"key": (), "shapes": {"feed": FEED}},
        {"key": ("id", "id")},
        {"max_shrink_fraction": -0.1},
        {"max_shrink_fraction": 1.1},
        {"schema": (Column("other", "str"),)},
        {"table": "__dogana_ledgers"},
        {"table": "__DOGANA_ledgers"},
        {"freshness": timedelta(days=-1)},
    ],
)
def test_contract_parameters_must_describe_a_coherent_table(kwargs):
    fields = {"table": "stock", "key": ("id",), "shapes": {"full": FULL}}
    fields.update(kwargs)
    with pytest.raises(InvalidContract):
        Contract(**fields)


def test_a_contract_copies_mutable_input_and_freezes_its_public_declarations():
    shapes = {"full": FULL}
    c = Contract("stock", ("id",), shapes)
    shapes.clear()
    assert c.shape("full") == FULL
    with pytest.raises(TypeError):
        c.shapes["feed"] = FEED
    with pytest.raises(FrozenInstanceError):
        c.table = "other"


def test_a_manifest_for_an_unadmitted_source_shape_is_refused_before_rows_are_read():
    with pytest.raises(InvalidContract, match="admits no shape"):
        Manifest("moves", "surprise", at(18)).check(contract())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"table": "other"},
        {"window": (1, 3)},
        {"since": at(17)},
        {"rows": -1},
        {"rows": True},
        {"tombstones": ((1,),)},
    ],
)
def test_a_full_export_manifest_cannot_contradict_its_contract(kwargs):
    fields = {"table": "moves", "shape": "full", "produced_at": at(18)}
    fields.update(kwargs)
    with pytest.raises(InvalidContract):
        Manifest(**fields).check(contract())


@pytest.mark.parametrize("window", [None, (30, 1), (1, "2"), (True, False)])
def test_window_manifests_need_ordered_bounds_of_one_type(window):
    with pytest.raises(InvalidContract):
        Manifest("moves", "daily", at(18), window=window).check(contract())


def test_source_read_boundaries_must_include_a_timezone():
    with pytest.raises(InvalidContract, match="timezone-aware"):
        Manifest("stock", "full", datetime(2026, 10, 18))


def test_generation_feeds_must_issue_the_cursor_the_next_read_will_use():
    with pytest.raises(InvalidContract, match="generation tokens"):
        Manifest("moves", "feed", at(19), since="g1").check(contract())


def test_append_only_history_has_an_explicit_start_and_a_keyless_full_export_needs_none():
    log = Contract("events", (), {"log": SINCE}, start_at=at(1))
    assert log.start_at == at(1)
    with pytest.raises(InvalidContract, match="start_at"):
        Contract("events", (), {"log": SINCE})
    receipts = Contract("receipts", (), {"full": FULL, "plant": WINDOW}, window_column="plant")
    assert receipts.start_at is None
    with pytest.raises(InvalidContract, match="start_at"):
        Contract("receipts", (), {"full": FULL}, start_at=at(1))
    with pytest.raises(InvalidContract, match="append-only"):
        Contract("receipts", (), {"feed": FEED})


def test_unrecognized_shape_properties_use_the_public_contract_error():
    with pytest.raises(InvalidContract, match="invalid type for Shape.coverage"):
        Shape("sometimes", "complete", "absence", "none")


def test_date_windows_cannot_request_fractional_calendar_days():
    from dataclasses import replace

    with pytest.raises(InvalidContract, match="whole number of days"):
        contract(shapes={"daily": replace(WINDOW, window_size=timedelta(hours=12))})


@pytest.mark.parametrize("column_type", ["int", "str"])
def test_a_duration_cannot_define_a_rolling_window_over_integer_or_string_partitions(column_type):
    with pytest.raises(InvalidContract, match="date or datetime"):
        contract(schema=(Column("id", "int"), Column("day", column_type), Column("value", "str")))


def test_shrink_protection_is_enabled_by_default_and_only_none_disables_it():
    assert contract().max_shrink_fraction == 0.5
    assert contract(max_shrink_fraction=None).max_shrink_fraction is None
    assert contract(max_shrink_fraction=0).max_shrink_fraction == 0.0
    assert type(contract(max_shrink_fraction=0).max_shrink_fraction) is float


@pytest.mark.parametrize("lookback", [timedelta(0), timedelta(minutes=5)])
def test_lookback_belongs_to_keyed_timestamp_reads(lookback):
    shape = replace(SINCE, lookback=lookback)
    assert contract(shapes={"since": shape}).shape("since").lookback == lookback
    with pytest.raises(InvalidContract, match="keyed"):
        Contract("events", (), {"append": shape}, start_at=at(1))


@pytest.mark.parametrize(
    "shape,lookback", [(FULL, timedelta(0)), (FEED, timedelta(0)), (SINCE, timedelta(-1))]
)
def test_lookback_cannot_change_generation_or_complete_read_semantics(shape, lookback):
    with pytest.raises(InvalidContract, match="lookback"):
        replace(shape, lookback=lookback)


def test_a_cursor_column_must_explicitly_name_a_datetime_schema_column():
    schema = (Column("id", "int"), Column("changed_at", "datetime"))
    c = Contract("stock", ("id",), {"since": SINCE}, schema=schema, cursor_column="changed_at")
    assert c.cursor_column == "changed_at"
    for invalid in ((), (Column("id", "int"),), (Column("id", "int"), Column("changed_at", "date"))):
        with pytest.raises(InvalidContract, match="cursor_column"):
            replace(c, schema=invalid)


def test_history_compatibility_binds_only_table_key_window_and_their_types():
    original = contract()
    evolved = replace(
        original,
        schema=tuple(reversed(original.schema)) + (Column("changed_at", "datetime"),),
        freshness=timedelta(days=1),
        max_shrink_fraction=None,
        cursor_column="changed_at",
        shapes={**original.shapes, "another": FULL},
    )
    assert original.compatible_with(evolved)
    assert evolved.compatible_with(original)
    assert original.identity != evolved.identity
    changed_value_type = replace(
        original, schema=tuple(Column(c.name, "float") if c.name == "value" else c for c in original.schema)
    )
    assert original.compatible_with(changed_value_type)
    for incompatible in (
        replace(original, table="other"),
        replace(original, key=("day", "id")),
        replace(
            original, schema=tuple(Column(c.name, "str") if c.name == "id" else c for c in original.schema)
        ),
        replace(original, schema=()),
        replace(original, window_column=None, shapes={"full": FULL}),
    ):
        assert not original.compatible_with(incompatible)


@pytest.mark.parametrize(
    "field,value",
    [
        ("key", "id"),
        ("key", {"id"}),
        ("key", {"id": 1}),
        ("schema", {"id": "int"}),
        ("shapes", [("full", FULL)]),
        ("shapes", {1: FULL}),
        ("freshness", 5),
        ("max_shrink_fraction", True),
        ("max_shrink_fraction", "0.5"),
        ("max_shrink_fraction", float("nan")),
        ("cursor_column", 1),
    ],
)
def test_contract_constructor_rejects_invalid_types_before_normalization(field, value):
    with pytest.raises(InvalidContract):
        replace(contract(), **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("table", 1),
        ("shape", []),
        ("shape", ""),
        ("rows", "2"),
        ("rows", True),
        ("keys", "ab"),
        ("keys", ("ab",)),
        ("keys", ((True,),)),
        ("keys", ((1.0,),)),
        ("keys", ((None,),)),
        ("keys", ((),)),
        ("keys", ((1,), (1,))),
        ("tombstones", {1}),
        ("window", "ab"),
        ("window", {1: 2, 3: 4}),
        ("since", 1),
        ("since", ""),
        ("generation", 1),
        ("generation", ""),
        ("schema_hash", 1),
        ("content_hash", 1),
        ("content_hash", ""),
    ],
)
def test_manifest_constructor_rejects_invalid_types_without_needing_a_contract(field, value):
    with pytest.raises(InvalidContract):
        replace(Manifest("stock", "full", at(18)), **{field: value})


def test_list_inputs_are_copied_into_immutable_typed_values():
    key = ["id"]
    schema = [Column("id", "int")]
    c = Contract("stock", key, {"full": FULL}, schema=schema)
    live_keys = [[1], [2]]
    m = Manifest("stock", "full", at(18), keys=live_keys)
    key.clear()
    schema.clear()
    live_keys[0].append(3)
    assert c.key == ("id",)
    assert c.schema == (Column("id", "int"),)
    assert m.keys == ((1,), (2,))
    assert hash(c) == hash(Contract.from_json(c.to_json()))
    assert hash(m) == hash(Manifest.from_json(m.to_json()))


def test_window_deliveries_cannot_issue_a_table_wide_generation():
    with pytest.raises(InvalidContract, match="table generation"):
        Manifest("moves", "daily", at(18), window=(at(17).date(), at(18).date()), generation="g1").check(
            contract()
        )


def test_corrected_content_changes_manifest_identity():
    original = Manifest("stock", "full", at(18), rows=2, content_hash="sha256:first")
    corrected = replace(original, content_hash="sha256:corrected")
    assert original.identity != corrected.identity
    assert Manifest.from_json(corrected.to_json()) == corrected


@pytest.mark.parametrize("duration", [timedelta(days=100000, microseconds=1), timedelta.max])
def test_durations_that_cannot_round_trip_in_seconds_are_refused(duration):
    for build in (
        lambda: replace(contract(), freshness=duration),
        lambda: replace(SINCE, lookback=duration),
        lambda: replace(WINDOW, window_size=duration),
    ):
        with pytest.raises(InvalidContract, match="represented exactly in wire seconds"):
            build()


def test_large_integral_durations_remain_exact_and_round_trip():
    duration = timedelta(days=999999999, seconds=86399)
    c = replace(contract(), freshness=duration)
    assert c.to_dict()["freshness"] == {"$seconds": 86399999999999}
    assert Contract.from_json(c.to_json()) == c
    adjacent = replace(c, freshness=duration - timedelta(seconds=1))
    assert adjacent.identity != c.identity


def test_integer_costs_cannot_silently_lose_precision():
    exact = replace(FULL, cost=2**53)
    assert exact.cost == 2**53
    assert Shape.from_json(exact.to_json()) == exact
    with pytest.raises(InvalidContract, match="represented exactly as a float"):
        replace(FULL, cost=2**53 + 1)


@pytest.mark.parametrize("value", ["\ud800", "\udfff", "plant\udc80"])
def test_lone_surrogates_cannot_enter_window_or_key_domains(value):
    from dogana import Region
    from dogana.contract import bounds

    for build in (
        lambda: Manifest("stock", "full", at(18), keys=((value,),)),
        lambda: Manifest("stock", "daily", at(18), window=(value, value)),
        lambda: Region(value, None),
        lambda: bounds((value, value)),
    ):
        with pytest.raises(InvalidContract, match="Unicode scalar"):
            build()
