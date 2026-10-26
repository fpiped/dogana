"""Ordered integer positions: auto-increment ids, log sequence numbers and offsets."""

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

from dogana.reference._sources import _RowSource
from dogana.reference.duckdb import DuckDBStore

COLUMNS = {"id": "int", "seq": "int", "value": "str"}
EVENTS = {"seq": "int", "value": "str"}


def keyed(**kwargs):
    return Contract(
        "orders",
        ("id",),
        {"full": Shape.full(), "changes": Shape.changes(checkpoint=Checkpoint.SEQUENCE)},
        types={"id": "int", "seq": "int"},
        cursor_column="seq",
        schema_hash=schema_hash(COLUMNS),
        max_shrink_fraction=None,
        **kwargs,
    )


def events():
    return Contract(
        "events",
        (),
        {"log": Shape.append(checkpoint=Checkpoint.SEQUENCE)},
        types={"seq": "int"},
        cursor_column="seq",
        schema_hash=schema_hash(EVENTS),
        start_at=0,
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


def test_a_keyless_sequence_appends_exactly_from_start_at_without_gap_or_overlap():
    c = events()
    log = lambda since, through, day: Manifest(  # noqa: E731
        c.table, "log", at(day), since=since, generation=through, schema_hash=c.schema_hash
    )
    ledger = Ledger(c).apply(log(0, 10, 18)).apply(log(10, 20, 19))
    claims = ledger.claims(at(19))
    assert (ledger.generation, claims.complete_as_of, claims.deletions_as_of) == (20, at(19), at(19))
    for since, through in ((15, 25), (25, 30)):
        with pytest.raises(RejectedDelivery, match="sequence_chain"):
            ledger.apply(log(since, through, 20))
    with pytest.raises(RejectedDelivery, match="sequence_chain"):
        Ledger(c).apply(log(5, 10, 18))


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


def test_contracts_keep_one_kind_of_position_and_type_their_start():
    with pytest.raises(InvalidContract, match="opaque generation or an ordered sequence"):
        Contract(
            "orders",
            ("id",),
            {"feed": Shape.feed(), "changes": Shape.changes(checkpoint=Checkpoint.SEQUENCE)},
            types={"id": "int"},
        )
    with pytest.raises(InvalidContract, match="position for sequence appends"):
        Contract("events", (), {"log": Shape.append(checkpoint=Checkpoint.SEQUENCE)}, start_at=at(1))
    with pytest.raises(InvalidContract, match="position for sequence appends"):
        Contract("events", (), {"log": Shape.append()}, start_at=0)
    with pytest.raises(InvalidContract, match="one checkpoint"):
        Contract(
            "events",
            (),
            {"log": Shape.append(), "seq": Shape.append(checkpoint=Checkpoint.SEQUENCE)},
            start_at=0,
        )
    with pytest.raises(InvalidContract, match="datetime or int"):
        Contract(
            "orders", ("id",), {"full": Shape.full()}, types={"id": "int", "c": "str"}, cursor_column="c"
        )


def test_the_store_refuses_rows_whose_cursor_is_outside_the_declared_interval():
    c = keyed()
    with DuckDBStore(":memory:") as store:
        rows = [{"id": 1, "seq": 90, "value": "a"}]
        assert store.deliver(c, _RowSource(rows, COLUMNS), full(c, position=90, rows=1), now=at(30)).accepted
        inside = [{"id": 1, "seq": 95, "value": "b"}, {"id": 2, "seq": 100, "value": "c"}]
        accepted = store.deliver(c, _RowSource(inside, COLUMNS), changes(c, 90, 100, rows=2), now=at(30))
        assert accepted.accepted, accepted.checks
        outside = [{"id": 3, "seq": 99, "value": "d"}]
        refused = store.deliver(c, _RowSource(outside, COLUMNS), changes(c, 100, 110, 20, rows=1), now=at(30))
        assert not refused.accepted
        assert any(check.name == "rows_in_interval" and not check.passed for check in refused.checks)
        assert store.load_ledger(c).generation == 100
        assert next_extraction(store.load_ledger(c), "changes", at(21)).since == 100


def test_positions_round_trip_on_the_wire():
    c = keyed()
    ledger = Ledger(c).apply(full(c)).apply(changes(c, 100, 150))
    assert Ledger.from_json(ledger.to_json()) == ledger
    assert Manifest.from_json(changes(c, 100, 150).to_json()) == changes(c, 100, 150)
    assert Contract.from_json(events().to_json()) == events()
    assert '"generation":150' in ledger.identity_json()


def test_a_change_feed_by_log_position_repairs_deletion_date_with_its_tombstones():
    c = Contract(
        "orders",
        ("id",),
        {"full": Shape.full(), "feed": Shape.feed(checkpoint=Checkpoint.SEQUENCE)},
        types={"id": "int"},
    )
    base = Ledger(c).apply(Manifest(c.table, "full", at(18), generation=100))
    fed = base.apply(
        Manifest(c.table, "feed", at(19), since=100, generation=120, keys=((1,),), tombstones=((2,),))
    )
    claims = fed.claims(at(19))
    assert (fed.generation, claims.complete_as_of, claims.deletions_as_of) == (120, at(19), at(19))
