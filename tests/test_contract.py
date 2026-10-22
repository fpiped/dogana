from dataclasses import FrozenInstanceError
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


def test_the_fourteen_semantic_shapes_and_no_other_are_admitted():
    valid = []
    for properties in product(Coverage, Completeness, Deletions, Checkpoint):
        try:
            valid.append(Shape(*properties))
        except InvalidContract:
            pass
    assert len(valid) == 14


@pytest.mark.parametrize(
    "properties",
    [
        ("all", "complete", "absence", "timestamp"),
        ("all", "complete", "tombstone", "none"),
        ("all", "changed_only", "absence", "timestamp"),
        ("all", "changed_only", "none", "none"),
        ("keys", "complete", "absence", "none"),
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
        {"key": ()},
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


def test_append_only_history_has_an_explicit_start_instead_of_an_impossible_full_export():
    log = Contract("events", (), {"log": SINCE}, start_at=at(1))
    assert log.start_at == at(1)
    with pytest.raises(InvalidContract):
        Contract("events", (), {"log": SINCE})


def test_unrecognized_shape_properties_use_the_public_contract_error():
    with pytest.raises(InvalidContract, match="closed vocabularies"):
        Shape("sometimes", "complete", "absence", "none")


def test_date_windows_cannot_request_fractional_calendar_days():
    from dataclasses import replace

    with pytest.raises(InvalidContract, match="whole number of days"):
        contract(shapes={"daily": replace(WINDOW, window_size=timedelta(hours=12))})


@pytest.mark.parametrize("column_type", ["int", "str"])
def test_a_duration_cannot_define_a_rolling_window_over_integer_or_string_partitions(column_type):
    with pytest.raises(InvalidContract, match="date or datetime"):
        contract(schema=(Column("id", "int"), Column("day", column_type), Column("value", "str")))
