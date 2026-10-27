from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from time import perf_counter

import pytest

pytest.importorskip("duckdb")

from dogana import Contract, Ledger, Shape, deliver, next_extraction
from dogana.reference._sources import Rows
from dogana.reference.duckdb import DuckDBStore
from tests.support import at, fold, schema_hash


@pytest.mark.parametrize("after_commit", [False, True])
def test_a_cdc_stream_restart_reads_the_committed_offset_and_converges(tmp_path, monkeypatch, after_commit):
    columns = {"id": "int", "value": "str"}
    c = Contract(
        "stock",
        ("id",),
        {"snapshot": Shape.full(), "cdc": Shape.feed()},
        types={"id": "int", "deleted": "bool"},
        schema_hash=schema_hash(columns),
        deleted_column="deleted",
    )
    path = tmp_path / "stream.duckdb"
    snapshot = c.manifest("snapshot", at(18), rows=2, generation="log:100")
    batch = c.manifest(
        "cdc",
        at(19),
        since="log:100",
        generation="log:103",
        rows=2,
        tombstones=1,
    )
    rows = [{"id": 2, "value": "updated"}, {"id": 3, "value": "new"}]
    delivered = [*({**row, "deleted": False} for row in rows), {"id": 1, "value": None, "deleted": True}]
    original = [{"id": 1, "value": "old"}, {"id": 2, "value": "old"}]
    with DuckDBStore(path) as store:
        first = deliver(store, c, original, snapshot, now=at(18))
        assert first.accepted
        connection = store.connection

        class CommitFault:
            def executemany(self, sql, parameters):
                return connection.executemany(sql, parameters)

            def execute(self, sql, *args):
                if sql == "COMMIT":
                    if after_commit:
                        connection.execute(sql, *args)
                    raise KeyboardInterrupt("consumer stopped at commit")
                return connection.execute(sql, *args)

        with monkeypatch.context() as patch:
            patch.setattr(store, "connection", CommitFault())
            with pytest.raises(KeyboardInterrupt, match="consumer stopped at commit"):
                deliver(store, c, delivered, batch, now=at(19))

    with DuckDBStore(path) as restarted:
        held = restarted.load_ledger(c)
        stored = restarted.accepted_receipt(batch.identity)
        assert held.generation == ("log:103" if after_commit else "log:100")
        assert (stored is not None) is after_commit
        assert restarted.read(c.table) == (rows if after_commit else original)
        assert len(restarted.receipts(c.table)) == (2 if after_commit else 1)
        retried = deliver(restarted, c, delivered, batch, now=at(20))
        assert retried.accepted
        assert retried.replayed is after_commit
        if stored is not None:
            assert retried == replace(stored, outcome="replayed")
            assert retried.identity == stored.identity
            assert retried.received_at == at(19)
        ledger = Ledger(c).apply(snapshot).apply(batch)
        assert ledger.identity == retried.ledger_identity
        assert restarted.read_publication(c) == (rows, ledger)
        claims = ledger.claims(retried.received_at)
        assert (claims.complete_as_of, claims.deletions_as_of) == (at(19), at(19))
        request = next_extraction(restarted.load_ledger(c), "cdc", at(20))
        assert request.since == "log:103"
        next_batch = c.manifest(
            "cdc", request.until, since=request.since, generation="log:104", rows=0, tombstones=1
        )
        gone = Rows([{"id": 2, "value": None, "deleted": True}], {**columns, "deleted": "bool"})
        final = deliver(restarted, c, gone, next_batch, now=at(20))
        assert final.accepted
        ledger = Ledger(c).apply(snapshot).apply(batch).apply(next_batch)
        claims = ledger.claims(final.received_at)
        assert (claims.complete_as_of, claims.deletions_as_of) == (at(20), at(20))
        assert restarted.read(c.table) == [{"id": 3, "value": "new"}]
        assert ledger.identity == final.ledger_identity
        assert ledger == Ledger.from_receipts(c, restarted.receipts(c.table)) == restarted.load_ledger(c)
        assert len(restarted.receipts(c.table)) == 3
        assert restarted.connection.execute("SELECT count(*) FROM __dogana_commits").fetchone()[0] == 3


