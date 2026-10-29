import json
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest

from dogana import (
    Deletions,
    DoganaError,
    InconsistentPublication,
    InvalidContract,
    Manifest,
    PlanningError,
    RejectedDelivery,
    SerializationError,
    StampAhead,
    State,
    StoreError,
    deliver,
)
from tests.integration.support import marked, row
from tests.memory_store import MemoryStore, RowSource
from tests.support import COLUMNS, FULL, SINCE, at, contract


def seed(store):
    c = contract()
    manifest = Manifest(c.table, "full", at(18), rows=1, schema_hash=c.schema_hash)
    return c, deliver(store, c, [row(1)], manifest, at(20))


@pytest.mark.protocol("store.2", "checks.errors")
@pytest.mark.parametrize("error", [InvalidContract("bad source"), RejectedDelivery("bad source")])
def test_a_protocol_refusal_persists_one_attempt_without_a_publication(monkeypatch, error):
    store = MemoryStore()
    c, first = seed(store)

    def refuse(*args):
        raise error

    monkeypatch.setattr(store, "hold", refuse)
    receipt = deliver(store, c, [row(2)], replace(first.manifest, produced_at=at(19)), at(20))
    assert not receipt.accepted
    assert receipt.received_at == at(20)
    assert receipt.publication_id == first.publication_id
    assert store.receipts(c.table) == (first, receipt)
    assert store.read(c.table) == [row(1)]


def test_abort_failure_after_an_uncommitted_delivery_is_the_cleanup_error_cause(monkeypatch):
    store = MemoryStore()
    c, first = seed(store)
    cleanup = StoreError("abort failed")

    def fail(*args):
        raise InvalidContract("bad rows")

    def abort(candidate):
        raise cleanup

    monkeypatch.setattr(store, "check", fail)
    monkeypatch.setattr(store, "abort", abort)
    with pytest.raises(StoreError, match="candidate cleanup failed") as error:
        deliver(store, c, [row(2)], replace(first.manifest, produced_at=at(19)), at(20))
    assert error.value.__cause__ is cleanup
    assert store.receipts() == (first,)


@pytest.mark.protocol("store.4", "checks.errors")
@pytest.mark.parametrize("operation", ["hold", "check", "_publication_point", "publish"])
@pytest.mark.parametrize("error", [OSError("disk"), ValueError("bug"), TypeError("bug"), StoreError("store")])
def test_an_operational_failure_aborts_without_recording_a_refused_delivery(monkeypatch, operation, error):
    store = MemoryStore()
    c, first = seed(store)

    def fail(*args):
        raise error

    monkeypatch.setattr(store, operation, fail)
    with pytest.raises(StoreError):
        deliver(store, c, [row(2)], replace(first.manifest, produced_at=at(19)), at(20))
    assert store.receipts(c.table) == (first,)
    assert store.load_ledger(c).identity == first.ledger_identity
    assert store.read(c.table) == [row(1)]
    assert not store.candidates


@pytest.mark.protocol("store.4", "checks.errors")
def test_a_lost_commit_acknowledgement_returns_the_persisted_acceptance(monkeypatch):
    store = MemoryStore()
    c, first = seed(store)
    publish = store.publish

    def lose_acknowledgement(candidate, receipt):
        publish(candidate, receipt)
        raise OSError("acknowledgement lost")

    monkeypatch.setattr(store, "publish", lose_acknowledgement)
    receipt = deliver(store, c, [row(2)], replace(first.manifest, produced_at=at(19)), at(20))
    assert receipt.accepted and not receipt.replayed
    assert store.receipts(c.table) == (first, receipt)
    assert store.read(c.table) == [row(2)]
    assert not store.candidates


@pytest.mark.protocol("store.1", "store.obligations.5")
def test_a_replayed_delivery_retains_its_receipt_and_original_receiver_clock():
    store = MemoryStore()
    c, first = seed(store)
    replay = deliver(store, c, object(), first.manifest, at(25))
    assert replay == replace(first, outcome="replayed")
    assert replay.identity == first.identity
    assert replay.received_at == first.received_at
    assert store.receipts(c.table) == (first,)


