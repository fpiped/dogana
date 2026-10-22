from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from dogana import Column, Contract, Gate, Ledger, Manifest, Outcome, Receipt, Shape, StoreError, deliver
from dogana.errors import LedgerAhead, StampAhead
from tests.memory_store import CommitMemoryStore, MemoryStore

START = datetime(2026, 1, 1, tzinfo=UTC)
NOW = START + timedelta(days=1)
VALUES = st.dictionaries(st.integers(0, 8), st.integers(-9, 9), max_size=9)


def rows_for(values: dict[int, int]) -> list[dict[str, int]]:
    return [{"id": key, "bucket": key % 3, "value": value} for key, value in sorted(values.items())]


class PublicationSequences(RuleBasedStateMachine):
    store_type = MemoryStore

    def __init__(self) -> None:
        super().__init__()
        self.store = self.store_type()
        self.contract = Contract(
            "records",
            key=("id",),
            shapes={
                "full": Shape.full(),
                "window": Shape.window(),
                "patch": Shape.changes(),
                "feed": Shape.feed(),
            },
            window_column="bucket",
            schema=(Column("id", "int"), Column("bucket", "int"), Column("value", "int")),
            max_shrink_fraction=None,
        )
        self.tick = 0
        self.values: dict[int, int] = {}
        self.evidence = {bucket: (START, START) for bucket in range(3)}
        self.generation = "g0"
        first = deliver(self.store, self.contract, [], self.full_manifest({}, START), NOW)
        assert first.accepted
        self.accepted = [first]
        self.attempts = [first]
        self.snapshots: dict[str, list[dict[str, int]]] = {}
        assert first.publication_id is not None
        self.snapshots[first.publication_id] = []
        self.interrupted: list[tuple[Manifest, dict[int, int]]] = []

    def next_time(self) -> datetime:
        self.tick += 1
        return START + timedelta(seconds=self.tick)

    def full_manifest(self, values: dict[int, int], boundary: datetime) -> Manifest:
        return Manifest(
            self.contract.table,
            shape="full",
            produced_at=boundary,
            generation=f"g{self.tick}",
            rows=len(values),
            schema_hash=self.contract.schema_hash,
        )

    def remember(self, receipt: Receipt) -> None:
        assert receipt.outcome == "accepted"
        assert receipt.publication_id is not None
        self.accepted.append(receipt)
        self.attempts.append(receipt)
        self.snapshots[receipt.publication_id] = rows_for(self.values)

    def accept_full(self, values: dict[int, int], manifest: Manifest | None = None) -> Receipt:
        manifest = manifest or self.full_manifest(values, self.next_time())
        receipt = deliver(self.store, self.contract, rows_for(values), manifest, NOW)
        assert receipt.accepted, receipt.checks
        self.values = dict(values)
        self.evidence = {bucket: (manifest.produced_at, manifest.produced_at) for bucket in range(3)}
        assert manifest.generation is not None
        self.generation = manifest.generation
        self.remember(receipt)
        return receipt

    def remember_rejection(self, receipt: Receipt) -> None:
        assert receipt.outcome == "rejected"
        assert receipt.ledger == self.accepted[-1].ledger
        self.attempts.append(receipt)

    @rule(values=VALUES)
    def replace_all(self, values: dict[int, int]) -> None:
        self.accept_full(values)

    @rule(values=VALUES, low=st.integers(0, 2), high=st.integers(0, 2))
    def replace_window(self, values: dict[int, int], low: int, high: int) -> None:
        low, high = min(low, high), max(low, high)
        incoming = {key: value for key, value in values.items() if low <= key % 3 <= high}
        boundary = self.next_time()
        manifest = Manifest(
            self.contract.table,
            shape="window",
            produced_at=boundary,
            window=(low, high),
            rows=len(incoming),
            schema_hash=self.contract.schema_hash,
        )
        receipt = deliver(self.store, self.contract, rows_for(incoming), manifest, NOW)
        assert receipt.accepted, receipt.checks
        self.values = {key: value for key, value in self.values.items() if not low <= key % 3 <= high}
        self.values.update(incoming)
        for bucket in range(low, high + 1):
            self.evidence[bucket] = boundary, boundary
        self.remember(receipt)

    @rule(values=VALUES)
    def patch_without_deletions(self, values: dict[int, int]) -> None:
        boundary = self.next_time()
        manifest = Manifest(
            self.contract.table,
            shape="patch",
            produced_at=boundary,
            since=START,
            rows=len(values),
            schema_hash=self.contract.schema_hash,
        )
        receipt = deliver(self.store, self.contract, rows_for(values), manifest, NOW)
        assert receipt.accepted, receipt.checks
        self.values.update(values)
        self.evidence = {bucket: (boundary, valid) for bucket, (_, valid) in self.evidence.items()}
        self.remember(receipt)

    @rule(values=VALUES, deleted=st.sets(st.integers(0, 8), max_size=9))
    def generation_feed(self, values: dict[int, int], deleted: set[int]) -> None:
        deleted = deleted - values.keys()
        boundary = self.next_time()
        manifest = Manifest(
            self.contract.table,
            shape="feed",
            produced_at=boundary,
            since=self.generation,
            generation=f"g{self.tick}",
            rows=len(values),
            schema_hash=self.contract.schema_hash,
            keys=tuple((key,) for key in sorted(values)),
            tombstones=tuple((key,) for key in sorted(deleted)),
        )
        receipt = deliver(self.store, self.contract, rows_for(values), manifest, NOW)
        assert receipt.accepted, receipt.checks
        self.values.update(values)
        for key in deleted:
            self.values.pop(key, None)
        self.evidence = {
            bucket: (boundary, boundary if valid == complete else valid)
            for bucket, (complete, valid) in self.evidence.items()
        }
        assert manifest.generation is not None
        self.generation = manifest.generation
        self.remember(receipt)

    @rule(index=st.integers(0, 100))
    def replay_an_earlier_acceptance(self, index: int) -> None:
        old = self.accepted[index % len(self.accepted)]
        replay = deliver(self.store, self.contract, object(), old.manifest, NOW + timedelta(hours=1))
        assert replay == replace(old, outcome=Outcome.REPLAYED)
        assert replay.identity == old.identity
        assert replay.received_at == old.received_at
        assert self.store.accepted_receipt(old.manifest.identity) == old

    @rule(values=VALUES)
    def reject_a_wrong_row_count(self, values: dict[int, int]) -> None:
        manifest = replace(self.full_manifest(values, self.next_time()), rows=len(values) + 1)
        self.remember_rejection(deliver(self.store, self.contract, rows_for(values), manifest, NOW))

    @rule()
    def reject_an_out_of_order_replacement(self) -> None:
        self.next_time()
        manifest = self.full_manifest({0: self.tick}, START)
        self.remember_rejection(deliver(self.store, self.contract, object(), manifest, NOW))

    @rule()
    def reject_a_broken_generation(self) -> None:
        boundary = self.next_time()
        manifest = Manifest(
            self.contract.table,
            shape="feed",
            produced_at=boundary,
            since=f"missing{self.tick}",
            generation=f"g{self.tick}",
            rows=0,
            schema_hash=self.contract.schema_hash,
        )
        self.remember_rejection(deliver(self.store, self.contract, object(), manifest, NOW))

    @rule(values=VALUES, interrupt=st.booleans())
    def crash_before_publication(self, values: dict[int, int], interrupt: bool) -> None:
        manifest = self.full_manifest(values, self.next_time())
        error = KeyboardInterrupt if interrupt else OSError
        expected_error = KeyboardInterrupt if interrupt else StoreError
        with patch.object(
            self.store, "_publication_point", side_effect=error("publication interrupted")
        ) as fault:
            with pytest.raises(expected_error, match="publication interrupted"):
                deliver(self.store, self.contract, rows_for(values), manifest, NOW)
            fault.assert_called_once_with()
        assert self.store.accepted_receipt(manifest.identity) is None
        self.interrupted.append((manifest, dict(values)))

    @precondition(lambda self: bool(self.interrupted))
    @rule(index=st.integers(0, 100))
    def retry_an_interrupted_delivery(self, index: int) -> None:
        manifest, values = self.interrupted.pop(index % len(self.interrupted))
        latest = max(complete for complete, _ in self.evidence.values())
        if manifest.produced_at > latest:
            self.accept_full(values, manifest)
        else:
            self.remember_rejection(deliver(self.store, self.contract, rows_for(values), manifest, NOW))

    @rule(values=VALUES)
    def discard_a_candidate_checked_before_another_publication(self, values: dict[int, int]) -> None:
        manifest = self.full_manifest(values, self.next_time())
        candidate = self.store.hold(rows_for(values), manifest, self.contract)
        checks = self.store.check(
            candidate, self.contract, manifest, Gate.requirements(self.contract, manifest)
        )
        stale = Gate.evaluate(self.accepted[-1].ledger, manifest, checks, now=NOW)
        assert stale.accepted
        self.accept_full({0: self.tick})
        with pytest.raises(StoreError, match="base publication moved"):
            self.store.publish(candidate, stale)

    @rule()
    def refuse_to_persist_a_rejection_after_its_base_moves(self) -> None:
        manifest = self.full_manifest({0: 0}, START)
        stale = Gate.evaluate(self.accepted[-1].ledger, manifest, now=NOW)
        assert not stale.accepted
        self.accept_full({0: self.tick})
        with pytest.raises(StoreError, match="base publication moved"):
            self.store.publish(None, stale)

    @rule()
    def repair_a_lost_ledger_from_receipts_and_the_row_stamp(self) -> None:
        self.store.state[1].pop(self.contract.table)
        with pytest.raises(StampAhead):
            self.store.load_ledger(self.contract)
        assert self.store.repair(self.contract) == self.accepted[-1].ledger

    @precondition(lambda self: len(self.accepted) > 1)
    @rule(index=st.integers(0, 100))
    def repair_a_cached_prefix_without_rewinding_rows(self, index: int) -> None:
        older = self.accepted[index % (len(self.accepted) - 1)]
        self.store.state[1][self.contract.table] = older.ledger
        with pytest.raises(StampAhead):
            self.store.load_ledger(self.contract)
        assert self.store.repair(self.contract) == self.accepted[-1].ledger

    @rule(index=st.integers(0, 100))
    def refuse_to_relabel_rows_with_newer_ledger_evidence(self, index: int) -> None:
        table = self.contract.table
        rows, current = self.store.state[0][table]
        earlier = [None, *(receipt.publication_id for receipt in self.accepted[:-1])]
        self.store.state[0][table] = rows, earlier[index % len(earlier)]
        try:
            with pytest.raises(LedgerAhead):
                self.store.load_ledger(self.contract)
            with pytest.raises(LedgerAhead):
                self.store.repair(self.contract)
        finally:
            self.store.state[0][table] = rows, current

    @invariant()
    def rows_claims_and_retained_history_describe_one_publication(self) -> None:
        table = self.contract.table
        current = self.store.load_ledger(self.contract)
        assert current == self.accepted[-1].ledger
        assert self.store.publication_stamp(table) == current.publication_id
        assert self.store.receipts(table) == tuple(self.attempts)
        assert Ledger.from_receipts(self.contract, self.attempts) == current
        assert current.generation == self.generation
        assert sorted(self.store.read(table), key=lambda row: row["id"]) == rows_for(self.values)
        for bucket, dates in self.evidence.items():
            claims = current.claims(NOW, window=(bucket, bucket))
            assert (claims.complete_as_of, claims.valid_as_of) == dates
        for publication, rows in self.snapshots.items():
            assert sorted(self.store.read(table, publication), key=lambda row: row["id"]) == rows
        assert not self.store.candidates
        assert not self.store.checked
        assert not self.store.executed


class CommitPublicationSequences(PublicationSequences):
    store_type = CommitMemoryStore


TestMemoryPublicationSequences = PublicationSequences.TestCase
TestCommitPublicationSequences = CommitPublicationSequences.TestCase
for case in (TestMemoryPublicationSequences, TestCommitPublicationSequences):
    case.settings = settings(max_examples=25, stateful_step_count=25, deadline=None)
