from datetime import UTC, date, timedelta

import pytest
from example_store import DuckDBStore, FileDrop, FilePattern, ingest_together

from dogana import (
    Gate,
    Group,
    InvalidContract,
    Manifest,
    Reference,
    StoreError,
    deliver,
    deliver_together,
)
from tests.integration.support import csv_file
from tests.memory_store import MemoryStore
from tests.support import COLUMNS, FULL, SINCE, WINDOW, at, contract, fold

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


def since(c, rows, day=19):
    return rows, Manifest(
        c.table, "since", at(day), since=at(day - 1), rows=len(rows), schema_hash=c.schema_hash
    )


def test_a_group_is_read_with_its_evidence_at_one_moment():
    with DuckDBStore(":memory:") as store:
        deliveries = {"orders": full(ORDERS, [order(1)]), "order_lines": full(LINES, [line(10, 1)])}
        receipts = deliver_together(store, GROUP, deliveries, at(20))
        assert all(r.accepted for r in receipts.values())
        rows, ledgers = store.read_together(GROUP)
        assert rows == {"orders": [order(1)], "order_lines": [line(10, 1)]}
        assert ledgers == {t: fold(r) for t, r in receipts.items()}
        assert GROUP.claims(ledgers, at(20), limit=timedelta.max).exact_as_of == at(18)


def test_a_store_that_cannot_publish_together_refuses_before_reading_any_source():
    store = MemoryStore()
    deliveries = {"orders": (object(), full(ORDERS, [])[1]), "order_lines": (object(), full(LINES, [])[1])}
    with pytest.raises(StoreError, match="cannot publish tables together"):
        deliver_together(store, GROUP, deliveries, at(20))
    assert store.receipts() == ()


def test_each_file_drop_uses_its_exact_group_contract_before_reading_files():
    from dataclasses import replace

    with DuckDBStore(":memory:") as store:
        changed = replace(ORDERS, shapes={"full": replace(FULL, cost=999)})
        drop = FileDrop(store, changed, columns=COLUMNS)
        with pytest.raises(
            InvalidContract, match="each drop's contract must be its table's contract in the group"
        ):
            ingest_together(GROUP, [(drop, "missing.csv")], now=at(20))
        assert store.receipts() == ()


def test_a_delivery_together_carries_one_source_per_table():
    with DuckDBStore(":memory:") as store, pytest.raises(InvalidContract, match="one source per table"):
        deliver_together(store, GROUP, {"orders": full(ORDERS, [order(1)])}, at(20))