@pytest.mark.protocol("contract.column-hash")
def test_a_full_publication_without_a_schema_hash_checks_the_bound_columns():
    store = MemoryStore()
    c = replace(contract(), schema_hash=None)
    manifest = c.manifest("full", at(18), rows=1)
    receipt = deliver(store, c, [row(1)], manifest, at(20))
    assert receipt.accepted
    assert "schema_hash" not in {check.name for check in receipt.checks}
    assert store.read(c.table) == [row(1)]


@pytest.mark.protocol("checks.window_present")
def test_a_full_delivery_with_a_null_window_value_is_refused_without_changing_the_publication():
    store = MemoryStore()
    c, first = seed(store)
    ledger = store.load_ledger(c)
    source = RowSource([{**row(2), "day": None}], COLUMNS)
    refused = deliver(store, c, source, c.manifest("full", at(19), rows=1), at(20))

    assert not refused.accepted
    assert [check.name for check in refused.checks if not check.passed] == ["window_present"]
    assert refused.publication_id == store.publication_stamp(c.table) == first.publication_id
    assert store.load_ledger(c) == ledger
    assert store.read(c.table) == [row(1)]
    assert store.receipts(c.table) == (first, refused)


@pytest.mark.protocol("contract.deleted-content")
def test_equal_tombstone_manifests_replay_without_reading_changed_keys_unless_content_is_hashed(tmp_path):
    class FileMemoryStore(MemoryStore):
        def __init__(self):
            super().__init__()
            self.digests = {}
            self.reads = []

        def digest(self, source):
            self.reads.append(source)
            return sha256(source.read_bytes()).hexdigest()

        def hold(self, source, manifest, c):
            if not hasattr(source, "read_bytes"):
                return super().hold(source, manifest, c)
            self.reads.append(source)
            content = source.read_bytes()
            candidate = super().hold(json.loads(content), manifest, c)
            self.digests[candidate] = sha256(content).hexdigest()
            return candidate

        def check(self, candidate, c, manifest, requirements):
            checks = tuple(
                replace(check, passed=self.digests[candidate] == manifest.content_hash)
                if check.name == "content_hash"
                else check
                for check in super().check(candidate, c, manifest, requirements)
            )
            base, stamp, _ = self.checked[candidate]
            self.checked[candidate] = base, stamp, checks
            return checks

    c = contract(
        columns=None, shapes={"full": FULL, "changes": replace(SINCE, deletions=Deletions.TOMBSTONE)}
    )
    files = [tmp_path / "delete-one.json", tmp_path / "delete-two.json"]
    for key, path in enumerate(files, 1):
        path.write_text(json.dumps(marked([], tombstones=(key,))))
    manifest = c.manifest("changes", at(19), since=at(18), rows=0, tombstones=1)

    for hashed in (False, True):
        store = FileMemoryStore()
        initial = deliver(store, c, [{"id": 1}, {"id": 2}], c.manifest("full", at(18), rows=2), at(20))
        assert initial.accepted
        manifests = [
            replace(manifest, content_hash=sha256(path.read_bytes()).hexdigest()) if hashed else manifest
            for path in files
        ]
        first = deliver(store, c, files[0], manifests[0], at(20))
        assert first.accepted and not first.replayed
        assert store.read(c.table) == [{"id": 2}]
        store.reads.clear()

        second = deliver(store, c, files[1], manifests[1], at(20))
        if hashed:
            assert manifests[0].identity != manifests[1].identity
            assert not second.accepted and not second.replayed
            assert any(check.name == "transition" and not check.passed for check in second.checks)
            later = deliver(store, c, files[1], replace(manifests[1], produced_at=at(20)), at(20))
            assert later.accepted and not later.replayed
            assert store.reads == [files[1]]
            assert store.read(c.table) == []
        else:
            assert manifests[0] == manifests[1]
            assert second == replace(first, outcome="replayed")
            assert store.reads == []
            assert store.read(c.table) == [{"id": 2}]
            assert store.load_ledger(c).identity == first.ledger_identity
            assert store.receipts(c.table) == (initial, first)


