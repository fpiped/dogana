from datetime import UTC

import pytest

from dogana import (
    Gate,
    Group,
    InvalidContract,
    Manifest,
    Reference,
    StoreError,
    deliver_together,
)
from dogana.reference.duckdb import DuckDBStore
from dogana.reference.files import FileDrop, FilePattern, ingest_together
from tests.integration.support import csv_file
from tests.memory_store import MemoryStore
from tests.support import COLUMNS, FULL, SINCE, at, contract

LINE_COLUMNS = {"id": "int", "order_id": "int", "day": "date", "value": "str"}
ORDERS = contract("orders", shapes={"full": FULL, "since": SINCE})
LINES = contract("order_lines", shapes={"full": FULL, "since": SINCE}, columns=LINE_COLUMNS)
GROUP = Group((ORDERS, LINES), (Reference("order_lines", ("order_id",), "orders"),))


def order(key):
    return {"id": key, "day": at(18).date(), "value": "order"}


def line(key, order_id):
    return {"id": key, "order_id": order_id, "day": at(18).date(), "value": "line"}


def full(c, rows, day=18):
    return rows, Manifest(c.table, "full", at(day), rows=len(rows), schema_hash=c.schema_hash)


def test_a_group_is_read_with_its_evidence_at_one_moment():
    with DuckDBStore(":memory:") as store:
        deliveries = {"orders": full(ORDERS, [order(1)]), "order_lines": full(LINES, [line(10, 1)])}
        receipts = deliver_together(store, GROUP, deliveries, at(20))
        assert all(r.accepted for r in receipts.values())
        rows, ledgers = store.read_together(GROUP)
        assert rows == {"orders": [order(1)], "order_lines": [line(10, 1)]}
        assert ledgers == {t: r.ledger for t, r in receipts.items()}
        assert GROUP.claims(ledgers, at(20)).exact_as_of == at(18)


def test_a_store_that_cannot_publish_together_refuses_before_reading_any_source():
    store = MemoryStore()
    deliveries = {"orders": (object(), full(ORDERS, [])[1]), "order_lines": (object(), full(LINES, [])[1])}
    with pytest.raises(StoreError, match="cannot publish tables together"):
        deliver_together(store, GROUP, deliveries, at(20))
    assert store.receipts() == ()


def test_a_delivery_together_carries_one_source_per_table():
    with DuckDBStore(":memory:") as store, pytest.raises(InvalidContract, match="one source per table"):
        deliver_together(store, GROUP, {"orders": full(ORDERS, [order(1)])}, at(20))


@pytest.mark.parametrize(
    ("lines", "columns", "problem"),
    [
        ([{**line(10, 1), "order_id": "1"}], {**LINE_COLUMNS, "order_id": "str"}, "different types"),
        ([order(10)], COLUMNS, "has no column order_id"),
    ],
)
def test_a_reference_the_rows_cannot_answer_refuses_the_group(tmp_path, lines, columns, problem):
    from dogana.reference._sources import _RowSource

    lines_contract = contract("order_lines", shapes={"full": FULL, "since": SINCE}, columns=columns)
    group = Group((ORDERS, lines_contract), GROUP.references)
    with DuckDBStore(":memory:") as store:
        deliveries = {
            "orders": full(ORDERS, [order(1)]),
            "order_lines": (_RowSource(lines, columns), full(lines_contract, lines)[1]),
        }
        receipts = deliver_together(store, group, deliveries, at(20))
        failed = [c for c in receipts["orders"].checks if not c.passed]
        assert [c.name for c in failed] == ["references"] and problem in failed[0].detail
        assert store.receipts("orders")[0] == receipts["orders"]
        assert not store._candidates


def hold_and_check(store, lines):
    held, checked = {}, {}
    for c, rows in ((ORDERS, [order(1)]), (LINES, lines)):
        source, manifest = full(c, rows)
        held[c.table] = store.hold(source, manifest, c)
        checks = store.check(held[c.table], c, manifest, Gate.requirements(c, manifest))
        checked[c.table] = (store.load_ledger(c), manifest, checks)
    joint = (Gate.together(tuple(Gate.evaluate(*args, now=at(20)) for args in checked.values())),)
    return held, checked, joint


