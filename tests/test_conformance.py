import csv
from contextlib import contextmanager
from threading import Event
from uuid import uuid4

import pytest

from dogana.conformance import StoreConformance
from tests.memory_store import CommitMemoryStore, MemoryStore


class MemoryFactory:
    def __init__(self, store_type, monkeypatch):
        self.store_type, self.monkeypatch = store_type, monkeypatch

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

    def remove_ledger(self, store, table):
        store.state[1].pop(table, None)

    def read_publication(self, store, contract):
        with store.lock:
            return store.read(contract.table), store.load_ledger(contract)

    @contextmanager
    def pause_after_rows(self, store):
        written, resume = Event(), Event()

        def pause():
            written.set()
            assert resume.wait(5)

        with self.monkeypatch.context() as patch:
            patch.setattr(store, "_after_rows", pause)
            yield written, resume

    def set_stamp(self, store, table, publication_id):
        store.state[0][table] = store.state[0][table][0], publication_id


class DuckDBFactory:
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

    def remove_ledger(self, store, table):
        store.connection.execute("DELETE FROM __dogana_ledgers WHERE table_name=?", [table])

    def read_publication(self, store, contract):
        return store.read_publication(contract)

    @contextmanager
    def pause_after_rows(self, store):
        written, resume = Event(), Event()
        original = store.execute

        def pause(candidate, plan):
            original(candidate, plan)
            written.set()
            assert resume.wait(5)

        with self.monkeypatch.context() as patch:
            patch.setattr(store, "execute", pause)
            yield written, resume

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


class TestCommitMemory(StoreConformance):
    @pytest.fixture
    def factory(self, monkeypatch):
        return MemoryFactory(CommitMemoryStore, monkeypatch)


class TestDuckDB(StoreConformance):
    @pytest.fixture
    def factory(self, tmp_path, monkeypatch):
        pytest.importorskip("duckdb")
        return DuckDBFactory(tmp_path, monkeypatch)


def test_a_commit_failure_before_publication_leaves_no_receipt_or_rows(monkeypatch):
    from dogana import Manifest, StoreError, deliver
    from tests.integration.support import row
    from tests.support import at, contract

    c = contract()
    store = CommitMemoryStore()
    first_manifest = Manifest(c.table, "full", at(18), rows=1, schema_hash=c.schema_hash)
    first = deliver(store, c, [row(1)], first_manifest, at(20))

    def fail():
        raise OSError("interrupted before commit")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_after_rows", fail)
        manifest = Manifest(c.table, "full", at(19), rows=1, schema_hash=c.schema_hash)
        with pytest.raises(StoreError, match="interrupted before commit"):
            deliver(store, c, [row(2)], manifest, at(20))
    assert store.load_ledger(c) == first.ledger
    assert store.receipts(c.table) == (first,)
    assert store.read(c.table) == [row(1)]
    assert not store.candidates