@pytest.mark.protocol("tables-together.when-a-reference-is-tested")
@pytest.mark.parametrize(
    ("lines", "columns", "problem"),
    [
        ([{**line(10, 1), "order_id": "1"}], {**LINE_COLUMNS, "order_id": "str"}, "different types"),
        ([order(10)], COLUMNS, "has no column order_id"),
    ],
)
def test_a_reference_the_rows_cannot_answer_refuses_the_group(tmp_path, lines, columns, problem):
    from example_store import Rows

    lines_contract = contract("order_lines", shapes={"full": FULL, "since": SINCE}, columns=columns)
    group = Group((ORDERS, lines_contract), GROUP.references)
    with DuckDBStore(":memory:") as store:
        deliveries = {
            "orders": full(ORDERS, [order(1)]),
            "order_lines": (Rows(lines, columns), full(lines_contract, lines)[1]),
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


@pytest.mark.protocol("tables-together.when-a-reference-is-tested")
def test_an_orphan_names_its_reference_values_and_an_unchecked_reference_says_why():
    def references(receipts):
        return next(c.detail for c in receipts["orders"].checks if c.name == "references")

    with DuckDBStore(":memory:") as store:
        deliveries = {"orders": full(ORDERS, [order(1)]), "order_lines": full(LINES, [line(10, 1)])}
        assert all(r.accepted for r in deliver_together(store, GROUP, deliveries, at(20)).values())
        orphans = {
            "orders": full(ORDERS, [order(1)], 19),
            "order_lines": full(LINES, [line(11, 2), line(12, 3), line(13, 3)], 19),
        }
        assert references(deliver_together(store, GROUP, orphans, at(20))) == (
            "order_lines(order_id) references orders: "
            "rows without a parent row in orders: 3, naming order_id 2, 3"
        )
        unchecked = {"orders": since(ORDERS, [order(5)]), "order_lines": since(LINES, [line(11, 2)])}
        receipts = deliver_together(store, GROUP, unchecked, at(20))
        assert all(r.accepted for r in receipts.values())
        assert references(receipts) == (
            "order_lines(order_id) references orders: not checked, "
            "order_lines is not exact as of 2026-10-19T00:00:00+00:00 where it was delivered"
        )


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
    # One export job reads both tables in one snapshot and stamps both files with its instant.
    pattern = r"{}-(?P<produced_at>\d{{8}}T\d{{6}}Z)\.csv"
    with DuckDBStore(tmp_path / "group.duckdb") as store, DuckDBStore(tmp_path / "other.duckdb") as other:
        orders = FileDrop(store, ORDERS, FilePattern(pattern.format("orders"), "full", UTC), columns=COLUMNS)
        lines = FileDrop(
            store, LINES, FilePattern(pattern.format("lines"), "full", UTC), columns=LINE_COLUMNS
        )
        files = [
            (orders, csv_file(tmp_path, "orders-20261018T021300Z.csv", [order(1)])),
            (lines, csv_file(tmp_path, "lines-20261018T021300Z.csv", [line(10, 1)], LINE_COLUMNS)),
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


@pytest.mark.protocol("tables-together.replay-and-races.2")
@pytest.mark.parametrize("meanwhile", ["alone", "together", "torn"])
def test_what_another_delivery_accepts_meanwhile_is_decided_as_a_replay(monkeypatch, meanwhile):
    with DuckDBStore(":memory:") as store:
        deliveries = {"orders": full(ORDERS, [order(1)]), "order_lines": full(LINES, [line(10, 1)])}
        answer = store.check_references

        def race(group, candidates):
            result = answer(group, candidates)
            monkeypatch.setattr(store, "check_references", answer)
            if meanwhile == "alone":
                assert deliver(store, ORDERS, *deliveries["orders"], at(21)).accepted
            else:
                assert all(r.accepted for r in deliver_together(store, GROUP, deliveries, at(21)).values())
            if meanwhile == "torn":
                # The first recovery lookup of orders runs before the other commit lands.
                lookup, missed = store.accepted_receipt, []

                def torn(identity):
                    if identity == deliveries["orders"][1].identity and not missed:
                        missed.append(identity)
                        return None
                    return lookup(identity)

                monkeypatch.setattr(store, "accepted_receipt", torn)
            return result

        monkeypatch.setattr(store, "check_references", race)
        receipts = deliver_together(store, GROUP, deliveries, at(20))
        assert not store._candidates
        if meanwhile == "alone":
            assert not any(r.accepted for r in receipts.values())
            assert receipts["order_lines"].checks[-1].detail == (
                "a group replays whole, and orders was not accepted together with this group"
            )
            assert not store._exists("order_lines")
        else:
            assert all(r.replayed and r.received_at == at(21) for r in receipts.values())


@pytest.mark.protocol("tables-together.replay-and-races.1")
def test_a_replay_is_one_delivery_of_these_files_under_these_references():
    from example_store import Rows

    shapes = {"full": FULL, "daily": WINDOW}
    orders = contract("orders", shapes=shapes)
    lines = contract("order_lines", shapes=shapes, columns=LINE_COLUMNS)
    group = Group((orders, lines), (Reference("order_lines", ("order_id",), "orders"),))

    def daily(c, columns, day):
        window = (at(day).date(), at(day).date())
        manifest = Manifest(c.table, "daily", at(19), window=window, rows=0, schema_hash=c.schema_hash)
        return Rows([], columns), manifest

    with DuckDBStore(":memory:") as store:
        baseline = {"orders": full(orders, [order(1)]), "order_lines": full(lines, [line(10, 1)])}
        assert all(r.accepted for r in deliver_together(store, group, baseline, at(20)).values())
        first = {"orders": daily(orders, COLUMNS, 19), "order_lines": daily(lines, LINE_COLUMNS, 19)}
        second = {"orders": daily(orders, COLUMNS, 18), "order_lines": daily(lines, LINE_COLUMNS, 18)}
        for deliveries in (first, second):
            assert all(r.accepted for r in deliver_together(store, group, deliveries, at(20)).values())
        mixed = {"orders": first["orders"], "order_lines": second["order_lines"]}
        assert not any(r.accepted for r in deliver_together(store, group, mixed, at(20)).values())
        assert not any(
            r.accepted for r in deliver_together(store, Group((orders, lines)), first, at(20)).values()
        )
        assert all(r.replayed for r in deliver_together(store, group, first, at(20)).values())


def test_a_group_replay_with_one_file_changed_refuses_every_table(tmp_path):
    from example_store._sources import _file_digest, _FileSource

    with DuckDBStore(":memory:") as store:
        files, deliveries = {}, {}
        for c, columns, rows in ((ORDERS, COLUMNS, [order(1)]), (LINES, LINE_COLUMNS, [line(10, 1)])):
            files[c.table] = csv_file(tmp_path, f"{c.table}.csv", rows, columns)
            manifest = Manifest(
                c.table,
                "full",
                at(18),
                rows=1,
                schema_hash=c.schema_hash,
                content_hash=_file_digest(files[c.table]),
            )
            deliveries[c.table] = (_FileSource(files[c.table], columns), manifest)
        assert all(r.accepted for r in deliver_together(store, GROUP, deliveries, at(20)).values())
        csv_file(tmp_path, "order_lines.csv", [line(10, 1), line(11, 1)], LINE_COLUMNS)
        receipts = deliver_together(store, GROUP, deliveries, at(20))
        assert not any(r.accepted for r in receipts.values())
        assert receipts["orders"].checks[-1].detail == (
            "a group replays whole, and order_lines differs from what was accepted"
        )


def test_a_candidate_stays_bound_to_the_contract_it_was_first_checked_under():
    with DuckDBStore(":memory:") as store:
        rows, manifest = full(ORDERS, [order(1)])
        held = store.hold(rows, manifest)
        store.check(held, ORDERS, manifest, Gate.requirements(ORDERS, manifest))
        other = contract("orders", shapes={"full": FULL})
        with pytest.raises(StoreError, match="different manifest"):
            store.check(held, other, manifest, Gate.requirements(other, manifest))


def test_stores_on_one_file_share_its_lock_whatever_spelling_names_it(tmp_path):
    import os

    lower, upper = tmp_path / "group.duckdb", tmp_path / "GROUP.duckdb"
    with DuckDBStore(lower) as first:
        if not os.path.exists(upper) or not os.path.samefile(lower, upper):
            pytest.skip("the file system tells letter case apart")
        with DuckDBStore(upper) as second:
            assert first._lock is second._lock


def test_closing_a_store_whose_rollback_failed_releases_the_file_for_other_threads(tmp_path):
    from threading import Thread

    path = tmp_path / "group.duckdb"
    store, other = DuckDBStore(path), DuckDBStore(path)
    rows, manifest = full(ORDERS, [order(1)])
    held = store.hold(rows, manifest, ORDERS)
    store.check(held, ORDERS, manifest, Gate.requirements(ORDERS, manifest))
    store._open((held,))
    connection = store.connection

    class RollbackFault:
        def execute(self, sql, *args):
            if sql == "ROLLBACK":
                raise OSError("rollback unavailable")
            return connection.execute(sql, *args)

        def close(self):
            connection.close()

    store.connection = RollbackFault()
    with pytest.raises(StoreError, match="rollback unavailable"):
        store.close()
    answered = []
    reader = Thread(target=lambda: answered.append(other.receipts()))
    reader.start()
    reader.join(5)
    assert answered == [()]
    other.close()


def test_a_window_to_the_end_of_the_dates_covers_values_past_what_python_can_name(tmp_path):
    shapes = {"full": FULL, "daily": WINDOW}
    moves = contract("moves", shapes=shapes)
    path = tmp_path / "far.csv"
    path.write_text("id,day,value\n1,infinity,far\n2,2026-10-18,near\n")
    with DuckDBStore(":memory:") as store:
        from example_store._sources import _FileSource

        first = Manifest("moves", "full", at(18), rows=2, schema_hash=moves.schema_hash)
        assert deliver(store, moves, _FileSource(path, COLUMNS), first, at(20)).accepted
        window = (date(2026, 1, 1), date.max)
        empty = Manifest("moves", "daily", at(19), window=window, rows=0, schema_hash=moves.schema_hash)
        assert deliver(
            store, moves, _FileSource(csv_file(tmp_path, "none.csv", []), COLUMNS), empty, at(20)
        ).accepted
        assert store.connection.execute("SELECT count(*) FROM moves").fetchone() == (0,)


@pytest.mark.parametrize(
    ("values", "kind"),
    [([None, None], "str"), ([1.0, None], "float")],
)
def test_a_reference_compares_values_not_type_names(values, kind):
    from example_store import Rows

    columns = {**LINE_COLUMNS, "order_id": kind}
    lines = contract("order_lines", shapes={"full": FULL, "since": SINCE}, columns=columns)
    rows = [{**line(10 + i, None), "order_id": value} for i, value in enumerate(values)]
    with DuckDBStore(":memory:") as store:
        deliveries = {
            "orders": full(ORDERS, [order(1)]),
            "order_lines": (Rows(rows, columns), full(lines, rows)[1]),
        }
        receipts = deliver_together(store, Group((ORDERS, lines), GROUP.references), deliveries, at(20))
        assert all(r.accepted for r in receipts.values()), [
            c for c in receipts["orders"].checks if not c.passed
        ]


def test_a_second_name_for_an_open_database_file_is_refused(tmp_path):
    import os

    path, link = tmp_path / "group.duckdb", tmp_path / "linked.duckdb"
    with DuckDBStore(path) as store:
        os.link(path, link)
        with pytest.raises(StoreError, match="already open as"):
            DuckDBStore(link)
        assert store.receipts() == ()


def test_the_log_is_unreadable_while_a_transaction_is_unresolved():
    with DuckDBStore(":memory:") as store:
        rows, manifest = full(ORDERS, [order(1)])
        held = store.hold(rows, manifest, ORDERS)
        store.check(held, ORDERS, manifest, Gate.requirements(ORDERS, manifest))
        store._open((held,))
        try:
            for read in (
                store.receipts,
                lambda: store.accepted_receipt(manifest.identity),
                lambda: store.publication_stamp("orders"),
            ):
                with pytest.raises(StoreError, match="publish or abort"):
                    read()
        finally:
            store.abort(held)


@pytest.mark.parametrize(("value", "names"), [(9007199254740992.0, False), (1.5, False), (2.0, True)])
def test_a_float_reference_names_an_int_key_only_with_its_exact_integer(value, names):
    from example_store import Rows

    columns = {**LINE_COLUMNS, "order_id": "float"}
    lines = contract("order_lines", shapes={"full": FULL, "since": SINCE}, columns=columns)
    rows = [{**line(10, None), "order_id": value}]
    parents = [order(1), order(2), order(2**53 + 1)]
    with DuckDBStore(":memory:") as store:
        deliveries = {
            "orders": full(ORDERS, parents),
            "order_lines": (Rows(rows, columns), full(lines, rows)[1]),
        }
        receipts = deliver_together(store, Group((ORDERS, lines), GROUP.references), deliveries, at(20))
        assert all(r.accepted for r in receipts.values()) is names


def test_a_quoted_empty_field_is_the_empty_string_and_an_unquoted_one_is_null(tmp_path):
    from example_store._sources import _FileSource

    path = tmp_path / "values.csv"
    path.write_text('id,day,value\n1,2026-10-18,""\n2,2026-10-18,\n')
    moves = contract("moves", shapes={"full": FULL})
    with DuckDBStore(":memory:") as store:
        manifest = Manifest("moves", "full", at(18), rows=2, schema_hash=moves.schema_hash)
        assert deliver(store, moves, _FileSource(path, COLUMNS), manifest, at(20)).accepted
        assert [row["value"] for row in store.read("moves")] == ["", None]


def test_an_int_window_bound_past_bigint_covers_every_stored_value():
    from dataclasses import replace

    from dogana import Contract

    columns = {"id": "int", "seq": "int", "value": "str"}
    shapes = {"full": FULL, "range": replace(WINDOW, window_size=None)}
    moves = Contract(
        "moves",
        ("id",),
        shapes,
        window_column="seq",
        types={"id": "int", "seq": "int"},
    )
    with DuckDBStore(":memory:") as store:
        assert deliver(
            store,
            moves,
            [{"id": 1, "seq": 5, "value": "x"}],
            Manifest("moves", "full", at(18), rows=1),
            at(20),
        ).accepted
        window = (0, 2**127)
        empty = Manifest("moves", "range", at(19), window=window, rows=0)
        from example_store import Rows

        assert deliver(store, moves, Rows([], columns), empty, at(20)).accepted
        assert store.read("moves") == []


def test_a_replay_whose_bytes_no_longer_read_is_refused_in_every_table(tmp_path):
    from example_store._sources import _file_digest, _FileSource

    with DuckDBStore(":memory:") as store:
        deliveries = {}
        for c, columns, rows in ((ORDERS, COLUMNS, [order(1)]), (LINES, LINE_COLUMNS, [line(10, 1)])):
            path = csv_file(tmp_path, f"{c.table}.csv", rows, columns)
            manifest = Manifest(
                c.table, "full", at(18), rows=1, schema_hash=c.schema_hash, content_hash=_file_digest(path)
            )
            deliveries[c.table] = (_FileSource(path, columns), manifest)
        assert all(r.accepted for r in deliver_together(store, GROUP, deliveries, at(20)).values())
        (tmp_path / "order_lines.csv").write_text("id,order_id,day,value\nnot a number,1,2026-10-18,x\n")
        receipts = deliver_together(store, GROUP, deliveries, at(20))
        assert not any(r.accepted for r in receipts.values())
        assert {t: len([r for r in store.receipts(t) if not r.accepted]) for t in GROUP.tables} == {
            "orders": 1,
            "order_lines": 1,
        }
        single = deliver(store, LINES, *deliveries["order_lines"], at(20))
        assert not single.accepted
