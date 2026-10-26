"""A finite window domain: per-plant or per-company partitions that together close the table."""

import pytest

from dogana import Contract, InvalidContract, Ledger, Manifest, RejectedDelivery, Shape, State
from tests.support import at, schema_hash

pytest.importorskip("duckdb")

from dogana.reference._sources import _RowSource
from dogana.reference.duckdb import DuckDBStore

COLUMNS = {"id": "int", "plant": "str"}
PLANTS = ("A", "B", "C")


def plants(domain=PLANTS):
    return Contract(
        "stock",
        ("id",),
        {"full": Shape.full(), "plant": Shape.window(), "changes": Shape.changes()},
        window_column="plant",
        types={"id": "int", "plant": "str"},
        schema_hash=schema_hash(COLUMNS),
        max_shrink_fraction=None,
        domain=domain,
    )


def per_plant(c, day=18):
    ledger = Ledger(c)
    for plant in PLANTS:
        ledger = ledger.apply(Manifest(c.table, "plant", at(day), window=(plant, plant)))
    return ledger


def test_every_declared_partition_delivered_closes_the_whole_table():
    assert per_plant(plants()).claims(at(18)).state is State.COMPLETE
    assert per_plant(plants(domain=None)).claims(at(18)).state is State.UNKNOWN


def test_changes_over_the_whole_table_need_only_the_declared_partitions_as_base():
    changes = Manifest("stock", "changes", at(19), since=at(18))
    claims = per_plant(plants()).apply(changes).claims(at(19))
    assert (claims.state, claims.complete_as_of, claims.valid_as_of) == (State.PATCHED, at(19), at(18))
    with pytest.raises(RejectedDelivery, match="known_base"):
        per_plant(plants(domain=None)).apply(changes)


def test_windows_name_declared_partitions():
    c = plants()
    with pytest.raises(InvalidContract, match="domain"):
        Manifest(c.table, "plant", at(18), window=("D", "D")).check(c)
    assert Manifest(c.table, "plant", at(18), window=("A", "B")).check(c)


@pytest.mark.parametrize(
    ("domain", "message"),
    [((), "values a window column takes"), ((1, 2), "window column's type"), (("B", "A"), "ascending")],
)
def test_a_domain_lists_ascending_values_of_the_window_column(domain, message):
    with pytest.raises(InvalidContract, match=message):
        plants(domain=domain)
    with pytest.raises(InvalidContract, match="values a window column takes"):
        Contract("stock", ("id",), {"full": Shape.full()}, types={"id": "int"}, domain=("A",))


def test_the_store_refuses_rows_outside_the_domain():
    c = plants()
    rows = [{"id": 1, "plant": "A"}, {"id": 2, "plant": "D"}]
    with DuckDBStore(":memory:") as store:
        manifest = Manifest(c.table, "full", at(18), rows=2, schema_hash=c.schema_hash)
        refused = store.deliver(c, _RowSource(rows, COLUMNS), manifest, now=at(30))
        assert not refused.accepted
        assert any(check.name == "rows_in_domain" and not check.passed for check in refused.checks)
        manifest = Manifest(c.table, "full", at(18), rows=1, schema_hash=c.schema_hash)
        accepted = store.deliver(c, _RowSource(rows[:1], COLUMNS), manifest, now=at(30))
        assert accepted.accepted, accepted.checks


def test_a_domain_round_trips_and_leaves_other_identities_alone():
    c = plants()
    assert Contract.from_json(c.to_json()) == c
    assert '"domain"' not in plants(domain=None).identity_json()