@pytest.mark.protocol("contract.soft-delete-fallback")
def test_changes_without_deletions_keep_a_physically_removed_soft_deleted_row_until_a_full_read():
    store = MemoryStore()
    c = contract(columns=None, shapes={"full": FULL, "since": SINCE})
    source = [{"id": 1, "inactive": False}, {"id": 2, "inactive": False}]
    first = deliver(store, c, source, c.manifest("full", at(18), rows=2), at(18))
    assert first.accepted

    source[0] = {"id": 1, "inactive": True}
    flagged = deliver(store, c, [source[0]], c.manifest("since", at(19), since=at(18), rows=1), at(19))
    assert flagged.accepted
    assert store.read(c.table) == [source[1], source[0]]
    claims = store.load_ledger(c).claims(at(19), limit=timedelta.max)
    assert (claims.state, claims.complete_as_of, claims.deletions_as_of) == (
        State.PATCHED,
        at(19),
        at(18),
    )

    removed = source.pop(0)
    changes = deliver(store, c, [], c.manifest("since", at(20), since=at(19), rows=0), at(20))
    assert changes.accepted
    assert store.read(c.table) == [source[0], removed]
    claims = store.load_ledger(c).claims(at(20), limit=timedelta.max)
    assert (claims.state, claims.complete_as_of, claims.deletions_as_of) == (
        State.PATCHED,
        at(20),
        at(18),
    )

    complete = deliver(store, c, source, c.manifest("full", at(21), rows=1), at(21))
    assert complete.accepted
    assert store.read(c.table) == source
    claims = store.load_ledger(c).claims(at(21), limit=timedelta.max)
    assert (claims.state, claims.exact_as_of) == (State.COMPLETE, at(21))


@pytest.mark.protocol("trust-model.1")
def test_a_truncated_full_export_passes_only_when_its_declared_count_also_omits_the_missing_rows():
    source = [row(2), row(3)]
    exported = source[:1]
    for declared, accepted in ((len(exported), True), (len(source), False)):
        store = MemoryStore()
        c, first = seed(store)
        ledger = store.load_ledger(c)
        manifest = c.manifest("full", at(19), rows=declared)
        receipt = deliver(store, c, exported, manifest, at(20))

        assert receipt.accepted is accepted
        assert next(check.passed for check in receipt.checks if check.name == "row_count") is accepted
        if accepted:
            assert store.read(c.table) == exported
            assert source[1] not in store.read(c.table)
            assert store.load_ledger(c).claims(at(19), limit=timedelta.max).exact_as_of == at(19)
        else:
            assert [check.name for check in receipt.checks if not check.passed] == ["row_count"]
            assert store.read(c.table) == [row(1)]
            assert store.load_ledger(c) == ledger
            assert store.publication_stamp(c.table) == receipt.publication_id == first.publication_id
        assert store.receipts(c.table) == (first, receipt)


@pytest.mark.protocol("store.chain-and-recovery")
def test_a_lost_accepted_receipt_cannot_be_replaced_by_another_acceptance():
    store = MemoryStore()
    c, first = seed(store)
    store.state[2].clear()
    with pytest.raises(StampAhead):
        deliver(store, c, [row(2)], first.manifest, at(20))
    assert not store.receipts(c.table)
    assert store.read(c.table) == [row(1)]


@pytest.mark.protocol("store.4", "checks.errors")
def test_an_interrupted_delivery_aborts_before_propagating(monkeypatch):
    store = MemoryStore()
    c, first = seed(store)

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "_publication_point", interrupt)
    with pytest.raises(KeyboardInterrupt):
        deliver(store, c, [row(2)], replace(first.manifest, produced_at=at(19)), at(20))
    assert not store.candidates
    assert store.receipts(c.table) == (first,)


@pytest.mark.protocol("checks.errors")
def test_delivery_failures_share_one_error_root_and_serialization_is_a_declaration_error():
    for error in (InvalidContract, RejectedDelivery, PlanningError, StoreError):
        assert issubclass(error, DoganaError)
    assert issubclass(SerializationError, InvalidContract)
    assert issubclass(InconsistentPublication, StoreError)


@pytest.fixture
def grouped_delivery():
    from example_store import DuckDBStore

    from dogana import Group

    contracts = (contract("one"), contract("two"))
    group = Group(contracts)
    deliveries = {c.table: ([row(1)], c.manifest("full", at(18), rows=1)) for c in contracts}
    with DuckDBStore(":memory:") as store:
        yield store, group, deliveries