def event_contract():
    columns = {"event": "str", "arrived_at": "datetime"}
    return Contract(
        "events",
        (),
        {"append": Shape.append()},
        types={"arrived_at": "datetime"},
        schema_hash=schema_hash(columns),
        cursor_column="arrived_at",
        start_at=at(18),
    )


def test_an_event_stream_restart_replays_the_last_arrival_interval_and_refuses_a_gap(tmp_path):
    c = event_contract()
    path = tmp_path / "events.duckdb"
    rows = [{"event": "same", "arrived_at": at(19)}] * 2
    manifest = c.manifest("append", at(19), since=at(18), rows=2)
    with DuckDBStore(path) as store:
        first = deliver(store, c, rows, manifest, now=at(19))
        assert first.accepted
    with DuckDBStore(path) as restarted:
        replay = deliver(restarted, c, object(), manifest, now=at(20))
        assert replay == replace(first, outcome="replayed")
        assert replay.identity == first.identity and replay.received_at == at(19)
        gap = c.manifest("append", at(21), since=at(20), rows=1)
        refused = deliver(restarted, c, object(), gap, now=at(21))
        assert not refused.accepted
        assert refused.checks[-1].detail.endswith(
            "append_contiguous: append-only intervals must be contiguous without overlap"
        )
        assert restarted.read_publication(c) == (rows, fold(first))
        request = next_extraction(restarted.load_ledger(c), "append", at(20))
        assert request.since == at(19)
        next_rows = [{"event": "next", "arrived_at": at(20)}]
        next_batch = c.manifest("append", request.until, since=request.since, rows=1)
        final = deliver(restarted, c, next_rows, next_batch, now=at(21))
        assert final.accepted
        assert restarted.read(c.table) == next_rows + rows
        assert len([r for r in restarted.receipts(c.table) if r.accepted]) == 2
        assert restarted.connection.execute("SELECT count(*) FROM __dogana_commits").fetchone()[0] == 2


def test_two_event_stream_consumers_racing_on_the_same_interval_publish_once(tmp_path):
    c = event_contract()
    path = tmp_path / "race.duckdb"
    rows = [{"event": "same", "arrived_at": at(19)}] * 2
    manifest = c.manifest("append", at(19), since=at(18), rows=2)
    barrier = Barrier(2)
    with DuckDBStore(path) as first, DuckDBStore(path) as second:

        def consume(store):
            barrier.wait(timeout=5)
            return store.deliver(c, rows, manifest, now=at(19))

        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(consume, (first, second)))
        assert sorted(r.outcome.value for r in receipts) == ["accepted", "replayed"]
        assert receipts[0].identity == receipts[1].identity
        assert first.read_publication(c) == second.read_publication(c) == (rows, fold(receipts[0]))
        assert len(first.receipts(c.table)) == 1
        assert first.connection.execute("SELECT count(*) FROM __dogana_commits").fetchone()[0] == 1


def test_a_micro_batch_stream_commits_1000_tiny_batches_with_bounded_receipts(tmp_path):
    c = event_contract()
    with DuckDBStore(tmp_path / "cadence.duckdb") as store:
        started = perf_counter()
        for index in range(1, 1_001):
            boundary = at(18) + timedelta(milliseconds=index)
            manifest = c.manifest("append", boundary, since=boundary - timedelta(milliseconds=1), rows=1)
            receipt = deliver(store, c, [{"event": "tick", "arrived_at": boundary}], manifest, now=boundary)
            assert receipt.accepted, receipt.checks
        elapsed = perf_counter() - started
        assert elapsed < 120, elapsed  # a guard against slowdowns, sized for shared CI runners
        receipts = store.receipts(c.table)
        sizes = [len(r.to_json().encode("utf-8")) for r in receipts]
        assert len(receipts) == len(store.read(c.table)) == 1_000
        assert max(sizes) < 5_000
        assert max(sizes) - min(sizes) < 500
        ledger = Ledger.from_receipts(c, receipts)
        assert len(ledger.regions) == 1
        assert ledger.identity == receipt.ledger_identity == store.load_ledger(c).identity
        assert deliver(store, c, object(), manifest, now=boundary).replayed
        assert store.receipts(c.table) == receipts
        assert store.connection.execute("SELECT count(*) FROM __dogana_commits").fetchone()[0] == 1_000
