import pytest

pytest.importorskip("duckdb")

from dogana import Ledger
from dogana.reference.duckdb import DuckDBStore
from tests.integration.support import csv_file, row, sidecar
from tests.support import contract


class StatementFault:
    def __init__(self, connection, fail_at=None, after=False):
        self.connection, self.fail_at, self.after = connection, fail_at, after
        self.statements = []
        self.fired = False

    def execute(self, sql, *args):
        index = len(self.statements)
        self.statements.append(sql)
        fail = index == self.fail_at and not self.fired
        if fail and not self.after:
            self.fired = True
            raise OSError(f"interrupted before statement {index}")
        result = self.connection.execute(sql, *args)
        if fail:
            self.fired = True
            raise OSError(f"interrupted after statement {index}")
        return result

    def close(self):
        self.connection.close()


def test_every_duckdb_statement_boundary_is_atomic_and_resubmission_converges(tmp_path):
    c = contract()
    base = csv_file(tmp_path, "base.csv", [row(1), row(2)])
    incoming = csv_file(tmp_path, "feed.csv", [row(2, value="changed")])
    first_manifest = sidecar(base, c, "full", 18, 2, generation="g1")
    manifest = sidecar(
        incoming, c, "feed", 19, 1, since="g1", generation="g2", keys=((2,),), tombstones=((1,),)
    )
    with DuckDBStore(":memory:") as store:
        assert store.deliver(c, base, first_manifest).accepted
        trace = StatementFault(store.connection)
        store.connection = trace
        assert store.deliver(c, incoming, manifest).accepted
        statements = trace.statements
    assert any(sql == "COMMIT" for sql in statements)
    assert any("__dogana_publications VALUES" in sql for sql in statements)
    for after in (False, True):
        for index, sql in enumerate(statements):
            with DuckDBStore(":memory:") as store:
                first = store.deliver(c, base, first_manifest)
                connection = store.connection
                fault = StatementFault(connection, index, after)
                store.connection = fault
                try:
                    result = store.deliver(c, incoming, manifest)
                except OSError:
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
                retry = store.deliver(c, incoming, manifest)
                assert retry.accepted, (index, sql, retry.checks)
                assert store.read(c.table) == [row(2, value="changed")]
                assert Ledger.from_receipts(c, store.receipts(c.table)) == store.load_ledger(c)
                assert sum(r.accepted and r.manifest == manifest for r in store.receipts(c.table)) == 1
                assert not store._candidates and store._transaction is None
                assert not connection.execute(
                    "SELECT count(*) FROM duckdb_tables() WHERE table_name LIKE '__dogana_candidate_%'"
                ).fetchone()[0], (index, sql)
