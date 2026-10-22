from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from time import sleep

import pytest

pytest.importorskip("duckdb")

from dogana import Ledger
from dogana.reference.duckdb import DuckDBStore
from tests.integration.support import csv_file, row, sidecar
from tests.support import contract


@pytest.mark.parametrize("connections", [1, 2])
def test_threads_delivering_to_one_file_serialize_checks_and_publish_whole_states(
    tmp_path, monkeypatch, connections
):
    c = contract()
    stores = [DuckDBStore(tmp_path / "landing.duckdb") for _ in range(connections)]
    mutex = Lock()
    active = peak = 0
    try:
        base = csv_file(tmp_path, "base.csv", [row(1)])
        assert stores[0].deliver(c, base, sidecar(base, c, "full", 18, 1)).accepted
        for store in stores:
            original = store.check

            def checked(*args, original=original):
                nonlocal active, peak
                with mutex:
                    active += 1
                    peak = max(peak, active)
                try:
                    sleep(0.005)
                    return original(*args)
                finally:
                    with mutex:
                        active -= 1

            monkeypatch.setattr(store, "check", checked)
        for day in (19, 21, 23):
            barrier = Barrier(2)
            paths = [csv_file(tmp_path, f"{day}-{i}.csv", [row(i + 2)]) for i in range(2)]
            manifests = [sidecar(path, c, "full", day + i, 1) for i, path in enumerate(paths)]

            def deliver(i, barrier=barrier, paths=paths, manifests=manifests):
                barrier.wait(timeout=5)
                return stores[i % connections].deliver(c, paths[i], manifests[i])

            with ThreadPoolExecutor(max_workers=2) as pool:
                receipts = list(pool.map(deliver, range(2)))
            assert all(isinstance(r.accepted, bool) for r in receipts)
            assert any(r.accepted for r in receipts)
            assert Ledger.from_receipts(c, stores[0].receipts(c.table)) == stores[0].load_ledger(c)
            assert stores[0].read(c.table) in ([row(2)], [row(3)])
            for store in stores:
                assert store.load_ledger(c) == stores[0].load_ledger(c)
                assert not store._candidates and store._transaction is None
        assert peak == 1
    finally:
        for store in stores:
            store.close()
