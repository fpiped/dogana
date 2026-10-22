import csv
from contextlib import contextmanager
from uuid import uuid4

import pytest

from dogana.conformance import StoreConformance
from tests.memory_store import MemoryStore, OrderedMemoryStore


class MemoryFactory:
    def __init__(self, store_type, monkeypatch):
        self.store_type, self.monkeypatch = store_type, monkeypatch
        self.atomic_append = store_type.atomic_append

    def make_store(self):
        return self.store_type()

    def write_source(self, rows, schema):
        return [dict(r) for r in rows]

    def read_table(self, store, table):
        return store.state[0][table][0]

    @contextmanager
    def fail_after_rows(self, store):
        def fail():
            raise OSError("interrupted after rows")

        with self.monkeypatch.context() as patch:
            patch.setattr(store, "_after_rows", fail)
            yield

    def set_ledger(self, store, ledger):
        store.state[1][ledger.contract.table] = ledger

    def set_stamp(self, store, table, publication_id):
        store.state[0][table] = store.state[0][table][0], publication_id


class DuckDBFactory:
    atomic_append = True

    def __init__(self, folder, monkeypatch):
        self.folder, self.monkeypatch = folder, monkeypatch

    def make_store(self):
        from dogana.reference.duckdb import DuckDBStore

        return DuckDBStore(self.folder / f"{uuid4().hex}.duckdb")

    def write_source(self, rows, schema):
        path = self.folder / f"{uuid4().hex}.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=[c.name for c in schema])
            writer.writeheader()
            writer.writerows(rows)
        return path

    def read_table(self, store, table):
        return store.read(table)

    @contextmanager
    def fail_after_rows(self, store):
        original = store.execute

        def fail(candidate, plan):
            original(candidate, plan)
            raise OSError("interrupted after rows")

        with self.monkeypatch.context() as patch:
            patch.setattr(store, "execute", fail)
            yield

    def set_ledger(self, store, ledger):
        store.connection.execute(
            "INSERT OR REPLACE INTO __dogana_ledgers VALUES (?, ?)",
            [ledger.contract.table, ledger.to_json()],
        )

    def set_stamp(self, store, table, publication_id):
        if publication_id is None:
            store.connection.execute("DELETE FROM __dogana_publications WHERE table_name=?", [table])
            return
        store.connection.execute(
            "UPDATE __dogana_publications SET publication_id=? WHERE table_name=?", [publication_id, table]
        )


class TestAtomicMemory(StoreConformance):
    @pytest.fixture
    def factory(self, monkeypatch):
        return MemoryFactory(MemoryStore, monkeypatch)


class TestOrderedMemory(StoreConformance):
    @pytest.fixture
    def factory(self, monkeypatch):
        return MemoryFactory(OrderedMemoryStore, monkeypatch)


class TestDuckDB(StoreConformance):
    @pytest.fixture
    def factory(self, tmp_path, monkeypatch):
        pytest.importorskip("duckdb")
        return DuckDBFactory(tmp_path, monkeypatch)


def test_an_ordered_receipt_written_before_a_crash_repairs_the_ledger_before_replay(monkeypatch):
    from dogana import InconsistentPublication, Ledger
    from tests.integration.support import row
    from tests.support import at, contract

    c = contract()
    store = OrderedMemoryStore()
    factory = MemoryFactory(OrderedMemoryStore, monkeypatch)
    suite = StoreConformance()
    first = suite._deliver(factory, store, c, [row(1)])

    def fail():
        raise OSError("interrupted after receipt")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_after_receipt", fail)
        from dogana import Manifest

        manifest = Manifest(c.table, "full", at(19), rows=1, schema_hash=c.schema_hash)
        failed = store.deliver(c, [row(2)], manifest)
    assert not failed.accepted
    with pytest.raises(InconsistentPublication, match="stamp ahead"):
        store.load_ledger(c)
    replay = store.deliver(c, object(), manifest)
    assert replay.accepted
    assert replay.ledger != first.ledger
    assert store.load_ledger(c) == Ledger.from_receipts(c, store.receipts(c.table))
    assert factory.read_table(store, c.table) == [row(2)]
    assert not store.pending
