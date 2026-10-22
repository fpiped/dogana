from dataclasses import replace

import pytest

from dogana import (
    DoganaError,
    InconsistentPublication,
    InvalidContract,
    Manifest,
    PlanningError,
    RejectedDelivery,
    SerializationError,
    StoreError,
    deliver,
)
from tests.integration.support import row
from tests.memory_store import MemoryStore
from tests.support import at, contract


def seed(store):
    c = contract()
    manifest = Manifest(c.table, "full", at(18), rows=1, schema_hash=c.schema_hash)
    return c, deliver(store, c, [row(1)], manifest, at(20))


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


@pytest.mark.parametrize("operation", ["hold", "check", "execute", "publish"])
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
    assert store.load_ledger(c) == first.ledger
    assert store.read(c.table) == [row(1)]
    assert not store.candidates


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


def test_a_replayed_delivery_retains_its_receipt_and_original_receiver_clock():
    store = MemoryStore()
    c, first = seed(store)
    replay = deliver(store, c, object(), first.manifest, at(25))
    assert replay == replace(first, replayed=True)
    assert replay.identity == first.identity
    assert replay.received_at == first.received_at
    assert store.receipts(c.table) == (first,)


def test_a_lost_accepted_receipt_cannot_be_replaced_by_another_acceptance():
    store = MemoryStore()
    c, first = seed(store)
    store.state[2].clear()
    with pytest.raises(InconsistentPublication, match="receipt is missing"):
        deliver(store, c, [row(2)], first.manifest, at(20))
    assert not store.receipts(c.table)
    assert store.read(c.table) == [row(1)]


def test_an_interrupted_delivery_aborts_before_propagating(monkeypatch):
    store = MemoryStore()
    c, first = seed(store)

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "execute", interrupt)
    with pytest.raises(KeyboardInterrupt):
        deliver(store, c, [row(2)], replace(first.manifest, produced_at=at(19)), at(20))
    assert not store.candidates
    assert store.receipts(c.table) == (first,)


def test_delivery_failures_share_one_error_root_and_serialization_is_a_declaration_error():
    for error in (InvalidContract, RejectedDelivery, PlanningError, StoreError):
        assert issubclass(error, DoganaError)
    assert issubclass(SerializationError, InvalidContract)
    assert issubclass(InconsistentPublication, StoreError)