@pytest.mark.parametrize("failure_mode", ["partial", "committed", "race_cleanup"])
def test_failed_group_publication_recovers_only_a_complete_commit_with_successful_cleanup(
    grouped_delivery, monkeypatch, failure_mode
):
    from dogana import deliver_together

    store, group, deliveries = grouped_delivery
    publish = store.publish_together
    abort = store.abort
    load = store.load_ledger
    loaded = []
    attempted = {}
    partial_reads = []
    cleanup = StoreError("abort failed")

    def fail(group, entries):
        attempted.update({receipt.contract.table: receipt for _, receipt in entries})
        if failure_mode == "partial":
            receipt = entries[0][1]

            def partial(identity):
                partial_reads.append(identity)
                return receipt if identity == receipt.manifest.identity else None

            monkeypatch.setattr(store, "accepted_receipt", partial)
        else:
            if failure_mode == "race_cleanup":
                entries = tuple(
                    (candidate, replace(receipt, source="another writer")) for candidate, receipt in entries
                )
            publish(group, entries)
        raise StoreError("lost acknowledgement")

    def read(c):
        loaded.append(c.table)
        return load(c)

    def cleanup_failure(candidate):
        abort(candidate)
        raise cleanup

    monkeypatch.setattr(store, "publish_together", fail)
    monkeypatch.setattr(store, "load_ledger", read)
    if failure_mode == "race_cleanup":
        monkeypatch.setattr(store, "abort", cleanup_failure)
    if failure_mode == "committed":
        result = deliver_together(store, group, deliveries, at(20))
        assert result == attempted
        assert loaded.count("one") >= 2 and loaded.count("two") >= 2
        for c in group.contracts:
            assert store.load_ledger(c).identity == result[c.table].ledger_identity
    else:
        error_type = InconsistentPublication if failure_mode == "partial" else StoreError
        message = (
            "committed only some of its tables" if failure_mode == "partial" else "candidate cleanup failed"
        )
        with pytest.raises(error_type, match=message) as error:
            deliver_together(store, group, deliveries, at(20))
        if failure_mode == "race_cleanup":
            assert error.value.__cause__ is cleanup
        else:
            assert partial_reads == [deliveries[t][1].identity for t in group.tables] * 2
    assert not store._candidates


def test_group_cleanup_attempts_every_candidate_and_reports_the_last_failure(grouped_delivery, monkeypatch):
    from dogana import deliver_together

    store, group, deliveries = grouped_delivery
    attempted = []
    failures = [StoreError("first abort"), StoreError("last abort")]
    abort = store.abort

    def fail(*args):
        raise StoreError("publication failed")

    def abort_failure(candidate):
        abort(candidate)
        attempted.append(candidate)
        raise failures[len(attempted) - 1]

    monkeypatch.setattr(store, "publish_together", fail)
    monkeypatch.setattr(store, "abort", abort_failure)
    with pytest.raises(StoreError, match="candidate cleanup failed") as error:
        deliver_together(store, group, deliveries, at(20))
    assert len(attempted) == 2
    assert error.value.__cause__ is failures[-1]
    assert not store._candidates


@pytest.mark.parametrize("abort_fails", [False, True])
def test_a_group_preflight_refusal_does_not_hold_that_source_and_refuses_every_member(
    grouped_delivery, monkeypatch, abort_fails
):
    from dogana import deliver_together

    store, group, deliveries = grouped_delivery
    source, manifest = deliveries["two"]
    deliveries["two"] = (source, replace(manifest, produced_at=at(21)))
    held = []
    hold, abort = store.hold, store.abort
    cleanup = StoreError("abort failed")

    def record(source, manifest, c):
        held.append(c.table)
        return hold(source, manifest, c)

    def fail(candidate):
        abort(candidate)
        raise cleanup

    monkeypatch.setattr(store, "hold", record)
    if abort_fails:
        monkeypatch.setattr(store, "abort", fail)
        with pytest.raises(StoreError, match="candidate cleanup failed") as error:
            deliver_together(store, group, deliveries, at(20))
        assert error.value.__cause__ is cleanup
    else:
        receipts = deliver_together(store, group, deliveries, at(20))
        assert all(not receipt.accepted for receipt in receipts.values())
    assert held == ["one"]
    assert len(store.receipts()) == 2
    assert not store._candidates
