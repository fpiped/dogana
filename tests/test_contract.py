from datetime import datetime

import pytest

from dogana import Checkpoint, Completeness, Contract, Coverage, Deletions, InvalidContract, Manifest, Shape

FULL = Shape(Coverage.ALL, Completeness.COMPLETE, Deletions.ABSENCE, Checkpoint.NONE)
WINDOW = Shape(Coverage.WINDOW, Completeness.COMPLETE, Deletions.ABSENCE, Checkpoint.NONE)
SINCE = Shape(Coverage.ALL, Completeness.CHANGED_ONLY, Deletions.NONE, Checkpoint.TIMESTAMP)
FEED = Shape(Coverage.KEYS, Completeness.CHANGED_ONLY, Deletions.TOMBSTONE, Checkpoint.GENERATION)


def test_the_fourteen_valid_shapes_and_no_other():
    valid = 0
    for coverage in Coverage:
        for completeness in Completeness:
            for deletions in Deletions:
                for checkpoint in Checkpoint:
                    try:
                        Shape(coverage, completeness, deletions, checkpoint)
                        valid += 1
                    except InvalidContract:
                        pass
    assert valid == 14


@pytest.mark.parametrize("shape", [
    (Coverage.ALL, Completeness.COMPLETE, Deletions.ABSENCE, Checkpoint.TIMESTAMP),
    (Coverage.ALL, Completeness.COMPLETE, Deletions.TOMBSTONE, Checkpoint.NONE),
    (Coverage.ALL, Completeness.CHANGED_ONLY, Deletions.ABSENCE, Checkpoint.TIMESTAMP),
    (Coverage.ALL, Completeness.CHANGED_ONLY, Deletions.NONE, Checkpoint.NONE),
    (Coverage.KEYS, Completeness.COMPLETE, Deletions.ABSENCE, Checkpoint.NONE),
])
def test_shapes_the_protocol_refuses(shape):
    with pytest.raises(InvalidContract):
        Shape(*shape)


def test_a_contract_binds_its_shapes_to_the_table_identity():
    Contract("moves", ("id",), {"daily": WINDOW, "weekly": FULL}, window_column="day")
    Contract("events", (), {"log": SINCE})
    for args in [
        ("t", ("id",), {}, None),
        ("t", ("id",), {"daily": WINDOW}, None),
        ("t", ("id",), {"full": FULL}, "day"),
        ("t", (), {"full": FULL}, None),
        ("t", (), {"feed": FEED}, None),
    ]:
        with pytest.raises(InvalidContract):
            Contract(*args)


def test_a_manifest_must_name_an_admitted_shape_and_match_it():
    c = Contract("t", ("id",), {"full": FULL, "daily": WINDOW, "feed": FEED}, window_column="day")
    at = datetime(2026, 10, 22, 8)
    assert Manifest("t", "full", at).check(c) is FULL
    assert Manifest("t", "daily", at, window=(1, 30)).check(c) is WINDOW
    assert Manifest("t", "feed", at, since="g41").check(c) is FEED
    for manifest in [
        Manifest("other", "full", at),
        Manifest("t", "monthly", at),
        Manifest("t", "full", at, window=(1, 30)),
        Manifest("t", "daily", at),
        Manifest("t", "daily", at, window=(30, 1)),
        Manifest("t", "feed", at),
        Manifest("t", "full", at, since="g41"),
    ]:
        with pytest.raises(InvalidContract):
            manifest.check(c)
