from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from itertools import product

import pytest

from dogana import (
    Checkpoint,
    Completeness,
    Contract,
    Coverage,
    Deletions,
    InvalidContract,
    Manifest,
    Part,
    Shape,
)
from dogana.contract import bounds
from tests.support import FEED, FULL, SINCE, WINDOW, at, contract, part_of, revised


@pytest.mark.protocol("contract.shapes", "contract.shapes.1", "contract.shapes.2")
def test_the_ten_semantic_shapes_and_no_other_are_admitted():
    valid = []
    for properties in product(Coverage, Completeness, Deletions, Checkpoint):
        try:
            valid.append(
                Shape(
                    **dict(
                        zip(("coverage", "completeness", "deletions", "checkpoint"), properties, strict=True)
                    )
                )
            )
        except InvalidContract:
            pass
    assert len(valid) == 10
    assert not any(
        s.coverage is Coverage.WINDOW and s.checkpoint in (Checkpoint.GENERATION, Checkpoint.SEQUENCE)
        for s in valid
    )


@pytest.mark.protocol("contract.shapes", "contract.shapes.1", "contract.shapes.2")
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
        Shape(**dict(zip(("coverage", "completeness", "deletions", "checkpoint"), properties, strict=True)))


@pytest.mark.protocol("contract.declaration", "contract.types", "contract.window-column")
@pytest.mark.parametrize(
    "kwargs",
    [
        {"shapes": {}},
        {"shapes": {"daily": WINDOW}},
        {"window_column": "day"},
        {"key": (), "shapes": {"feed": FEED}},
        {"key": ("id", "id")},
        {"types": {"other": "str"}},
        {"table": "__dogana_ledgers"},
        {"table": "__DOGANA_ledgers"},
    ],
)
def test_contract_parameters_must_describe_a_coherent_table(kwargs):
    fields = {"table": "stock", "key": ("id",), "shapes": {"full": FULL}, "types": {"id": "int"}}
    fields.update(kwargs)
    with pytest.raises(InvalidContract):
        Contract(**fields)


def test_a_contract_copies_mutable_input_and_freezes_its_public_declarations():
    shapes = {"full": FULL}
    c = Contract("stock", ("id",), shapes, types={"id": "int"})
    shapes.clear()
    assert c.shape("full") == FULL
    with pytest.raises(TypeError):
        c.shapes["feed"] = FEED
    with pytest.raises(FrozenInstanceError):
        c.table = "other"


@pytest.mark.protocol("transitions.1")
def test_a_manifest_for_an_unadmitted_source_shape_is_refused_before_rows_are_read():
    with pytest.raises(InvalidContract, match="admits no shape"):
        Manifest.single("moves", "surprise", at(18)).check(contract())


@pytest.mark.protocol("transitions.1")
@pytest.mark.parametrize(
    "kwargs",
    [
        {"table": "other"},
        {"window": (1, 3)},
        {"since": at(17)},
        {"rows": -1},
    ],
)
def test_a_full_export_manifest_cannot_contradict_its_contract(kwargs):
    fields = {"table": "moves", "shape": "full", "produced_at": at(18)}
    fields.update(kwargs)
    with pytest.raises(InvalidContract):
        Manifest.single(**fields).check(contract())


@pytest.mark.protocol("manifest.1", "contract.domains")
@pytest.mark.parametrize("window", [None, (30, 1), (1, "2"), (True, False)])
def test_window_manifests_need_ordered_bounds_of_one_type(window):
    with pytest.raises(InvalidContract):
        Manifest.single("moves", "daily", at(18), window=window).check(contract())


@pytest.mark.protocol("trust-model.4")
def test_source_read_boundaries_must_include_a_timezone():
    with pytest.raises(InvalidContract, match="timezone-aware"):
        Manifest.single("stock", "full", datetime(2026, 10, 18))


@pytest.mark.protocol("manifest.1")
def test_generation_feeds_must_issue_the_cursor_the_next_read_will_use():
    with pytest.raises(InvalidContract, match="generation tokens"):
        Manifest.single("moves", "feed", at(19), since="g1").check(contract())


@pytest.mark.protocol("contract.keyless-tables")
def test_append_only_history_has_an_explicit_start_and_a_keyless_full_export_needs_none():
    log = Contract("events", (), {"log": SINCE}, start_at=at(1))
    assert log.start_at == at(1)
    with pytest.raises(InvalidContract, match="start_at"):
        Contract("events", (), {"log": SINCE})
    receipts = Contract(
        "receipts",
        (),
        {"full": FULL, "plant": replace(WINDOW, window_size=None)},
        window_column="plant",
        types={"plant": "str"},
    )
    assert receipts.start_at is None
    with pytest.raises(InvalidContract, match="start_at"):
        Contract("receipts", (), {"full": FULL}, start_at=at(1))
    with pytest.raises(InvalidContract, match="append-only"):
        Contract("receipts", (), {"feed": FEED}, types={"deleted": "bool"}, deleted_column="deleted")