def test_receipts_that_leave_out_a_failed_references_check_cannot_publish():
    with DuckDBStore(":memory:") as store:
        held, checked, joint = hold_and_check(store, [line(10, 2)])
        references = store.check_references(GROUP, held)
        assert not references.passed and "rows without a parent row in orders: 1" in references.detail
        entries = tuple(
            (held[table], Gate.evaluate(*args, now=at(20), joint=joint)) for table, args in checked.items()
        )
        with pytest.raises(StoreError, match="does not authorize"):
            store.publish_together(GROUP, entries)
        assert not store._candidates and store.receipts() == () and not store._exists("orders")
        with pytest.raises(StoreError, match="accepted receipts only"):
            store.publish_together(GROUP, ())


@pytest.mark.parametrize("answered", ["never", "another group", "before a new check"])
def test_a_group_with_references_publishes_only_after_the_store_answered_them(answered):
    with DuckDBStore(":memory:") as store:
        held, checked, joint = hold_and_check(store, [line(10, 2)])
        if answered == "another group":
            store.check_references(Group((ORDERS, LINES)), held)
        if answered == "before a new check":
            store.check_references(GROUP, held)
            ledger, manifest, _ = checked["order_lines"]
            store.check(held["order_lines"], LINES, manifest, Gate.requirements(LINES, manifest))
        entries = tuple(
            (held[table], Gate.evaluate(*args, now=at(20), joint=joint)) for table, args in checked.items()
        )
        with pytest.raises(StoreError, match="references must be checked"):
            store.publish_together(GROUP, entries)
        assert store.receipts() == () and not store._exists("order_lines")


def test_a_lost_rollback_after_checking_references_keeps_the_trial_rows_unreadable():
    with DuckDBStore(":memory:") as store:
        held, _, _ = hold_and_check(store, [line(10, 1)])
        connection = store.connection

        class RollbackFault:
            def execute(self, sql, *args):
                if sql == "ROLLBACK":
                    raise OSError("rollback unavailable")
                return connection.execute(sql, *args)

        store.connection = RollbackFault()
        try:
            with pytest.raises(StoreError, match="rollback unavailable"):
                store.check_references(GROUP, held)
            assert store._transaction == tuple(held.values())
            with pytest.raises(StoreError, match="publish or abort"):
                store.read_publication(ORDERS)
        finally:
            store.connection = connection
            for candidate in held.values():
                store.abort(candidate)
        assert store._transaction is None and not store._exists("orders")


def test_the_files_of_a_group_are_ingested_together(tmp_path):
    pattern = r"{}-(?P<produced_at>\d{{4}}-\d{{2}}-\d{{2}})\.csv"
    with DuckDBStore(tmp_path / "group.duckdb") as store, DuckDBStore(tmp_path / "other.duckdb") as other:
        orders = FileDrop(store, ORDERS, FilePattern(pattern.format("orders"), "full", UTC), columns=COLUMNS)
        lines = FileDrop(
            store, LINES, FilePattern(pattern.format("lines"), "full", UTC), columns=LINE_COLUMNS
        )
        files = [
            (orders, csv_file(tmp_path, "orders-2026-10-18.csv", [order(1)])),
            (lines, csv_file(tmp_path, "lines-2026-10-18.csv", [line(10, 1)], LINE_COLUMNS)),
        ]
        receipts = ingest_together(GROUP, files, now=at(20))
        assert all(r.accepted for r in receipts.values())
        assert all(r.manifest.content_hash is not None for r in receipts.values())
        elsewhere = FileDrop(
            other, LINES, FilePattern(pattern.format("lines"), "full", UTC), columns=LINE_COLUMNS
        )
        with pytest.raises(InvalidContract, match="one store"):
            ingest_together(GROUP, [files[0], (elsewhere, files[1][1])], now=at(20))
        with pytest.raises(InvalidContract, match="one file per table"):
            ingest_together(GROUP, [files[0], files[0]], now=at(20))
