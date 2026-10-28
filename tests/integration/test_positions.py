"""Ordered integer positions: log sequence numbers and offsets."""

import pytest

from dogana import (
    Checkpoint,
    Contract,
    InvalidContract,
    Ledger,
    Manifest,
    RejectedDelivery,
    Shape,
    next_extraction,
)
from tests.support import at, schema_hash

pytest.importorskip("duckdb")

from example_store import DuckDBStore, Rows

COLUMNS = {"id": "int", "seq": "int", "value": "str"}


def keyed(**kwargs):
    return Contract(
        "orders",
        ("id",),
        {"full": Shape.full(), "changes": Shape.changes(checkpoint=Checkpoint.SEQUENCE)},
        types={"id": "int", "seq": "int"},
        cursor_column="seq",
        schema_hash=schema_hash(COLUMNS),
        **kwargs,
    )


def full(c, day=18, position=100, rows=None):
    return Manifest(c.table, "full", at(day), generation=position, rows=rows, schema_hash=c.schema_hash)


def changes(c, since, through, day=19, rows=None):
    return Manifest(
        c.table, "changes", at(day), since=since, generation=through, rows=rows, schema_hash=c.schema_hash
    )


def test_a_keyed_sequence_continues_from_the_position_and_may_reread_but_never_skips():
    c = keyed()
    ledger = Ledger(c).apply(full(c)).apply(changes(c, 100, 150))
    assert ledger.generation == 150
    assert ledger.apply(changes(c, 120, 160, day=20)).generation == 160
    with pytest.raises(RejectedDelivery, match="sequence_chain"):
        ledger.apply(changes(c, 170, 180, day=20))
    with pytest.raises(RejectedDelivery, match="sequence_chain"):
        ledger.apply(changes(c, 120, 140, day=20))


def test_a_sequence_needs_a_position_issued_by_a_complete_delivery():
    c = keyed()
    without = Ledger(c).apply(Manifest(c.table, "full", at(18), schema_hash=c.schema_hash))
    with pytest.raises(RejectedDelivery, match="sequence_chain"):
        without.apply(changes(c, 100, 150))
    with pytest.raises(InvalidContract, match="kind of position"):
        Manifest(c.table, "full", at(18), generation="g1", schema_hash=c.schema_hash).check(c)


@pytest.mark.parametrize(
    ("since", "through", "rows", "message"),
    [
        ("100", 150, None, "integer since and generation"),
        (100, "150", None, "integer since and generation"),
        (True, 150, None, "Manifest.since"),
        (150, 100, None, "cannot end before its start"),
        (100, 100, 1, "empty interval"),
    ],
)
def test_sequence_manifests_need_ordered_integer_bounds(since, through, rows, message):
    c = keyed()
    with pytest.raises(InvalidContract, match=message):
        changes(c, since, through, rows=rows).check(c)


def test_contracts_keep_one_kind_of_position_and_a_cursor_that_checks_something():
    with pytest.raises(InvalidContract, match="opaque generation or an ordered sequence"):
        Contract(
            "orders",
            ("id",),
            {"feed": Shape.feed(), "changes": Shape.changes(checkpoint=Checkpoint.SEQUENCE)},
            types={"id": "int", "deleted": "bool"},
            deleted_column="deleted",
        )
    with pytest.raises(InvalidContract, match="append-only timestamp intervals"):
        Contract("events", (), {"log": Shape.changes(checkpoint=Checkpoint.SEQUENCE)}, start_at=at(1))
    with pytest.raises(InvalidContract, match="int cursor_column checks none of the changed-only shapes"):
        Contract(
            "orders",
            ("id",),
            {"changes": Shape.changes()},
            types={"id": "int", "seq": "int"},
            cursor_column="seq",
        )
    with pytest.raises(
        InvalidContract, match="datetime cursor_column checks none of the changed-only shapes"
    ):
        Contract(
            "orders",
            ("id",),
            {"changes": Shape.changes(checkpoint=Checkpoint.SEQUENCE)},
            types={"id": "int", "at": "datetime"},
            cursor_column="at",
        )
    with pytest.raises(InvalidContract, match="datetime or int"):
        Contract(
            "orders", ("id",), {"full": Shape.full()}, types={"id": "int", "c": "str"}, cursor_column="c"
        )


def test_a_timestamp_read_may_issue_position_zero():
    c = Contract(
        "orders",
        ("id",),
        {
            "full": Shape.full(),
            "since": Shape.changes(),
            "changes": Shape.changes(checkpoint=Checkpoint.SEQUENCE),
        },
        types={"id": "int"},
    )
    ledger = Ledger(c).apply(Manifest(c.table, "full", at(18), generation=7))
    ledger = ledger.apply(Manifest(c.table, "since", at(19), since=at(18), generation=0))
    assert ledger.generation == 0
    assert ledger.apply(Manifest(c.table, "changes", at(20), since=0, generation=3)).generation == 3


def test_the_store_refuses_rows_whose_cursor_is_outside_the_declared_interval():
    c = keyed()
    with DuckDBStore(":memory:") as store:
        rows = [{"id": 1, "seq": 90, "value": "a"}]
        assert store.deliver(c, Rows(rows, COLUMNS), full(c, position=90, rows=1), now=at(30)).accepted
        inside = [{"id": 1, "seq": 95, "value": "b"}, {"id": 2, "seq": 100, "value": "c"}]
        accepted = store.deliver(c, Rows(inside, COLUMNS), changes(c, 90, 100, rows=2), now=at(30))
        assert accepted.accepted, accepted.checks
        outside = [{"id": 3, "seq": 99, "value": "d"}]
        refused = store.deliver(c, Rows(outside, COLUMNS), changes(c, 100, 110, 20, rows=1), now=at(30))
        assert not refused.accepted
        assert any(check.name == "rows_in_interval" and not check.passed for check in refused.checks)
        assert store.load_ledger(c).generation == 100
        assert next_extraction(store.load_ledger(c), "changes", at(21)).since == 100


def test_positions_round_trip_on_the_wire():
    c = keyed()
    ledger = Ledger(c).apply(full(c)).apply(changes(c, 100, 150))
    assert Ledger.from_json(ledger.to_json()) == ledger
    assert Manifest.from_json(changes(c, 100, 150).to_json()) == changes(c, 100, 150)
    assert Contract.from_json(c.to_json()) == c
    assert '"generation":150' in ledger.identity_json()


def test_a_change_feed_by_log_position_repairs_deletion_date_with_its_tombstones():
    c = Contract(
        "orders",
        ("id",),
        {"full": Shape.full(), "feed": Shape.feed(checkpoint=Checkpoint.SEQUENCE)},
        types={"id": "int", "deleted": "bool"},
        deleted_column="deleted",
    )
    base = Ledger(c).apply(Manifest(c.table, "full", at(18), generation=100))
    fed = base.apply(Manifest(c.table, "feed", at(19), since=100, generation=120, rows=1, tombstones=1))
    claims = fed.claims(at(19))
    assert (fed.generation, claims.complete_as_of, claims.deletions_as_of) == (120, at(19), at(19))