def test_unrecognized_shape_properties_use_the_public_contract_error():
    with pytest.raises(InvalidContract, match="invalid value for Shape.coverage"):
        Shape(coverage="sometimes", completeness="complete", deletions="absence", checkpoint="none")


@pytest.mark.protocol("contract.shapes.4")
def test_date_windows_cannot_request_fractional_calendar_days():
    from dataclasses import replace

    with pytest.raises(InvalidContract, match="whole number of days"):
        contract(shapes={"daily": replace(WINDOW, window_size=timedelta(hours=12))})


@pytest.mark.protocol("contract.shapes.4")
@pytest.mark.parametrize("column_type", ["int", "str"])
def test_a_duration_cannot_define_a_rolling_window_over_integer_or_string_partitions(column_type):
    with pytest.raises(InvalidContract, match="date or datetime"):
        contract(types={"id": "int", "day": column_type, "deleted": "bool"})


@pytest.mark.protocol("contract.shapes.5")
@pytest.mark.parametrize("lookback", [timedelta(0), timedelta(minutes=5)])
def test_lookback_belongs_to_keyed_timestamp_reads(lookback):
    shape = replace(SINCE, lookback=lookback)
    assert contract(shapes={"since": shape}).shape("since").lookback == lookback
    with pytest.raises(InvalidContract, match="keyed"):
        Contract("events", (), {"append": shape}, start_at=at(1))


@pytest.mark.protocol("contract.shapes.5")
@pytest.mark.parametrize(
    "shape,lookback", [(FULL, timedelta(0)), (FEED, timedelta(0)), (SINCE, timedelta(-1))]
)
def test_lookback_cannot_change_generation_or_complete_read_semantics(shape, lookback):
    with pytest.raises(InvalidContract, match="lookback"):
        replace(shape, lookback=lookback)


@pytest.mark.protocol("contract.types", "contract.cursor-column")
def test_a_cursor_column_must_explicitly_name_a_datetime_type():
    types = {"id": "int", "changed_at": "datetime"}
    c = Contract("stock", ("id",), {"since": SINCE}, types=types, cursor_column="changed_at")
    assert c.cursor_column == "changed_at"
    for invalid in ({}, {"id": "int"}, {"id": "int", "changed_at": "date"}):
        with pytest.raises(InvalidContract, match="cursor"):
            replace(c, types=invalid)


@pytest.mark.protocol("contract-evolution.compatibility")
def test_history_compatibility_binds_only_table_key_window_and_their_types():
    original = contract()
    evolved = replace(
        original,
        types={**original.types, "changed_at": "datetime"},
        schema_hash="new-columns",
        cursor_column="changed_at",
        shapes={**original.shapes, "another": FULL},
    )
    assert original.compatible_with(evolved)
    assert evolved.compatible_with(original)
    assert original.identity != evolved.identity
    assert original.compatible_with(replace(original, schema_hash=None))
    for incompatible in (
        replace(original, table="other"),
        replace(original, key=("day", "id")),
        replace(original, types={**original.types, "id": "str"}),
        replace(
            original, window_column=None, shapes={"full": FULL}, types={"id": "int"}, deleted_column=None
        ),
        replace(
            original,
            deleted_column="erased",
            types={**{k: v for k, v in original.types.items() if k != "deleted"}, "erased": "bool"},
        ),
    ):
        assert not original.compatible_with(incompatible)


@pytest.mark.parametrize(
    "build,message",
    [
        (lambda: bounds((1.0, 2.0)), "window bounds must be"),
        (lambda: replace(FULL, cost=-1), "cost must be finite and nonnegative"),
        (lambda: replace(WINDOW, window_size=timedelta(0)), "positive window_size"),
        (lambda: replace(WINDOW, window_size=-timedelta(days=1)), "positive window_size"),
        (lambda: replace(FULL, window_size=timedelta(days=1)), "positive window_size"),
        (lambda: contract(start_at=at(1)), "start_at belongs to append-only history"),
    ],
)
def test_declarations_refuse_unsupported_bounds_costs_sizes_and_history_starts(build, message):
    with pytest.raises(InvalidContract, match=message):
        build()


