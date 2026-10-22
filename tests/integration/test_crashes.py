import pytest

from tests.support import at

pytest.importorskip("duckdb")

from dogana import Column, Contract, Ledger, Manifest, Shape
from dogana.errors import StoreError
from dogana.reference.duckdb import DuckDBStore
from tests.integration.support import csv_file, row, sidecar
from tests.support import contract


class StatementFault:
    def __init__(self, connection, fail_at=None, after=False, error_type=OSError):
        self.connection, self.fail_at, self.after = connection, fail_at, after
        self.statements = []
        self.fired = False
        self.error_type = error_type

    def execute(self, sql, *args):
        index = len(self.statements)
        self.statements.append(sql)
        fail = index == self.fail_at and not self.fired
        if fail and not self.after:
            self.fired = True
            raise self.error_type(f"interrupted before statement {index}")
        result = self.connection.execute(sql, *args)
        if fail:
            self.fired = True
            raise self.error_type(f"interrupted after statement {index}")
        return result

    def close(self):
        self.connection.close()


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, KeyboardInterrupt])
def test_every_duckdb_statement_boundary_is_atomic_and_resubmission_converges(tmp_path, error_type):
    c = contract()
    base = csv_file(tmp_path, "base.csv", [row(1), row(2)])
    incoming = csv_file(tmp_path, "feed.csv", [row(2, value="changed")])
    first_manifest = sidecar(base, c, "full", 18, 2, generation="g1")
    manifest = sidecar(
        incoming, c, "feed", 19, 1, since="g1", generation="g2", keys=((2,),), tombstones=((1,),)
    )
    with DuckDBStore(":memory:") as store:
        assert store.deliver(c, base, first_manifest, now=at(30)).accepted
        trace = StatementFault(store.connection)
        store.connection = trace
        assert store.deliver(c, incoming, manifest, now=at(30)).accepted
        statements = trace.statements
    assert any(sql == "COMMIT" for sql in statements)
    assert any("__dogana_publications VALUES" in sql for sql in statements)
    for after in (False, True):
        for index, sql in enumerate(statements):
            with DuckDBStore(":memory:") as store:
                first = store.deliver(c, base, first_manifest, now=at(30))
                connection = store.connection
                fault = StatementFault(connection, index, after, error_type)
                store.connection = fault
                try:
                    result = store.deliver(c, incoming, manifest, now=at(30))
                except (StoreError, KeyboardInterrupt):
                    result = None
                finally:
                    store.connection = connection
                assert fault.fired, (index, sql)
                receipts = store.receipts(c.table)
                committed = next((r for r in receipts if r.accepted and r.manifest == manifest), None)
                current = store.load_ledger(c)
                assert current == (committed.ledger if committed else first.ledger), (index, sql)
                assert store.read(c.table) == ([row(2, value="changed")] if committed else [row(1), row(2)])
                if result is not None and committed:
                    assert result == committed, (index, sql)
                retry = store.deliver(c, incoming, manifest, now=at(30))
                assert retry.accepted, (index, sql, retry.checks)
                assert store.read(c.table) == [row(2, value="changed")]
                assert Ledger.from_receipts(c, store.receipts(c.table)) == store.load_ledger(c)
                assert sum(r.accepted and r.manifest == manifest for r in store.receipts(c.table)) == 1
                assert not store._candidates and store._transaction is None
                assert not connection.execute(
                    "SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE '__dogana_candidate_%'"
                ).fetchone()[0], (index, sql)


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_reopening_after_an_interrupted_append_retains_one_publication_and_retry_preserves_duplicates(
    tmp_path, monkeypatch, error_type
):
    c = Contract("events", (), {"log": Shape.append()}, schema=(Column("event", "str"),), start_at=at(18))
    path = tmp_path / "landing.duckdb"
    first_manifest = Manifest(c.table, "log", at(19), since=at(18), rows=1, schema_hash=c.schema_hash)
    manifest = Manifest(c.table, "log", at(20), since=at(19), rows=2, schema_hash=c.schema_hash)
    rows = [{"event": "same"}]
    with DuckDBStore(path) as store:
        first = store.deliver(c, rows, first_manifest, now=at(30))
        assert first.accepted

        def fail():
            raise error_type("publication interrupted")

        with monkeypatch.context() as patch:
            patch.setattr(store, "_publication_point", fail)
            with pytest.raises(KeyboardInterrupt if error_type is KeyboardInterrupt else StoreError):
                store.deliver(c, rows * 2, manifest, now=at(30))
    with DuckDBStore(path) as reopened:
        assert reopened.read_publication(c) == (rows, first.ledger)
        assert reopened.publication_stamp(c.table) == first.publication_id
        assert reopened.receipts(c.table) == (first,)
        assert reopened.accepted_receipt(manifest.identity) is None
        retried = reopened.deliver(c, rows * 2, manifest, now=at(30))
        assert retried.accepted
        assert reopened.read_publication(c) == (rows * 3, retried.ledger)
        assert reopened.read(c.table, first.publication_id) == rows
        replayed = reopened.deliver(c, object(), manifest, now=at(30))
        assert replayed.replayed and replayed.identity == retried.identity
        assert reopened.receipts(c.table) == (first, retried)
        assert reopened.read(c.table) == rows * 3
