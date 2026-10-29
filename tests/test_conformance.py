import csv
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dogana.conformance import StampedLedgerConformance, StoreConformance, TogetherConformance
from tests.memory_store import CommitMemoryStore, MemoryStore, RowSource


class MemoryFactory:
    def __init__(self, store_type, monkeypatch):
        self.store_type, self.monkeypatch = store_type, monkeypatch

    def make_store(self):
        return self.store_type()

    def write_source(self, rows, columns):
        return RowSource(rows, columns)

    def content_hash(self, source):
        return None

    def read_table(self, store, table):
        return store.state[0][table][0]

    @contextmanager
    def fail_after_rows(self, store):
        def fail():
            raise OSError("interrupted after rows")

        with self.monkeypatch.context() as patch:
            patch.setattr(store, "_publication_point", fail)
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
            patch.setattr(store, "_publication_point", pause)
            yield written, resume

    def set_stamp(self, store, table, publication_id):
        store.state[0][table] = store.state[0][table][0], publication_id


class MinimalMemoryFactory(MemoryFactory):
    def make_store(self):
        storage = MemoryStore()
        operations = (
            "digest",
            "hold",
            "check",
            "publish",
            "abort",
            "load_ledger",
            "receipts",
            "accepted_receipt",
        )
        return SimpleNamespace(_storage=storage, **{name: getattr(storage, name) for name in operations})

    def read_table(self, store, table):
        return super().read_table(store._storage, table)

    def fail_after_rows(self, store):
        return super().fail_after_rows(store._storage)

    def pause_after_rows(self, store):
        return super().pause_after_rows(store._storage)

    def read_publication(self, store, contract):
        return super().read_publication(store._storage, contract)


class DuckDBFactory:
    def __init__(self, folder, monkeypatch):
        self.folder, self.monkeypatch, self.paths = folder, monkeypatch, {}

    def make_store(self):
        from example_store import DuckDBStore

        path = self.folder / f"{uuid4().hex}.duckdb"
        store = DuckDBStore(path)
        self.paths[id(store)] = path
        return store

    def reopen(self, store):
        from example_store import DuckDBStore

        return DuckDBStore(self.paths[id(store)])

    def write_source(self, rows, columns):
        from example_store._sources import _FileSource

        path = self.folder / f"{uuid4().hex}.csv"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(columns))
            writer.writeheader()
            writer.writerows(rows)
        return _FileSource(path, columns)

    def content_hash(self, source):
        from example_store._sources import _file_digest

        return _file_digest(source)

    def read_table(self, store, table):
        return store.read(table)

    @contextmanager
    def fail_after_rows(self, store):
        def fail():
            raise OSError("interrupted after rows")

        with self.monkeypatch.context() as patch:
            patch.setattr(store, "_publication_point", fail)
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

        def pause():
            written.set()
            assert resume.wait(5)

        with self.monkeypatch.context() as patch:
            patch.setattr(store, "_publication_point", pause)
            yield written, resume

    def set_stamp(self, store, table, publication_id):
        if publication_id is None:
            store.connection.execute("DELETE FROM __dogana_publications WHERE table_name=?", [table])
            return
        store.connection.execute(
            "UPDATE __dogana_publications SET publication_id=? WHERE table_name=?", [publication_id, table]
        )


class TestMinimalMemory(StoreConformance):
    @pytest.fixture
    def factory(self, monkeypatch):
        return MinimalMemoryFactory(MemoryStore, monkeypatch)


class TestAtomicMemory(StoreConformance, StampedLedgerConformance):
    @pytest.fixture
    def factory(self, monkeypatch):
        return MemoryFactory(MemoryStore, monkeypatch)


class TestCommitMemory(StoreConformance, StampedLedgerConformance):
    @pytest.fixture
    def factory(self, monkeypatch):
        return MemoryFactory(CommitMemoryStore, monkeypatch)


class TestDuckDB(StoreConformance, StampedLedgerConformance, TogetherConformance):
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
    first_manifest = Manifest(
        table=c.table, shape="full", produced_at=at(18), rows=1, schema_hash=c.schema_hash
    )
    first = deliver(store, c, [row(1)], first_manifest, at(20))

    def fail():
        raise OSError("interrupted before commit")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_publication_point", fail)
        manifest = Manifest(
            table=c.table, shape="full", produced_at=at(19), rows=1, schema_hash=c.schema_hash
        )
        with pytest.raises(StoreError, match="interrupted before commit"):
            deliver(store, c, [row(2)], manifest, at(20))
    assert store.load_ledger(c).identity == first.ledger_identity
    assert store.receipts(c.table) == (first,)
    assert store.read(c.table) == [row(1)]
    assert not store.candidates


@pytest.mark.parametrize(
    "scenario,message",
    [
        (
            StampedLedgerConformance.test_stamp_ahead_refuses_until_receipts_repair_the_lost_ledger,
            "store does not expose both stamped-ledger operations",
        ),
        (
            TogetherConformance.test_a_group_publishes_every_table_at_one_instant,
            "store does not publish tables together",
        ),
    ],
)
def test_optional_conformance_scenarios_skip_stores_without_the_required_operations(
    monkeypatch, scenario, message
):
    factory = MinimalMemoryFactory(MemoryStore, monkeypatch)
    suite = StampedLedgerConformance() if "stamp" in scenario.__name__ else TogetherConformance()
    with pytest.raises(pytest.skip.Exception, match=message):
        scenario(suite, factory, factory.make_store())


class ReopeningMemoryFactory(MinimalMemoryFactory):
    def reopen(self, store):
        return SimpleNamespace(**vars(store))


@pytest.mark.parametrize(
    "scenario",
    [
        StoreConformance.test_a_publication_survives_reopening_its_storage,
        StoreConformance.test_two_stores_on_one_storage_publish_only_one_candidate,
    ],
)
def test_durable_conformance_scenarios_accept_an_adapter_without_close(monkeypatch, scenario):
    factory = ReopeningMemoryFactory(MemoryStore, monkeypatch)
    store = factory.make_store()
    assert not hasattr(store, "close")
    scenario(StoreConformance(), factory, store)