@pytest.mark.parametrize(
    "shape,kind,message",
    [
        (replace(WINDOW, window_size=None), "date", "needs a date or datetime domain"),
        (WINDOW, "int", "needs a date or datetime domain"),
        (replace(WINDOW, window_size=timedelta(hours=1)), "date", "must contain whole days"),
    ],
)
def test_derived_windows_require_a_duration_in_a_compatible_calendar_domain(shape, kind, message):
    with pytest.raises(InvalidContract, match=message):
        shape.window_ending_at(at(18), kind)


@pytest.mark.protocol("manifest.7")
def test_a_snapshot_token_is_a_nonempty_string_that_changes_the_manifests_identity():
    plain = Manifest.single("stock", "full", at(18))
    named = Manifest.single("stock", "full", at(18), snapshot="txn-42")
    assert named.snapshot == "txn-42" and named.identity != plain.identity
    with pytest.raises(InvalidContract, match="nonempty"):
        Manifest.single("stock", "full", at(18), snapshot="")


@pytest.mark.parametrize(
    "field,value",
    [
        ("key", {"id"}),
        ("key", {"id": 1}),
        ("types", [("id", "int")]),
        ("types", {"id": 1}),
        ("schema_hash", 1),
        ("shapes", [("full", FULL)]),
        ("shapes", {1: FULL}),
        ("cursor_column", 1),
    ],
)
def test_contract_constructor_rejects_invalid_types_before_normalization(field, value):
    with pytest.raises(InvalidContract):
        replace(contract(), **{field: value})


@pytest.mark.protocol("manifest.declaration")
@pytest.mark.parametrize(
    "parts",
    [
        {},
        {1: Part(shape="full")},
        {"": Part(shape="full")},
        {"stock": "full"},
        {"stock": None},
        [("stock", Part(shape="full"))],
    ],
)
def test_a_manifest_rejects_invalid_parts_without_needing_a_contract(parts):
    with pytest.raises(InvalidContract):
        Manifest(at(18), parts)


@pytest.mark.protocol("manifest.6")
@pytest.mark.parametrize(
    "field,value",
    [
        ("shape", []),
        ("shape", ""),
        ("rows", "2"),
        ("rows", True),
        ("tombstones", -1),
        ("tombstones", True),
        ("tombstones", 1.0),
        ("tombstones", None),
        ("tombstones", ((1,),)),
        ("tombstones", {1}),
        ("window", "ab"),
        ("window", {1: 2, 3: 4}),
        ("since", True),
        ("since", ""),
        ("generation", 1.5),
        ("generation", ""),
        ("schema_hash", 1),
        ("content_hash", 1),
        ("content_hash", ""),
        ("content_hash", "sha256:" + "0" * 64),
        ("content_hash", "A" * 64),
        ("content_hash", "0" * 63),
    ],
)
def test_a_part_rejects_invalid_types_without_needing_a_contract(field, value):
    with pytest.raises(InvalidContract):
        replace(Part(shape="full"), **{field: value})


def test_list_inputs_are_copied_into_immutable_typed_values():
    key = ["id"]
    types = {"id": "int"}
    c = Contract("stock", key, {"full": FULL}, types=types)
    window = [1, 2]
    m = Manifest.single("stock", "daily", at(18), window=window)
    key.clear()
    types.clear()
    window.append(3)
    assert c.key == ("id",)
    assert c.types == {"id": "int"}
    with pytest.raises(TypeError):
        c.types["id"] = "str"
    assert part_of(m).window == (1, 2)
    assert hash(c) == hash(Contract.from_json(c.to_json()))
    assert hash(m) == hash(Manifest.from_json(m.to_json()))


@pytest.mark.protocol("manifest.declaration", "versioned-json.4")
def test_the_order_of_a_reads_parts_changes_neither_equality_nor_hash_nor_identity():
    part = Part(shape="full")
    read = Manifest(at(18), {"orders": part, "order_lines": part})
    reordered = Manifest(at(18), {"order_lines": part, "orders": part})
    assert read == reordered and hash(read) == hash(reordered) and read.identity == reordered.identity
    assert len({read, reordered}) == 1 and read.tables != reordered.tables


@pytest.mark.protocol("manifest.3")
def test_window_deliveries_cannot_issue_a_table_wide_generation():
    with pytest.raises(InvalidContract, match="table generation"):
        Manifest.single(
            "moves", "daily", at(18), window=(at(17).date(), at(18).date()), generation="g1"
        ).check(contract())


