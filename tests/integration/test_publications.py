import pytest

pytest.importorskip("duckdb")

from dogana import InconsistentPublication, Ledger, Manifest
from dogana.reference.duckdb import DuckDBStore
from tests.integration.support import csv_file, row, sidecar
from tests.support import at, contract


class InterruptedWrite(BaseException):
    pass


class CommitBeforeLedger:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, *args):
        if sql.startswith("INSERT OR REPLACE INTO __dogana_ledgers"):
            self.connection.execute("COMMIT")
            raise InterruptedWrite
        return self.connection.execute(sql, *args)


def test_rows_and_stamp_ahead_of_the_ledger_refuse_until_receipts_restore_evidence(tmp_path):
    c = contract()
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "base.csv", [row(1)])
        manifest = sidecar(path, c, "full", 18, 1)
        connection = store.connection
        store.connection = CommitBeforeLedger(connection)
        with pytest.raises(InterruptedWrite):
            store.deliver(c, path, manifest, now=at(30))
        store.connection = connection
        store._transaction = None
        with pytest.raises(InconsistentPublication, match="stamp ahead of ledger"):
            store.load_ledger(c)
        with pytest.raises(InconsistentPublication):
            store.deliver(c, path, manifest, now=at(30))
        repaired = store.repair(c)
        receipt = store.deliver(c, path, manifest, now=at(30))
        assert receipt.accepted
        assert repaired == receipt.ledger == Ledger.from_receipts(c, store.receipts(c.table))
        assert store.load_ledger(c) == repaired
        assert store.read(c.table) == [row(1)]
        assert len(store.receipts()) == 1


def test_rows_replaced_behind_the_gate_refuse_until_a_new_complete_full_delivery(tmp_path):
    c = contract()
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "base.csv", [row(1)])
        first = sidecar(path, c, "full", 18, 1)
        receipt = store.deliver(c, path, first, now=at(30))
        path = csv_file(tmp_path, "next.csv", [row(2)])
        second = sidecar(path, c, "full", 19, 1)
        assert store.deliver(c, path, second, now=at(30)).accepted
        store.connection.execute('DELETE FROM "moves"')
        store.connection.execute('INSERT INTO "moves" VALUES (1, ?, ?)', [at(18).date(), "current"])
        store.connection.execute(
            "UPDATE __dogana_publications SET publication_id=? WHERE table_name=?",
            [receipt.publication_id, c.table],
        )
        with pytest.raises(InconsistentPublication, match="ledger ahead of stamp"):
            store.load_ledger(c)
        with pytest.raises(InconsistentPublication, match="complete delivery"):
            store.repair(c)
        with pytest.raises(InconsistentPublication):
            store.deliver(c, path, second, now=at(30))
        partial = Manifest(
            c.table,
            "daily",
            at(20),
            window=(at(18).date(), at(19).date()),
            rows=1,
            schema_hash=c.schema_hash,
        )
        with pytest.raises(InconsistentPublication):
            store.deliver(c, path, partial, now=at(30))
        recovered = store.deliver(c, path, sidecar(path, c, "full", 20, 1), now=at(30))
        assert recovered.accepted
        assert store.load_ledger(c) == Ledger.from_receipts(c, store.receipts(c.table))
        assert store.read(c.table) == [row(2)]


def test_repair_cannot_rewind_a_ledger_that_is_ahead_of_the_last_retained_receipt(tmp_path):
    c = contract()
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "base.csv", [row(1)])
        first = store.deliver(c, path, sidecar(path, c, "full", 18, 1), now=at(30))
        ahead = first.ledger.apply(Manifest(c.table, "full", at(19)))
        store.connection.execute(
            "UPDATE __dogana_ledgers SET document=? WHERE table_name=?", [ahead.to_json(), c.table]
        )
        with pytest.raises(InconsistentPublication, match="ledger ahead"):
            store.repair(c)
        with pytest.raises(InconsistentPublication, match="ledger ahead"):
            store.load_ledger(c)


def test_a_lost_ledger_cannot_hide_a_case_alias_of_stamped_rows(tmp_path):
    from dogana import InvalidContract

    c = contract("Moves")
    with DuckDBStore(":memory:") as store:
        path = csv_file(tmp_path, "base.csv", [row(1)])
        assert store.deliver(c, path, sidecar(path, c, "full", 18, 1), now=at(30)).accepted
        store.connection.execute("DELETE FROM __dogana_ledgers")
        with pytest.raises(InvalidContract, match="letter case"):
            store.load_ledger(contract("moves"))
        assert store.repair(c).publication_id is not None


def test_retained_receipts_prevent_missing_ledger_and_stamp_from_looking_like_a_new_table(tmp_path):
    c = contract()
    with DuckDBStore(":memory:") as store:
        path = csv_file(tmp_path, "base.csv", [row(1)])
        first = store.deliver(c, path, sidecar(path, c, "full", 18, 1), now=at(30))
        store.connection.execute("DELETE FROM __dogana_ledgers")
        store.connection.execute("DELETE FROM __dogana_publications")
        store.connection.execute('DELETE FROM "moves"')
        with pytest.raises(InconsistentPublication, match="missing"):
            store.load_ledger(c)
        with pytest.raises(InconsistentPublication):
            store.deliver(c, object(), first.manifest, now=at(30))
        with pytest.raises(InconsistentPublication, match="complete delivery"):
            store.repair(c)
        path = csv_file(tmp_path, "recovered.csv", [row(2)])
        recovered = store.deliver(c, path, sidecar(path, c, "full", 19, 1), now=at(30))
        assert recovered.accepted
        assert store.load_ledger(c) == Ledger.from_receipts(c, store.receipts(c.table))
        assert sum(r.accepted for r in store.receipts(c.table)) == 2
        assert store.read(c.table) == [row(2)]
