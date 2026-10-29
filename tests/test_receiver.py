"""A receiver written against the gate: the in-memory loader does what the protocol asks of one."""

from dataclasses import replace
from datetime import timedelta

import pytest

from dogana import ApplyPlan, Gate, InconsistentPublication, Ledger, Operation, Outcome, State
from tests.loader import Loader
from tests.support import FULL, SINCE, at, contract


def rows(*keys, day=18):
    return [{"id": key, "day": at(day).date(), "value": "current"} for key in keys]


@pytest.mark.protocol(
    "receiver.sequence",
    "receiver.3",
    "receiver.4",
    "receiver.obligations.1",
    "receiver.obligations.3",
    "trust-model.3",
)
def test_an_accepted_delivery_changes_rows_and_ledger_together_and_a_rejected_one_changes_neither():
    loader, c = Loader(), contract()
    first = loader.deliver(c, rows(1, 2), c.manifest("full", at(18), rows=2), at(18))
    assert first.accepted and loader.read(c.table) == rows(1, 2)
    assert loader.ledger(c) == Ledger(c).follow(first)
    short = loader.deliver(c, rows(3), c.manifest("full", at(19), rows=2), at(19))
    assert not short.accepted
    assert [check.name for check in short.checks if not check.passed] == ["row_count"]
    assert loader.read(c.table) == rows(1, 2) and loader.ledger(c) == Ledger(c).follow(first)
    assert Ledger.from_receipts(c, loader.receipts_of(c.table)) == loader.ledger(c)


@pytest.mark.protocol("receiver.2")
def test_a_manifest_the_ledger_refuses_is_recorded_without_reading_any_row():
    loader, c = Loader(), contract()
    assert loader.deliver(c, rows(1), c.manifest("full", at(18), rows=1), at(18)).accepted
    earlier = loader.deliver(c, rows(1), c.manifest("full", at(17), rows=1), at(19))
    assert not earlier.accepted
    assert [check.name for check in earlier.checks] == ["manifest", "transition"]
    assert loader.read(c.table) == rows(1)


@pytest.mark.protocol("receiver.1", "contract.deleted-content", "manifest.corrections")
def test_a_repeated_manifest_replays_unread_and_a_corrected_file_is_a_new_delivery_at_a_later_instant():
    loader, c = Loader(), contract()
    manifest = c.manifest("full", at(18), rows=1)
    first = loader.deliver(c, rows(1), manifest, at(18))
    again = loader.deliver(c, rows(2), manifest, at(19))
    assert again.outcome is Outcome.REPLAYED and again.identity == first.identity
    assert loader.read(c.table) == rows(1)
    corrected = replace(manifest, content_hash="a" * 64)
    assert corrected.identity != manifest.identity
    assert not loader.deliver(c, rows(2), corrected, at(19)).accepted
    later = loader.deliver(c, rows(2), replace(corrected, produced_at=at(19)), at(19))
    assert later.accepted and loader.read(c.table) == rows(2)


@pytest.mark.protocol("receiver.obligations.2", "receiver.chain")
def test_a_decision_made_against_a_stale_ledger_cannot_be_committed():
    c, stale = contract(), Ledger(contract())
    first = Gate.evaluate(stale, c.manifest("full", at(18), rows=0), now=at(18))
    current = stale.follow(first)
    second = Gate.evaluate(stale, c.manifest("full", at(19), rows=0), now=at(19))
    assert second.base_publication_id != current.publication_id
    with pytest.raises(InconsistentPublication, match="parent"):
        current.follow(second)
    remade = Gate.evaluate(current, second.manifest, now=at(19))
    assert current.follow(remade).publication_id == remade.publication_id


@pytest.mark.protocol("trust-model.1", "checks.implied", "checks.declarations")
def test_a_truncated_export_passes_with_the_count_it_was_given_and_fails_with_the_sources_count():
    loader, c = Loader(), contract()
    truncated = rows(1, 2)
    assert loader.deliver(c, truncated, c.manifest("full", at(18), rows=2), at(18)).accepted
    honest = loader.deliver(c, truncated, c.manifest("full", at(19), rows=3), at(19))
    assert not honest.accepted
    assert any(check.name == "row_count" and not check.passed for check in honest.checks)


@pytest.mark.protocol("checks.blocked")
def test_a_window_file_carrying_a_key_published_in_another_region_is_refused():
    loader, c = Loader(), contract()
    published = rows(1, day=18) + rows(2, day=19)
    assert loader.deliver(c, published, c.manifest("full", at(20), rows=2), at(20)).accepted
    window = (at(19).date(), at(19).date())
    moved = c.manifest("daily", at(21), window=window, rows=1)
    assert [a.operation for a in ApplyPlan.build(c, moved).actions] == [Operation.REPLACE_WINDOW]
    refused = loader.deliver(c, rows(1, day=19), moved, at(21))
    assert not refused.accepted
    assert any(check.name == "rows_in_window" and not check.passed for check in refused.checks)
    assert loader.read(c.table) == published


@pytest.mark.protocol("contract.soft-delete-fallback")
def test_changes_without_deletions_keep_a_removed_row_until_a_complete_read():
    loader, c = Loader(), contract(shapes={"full": FULL, "since": SINCE})
    assert loader.deliver(c, rows(1, 2), c.manifest("full", at(18), rows=2), at(18)).accepted
    changed = loader.deliver(c, rows(1), c.manifest("since", at(19), since=at(18), rows=1), at(19))
    assert changed.accepted and sorted(loader.read(c.table), key=lambda row: row["id"]) == rows(1, 2)
    claims = loader.ledger(c).claims(at(19), limit=timedelta.max)
    assert (claims.state, claims.deletions_as_of) == (State.PATCHED, at(18))
    assert loader.deliver(c, rows(1), c.manifest("full", at(20), rows=1), at(20)).accepted
    assert loader.read(c.table) == rows(1)
    assert loader.ledger(c).claims(at(20), limit=timedelta.max).exact_as_of == at(20)