@pytest.mark.protocol("manifest.6")
def test_corrected_content_changes_manifest_identity():
    original = Manifest.single("stock", "full", at(18), rows=2, content_hash="1" * 64)
    corrected = revised(original, content_hash="2" * 64)
    assert original.identity != corrected.identity
    assert Manifest.from_json(corrected.to_json()) == corrected


@pytest.mark.protocol("versioned-json.3")
@pytest.mark.parametrize("duration", [timedelta(days=100000, microseconds=1), timedelta.max])
def test_durations_that_cannot_round_trip_in_seconds_are_refused(duration):
    for build in (
        lambda: replace(SINCE, lookback=duration),
        lambda: replace(WINDOW, window_size=duration),
    ):
        with pytest.raises(InvalidContract, match="represented exactly in wire seconds"):
            build()


@pytest.mark.protocol("versioned-json.3")
def test_large_integral_durations_remain_exact_and_round_trip():
    duration = timedelta(days=999999999, seconds=86399)
    s = replace(WINDOW, window_size=duration)
    assert s.to_dict()["window_size"] == {"$seconds": 86399999999999}
    assert Shape.from_json(s.to_json()) == s
    adjacent = replace(s, window_size=duration - timedelta(seconds=1))
    assert adjacent.identity != s.identity


@pytest.mark.protocol("versioned-json.5", "contract.shapes.3")
def test_integer_costs_cannot_silently_lose_precision():
    exact = replace(FULL, cost=2**53)
    assert exact.cost == 2**53
    assert Shape.from_json(exact.to_json()) == exact
    with pytest.raises(InvalidContract, match="represented exactly as a float"):
        replace(FULL, cost=2**53 + 1)


@pytest.mark.protocol("versioned-json.3")
@pytest.mark.parametrize("value", ["\ud800", "\udfff", "plant\udc80"])
def test_lone_surrogates_cannot_enter_window_or_key_domains(value):
    from dogana import Region
    from dogana.contract import bounds

    for build in (
        lambda: Manifest.single("stock", "daily", at(18), window=(value, value)),
        lambda: Region(value, None),
        lambda: bounds((value, value)),
    ):
        with pytest.raises(InvalidContract, match="Unicode scalar"):
            build()


@pytest.mark.protocol("contract.constructors", "manifest.declaration", "manifest.1")
@pytest.mark.parametrize(
    "shape,kwargs,kind",
    [
        ("full", {}, "FullDelivery"),
        ("daily", {"window": (at(17).date(), at(18).date())}, "WindowDelivery"),
        ("since", {"since": at(17)}, "TimestampChanges"),
        ("recent", {"since": at(17), "window": (at(17).date(), at(18).date())}, "TimestampChanges"),
        ("feed", {"since": "g0", "generation": "g1"}, "GenerationChanges"),
    ],
)
def test_each_keyed_delivery_pattern_parses_its_required_fields_once(shape, kwargs, kind):
    c = contract()
    m = c.manifest(shape, at(18), **kwargs)
    parsed = m.check(c)
    assert type(parsed).__name__ == kind
    assert parsed.manifest == m and parsed.part == m.part(c.table) and parsed.shape == c.shape(shape)
    assert m.tables == (c.table,) and parsed.part.schema_hash == c.schema_hash


@pytest.mark.protocol("contract.keyless-tables")
def test_keyless_append_parses_a_timestamp_without_optional_fields():
    from dogana.contract import AppendInterval

    c = Contract("events", (), {"append": Shape.append()}, start_at=at(17))
    parsed = c.manifest("append", at(18), since=at(17)).check(c)
    assert isinstance(parsed, AppendInterval)
    assert parsed.since == at(17)


@pytest.mark.protocol(
    "contract.constructors",
    "contract.shapes.3",
    "contract.shapes.coverage",
    "contract.shapes.completeness",
    "contract.shapes.deletions",
    "contract.shapes.checkpoint",
)
def test_shape_helpers_preserve_the_four_wire_properties():
    assert Shape.full() == replace(FULL, cost=1)
    assert Shape.window(size=timedelta(days=30)) == replace(WINDOW, cost=1)
    assert Shape.changes() == replace(SINCE, cost=1)
    assert Shape.feed() == FEED
    assert Shape.append() == Shape.changes()
    assert Shape.window(size=timedelta(days=30)).window_ending_at(at(19), "date") == (
        (at(19) - timedelta(days=29)).date(),
        at(19).date(),
    )


@pytest.mark.protocol("contract.constructors")
def test_optional_declaration_fields_require_keywords():
    with pytest.raises(TypeError):
        Shape("all", "complete", "absence", "none")
    with pytest.raises(TypeError):
        Contract("stock", ("id",), {"full": FULL}, None)
    with pytest.raises(TypeError):
        Manifest.single("stock", "full", at(18), None)
    with pytest.raises(TypeError):
        Manifest(at(18), {"stock": Part(shape="full")}, None)
    with pytest.raises(TypeError):
        Part("full")


@pytest.mark.protocol("contract.types")
@pytest.mark.parametrize(
    "types", [{}, {"id": "int", "extra": "str"}, {"id": "decimal"}, {"id": "bool"}, {"id": "float"}]
)
def test_types_cover_exactly_the_bound_columns_with_valid_ordered_types(types):
    with pytest.raises(InvalidContract):
        Contract("stock", ("id",), {"full": FULL}, types=types)


@pytest.mark.protocol("contract.types")
def test_one_type_covers_a_column_shared_by_key_window_and_cursor():
    c = Contract(
        "events",
        ("at",),
        {"daily": WINDOW, "since": SINCE},
        window_column="at",
        cursor_column="at",
        types={"at": "datetime"},
    )
    assert c.types == {"at": "datetime"}
    assert part_of(c.manifest("daily", at(18), window=(at(17), at(18)))).schema_hash is None


@pytest.mark.protocol("contract.column-hash")
@pytest.mark.parametrize("digest", [None, "", "opaque:data-contract:v2"])
def test_manifest_construction_carries_the_optional_opaque_data_contract_hash(digest):
    c = Contract("stock", ("id",), {"full": FULL}, types={"id": "int"}, schema_hash=digest)
    assert part_of(c.manifest("full", at(18))).schema_hash == digest
    assert Contract.from_json(c.to_json()) == c


@pytest.mark.protocol("contract.types")
def test_unbound_accounting_needs_no_types_or_data_contract_hash():
    c = Contract("events", (), {"full": FULL})
    assert c.types == {} and c.schema_hash is None
    with pytest.raises(InvalidContract, match="exactly"):
        replace(c, types={"payload": "str"})


def test_type_mapping_order_changes_neither_contract_identity_nor_hash():
    c = contract()
    reordered = replace(c, types=dict(reversed(list(c.types.items()))))
    assert c == reordered and c.identity == reordered.identity and hash(c) == hash(reordered)


@pytest.mark.protocol("contract.deleted-column")
def test_a_deleted_column_exists_exactly_for_tombstone_shapes_and_has_its_own_boolean_type():
    feed = Contract(
        "stock", ("id",), {"feed": FEED}, types={"id": "int", "gone": "bool"}, deleted_column="gone"
    )
    assert feed.deleted_column == "gone"
    assert not feed.compatible_with(
        replace(feed, deleted_column="__deleted", types={"id": "int", "__deleted": "bool"})
    )
    with pytest.raises(InvalidContract, match="exactly one deleted column"):
        Contract("stock", ("id",), {"feed": FEED}, types={"id": "int"})
    with pytest.raises(InvalidContract, match="exactly one deleted column"):
        Contract("stock", ("id",), {"full": FULL}, types={"id": "int", "gone": "bool"}, deleted_column="gone")
    with pytest.raises(InvalidContract, match="type bool"):
        Contract("stock", ("id",), {"feed": FEED}, types={"id": "int", "gone": "str"}, deleted_column="gone")
    with pytest.raises(InvalidContract, match="column of its own"):
        Contract("stock", ("id",), {"feed": FEED}, types={"id": "int"}, deleted_column="id")
    with pytest.raises(InvalidContract, match="types must cover"):
        Contract("stock", ("id",), {"feed": FEED}, types={"id": "int"}, deleted_column="gone")


@pytest.mark.protocol("manifest.4")
def test_only_tombstone_shapes_count_tombstones_and_an_empty_interval_has_none():
    c = contract()
    counted = c.manifest("feed", at(19), since="g0", generation="g1", rows=1, tombstones=2)
    assert part_of(counted).tombstones == 2
    with pytest.raises(InvalidContract, match="only tombstone shapes"):
        c.manifest("since", at(19), since=at(18), tombstones=1)
    with pytest.raises(InvalidContract, match="empty interval"):
        c.manifest("since", at(19), since=at(19), tombstones=1)


def test_a_wrong_field_type_names_the_type_expected():
    with pytest.raises(InvalidContract, match=r"Contract\.key='id': expected tuple\[str, \.\.\.\]"):
        Contract("t", key="id", types={"id": "int"}, shapes={"full": Shape.full()})
