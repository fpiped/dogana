from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import product

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.support import COLUMNS, at, schema_hash

pytest.importorskip("duckdb")

from dogana import (
    Checkpoint,
    Completeness,
    Contract,
    Coverage,
    Deletions,
    Ledger,
    Manifest,
    Region,
    Shape,
)
from dogana.reference._sources import _FileSource, _RowSource
from dogana.reference.duckdb import DuckDBStore
from tests.integration.support import csv_file


def shapes():
    result = {}
    for coverage, completeness, deletions, checkpoint in product(
        Coverage, Completeness, Deletions, Checkpoint
    ):
        try:
            shape = Shape(
                coverage=coverage, completeness=completeness, deletions=deletions, checkpoint=checkpoint
            )
        except ValueError:
            continue
        result["_".join((coverage, completeness, deletions, checkpoint))] = shape
    return result


def invariants(before, after, manifest, shape):
    coverage = Region(*manifest.window) if manifest.window else Region()
    assert after.regions[0].low is None and after.regions[-1].high is None
    assert all(a.touches(b) for a, b in zip(after.regions, after.regions[1:], strict=False))
    for new in after.regions:
        if new.complete_as_of is not None:
            assert new.valid_as_of <= new.complete_as_of
        if new.intersection(coverage) is not None:
            assert new.complete_as_of == manifest.produced_at
            if shape.complete:
                assert new.valid_as_of == manifest.produced_at
        for old in before.regions:
            if new.intersection(old) is not None and new.intersection(coverage) is None:
                assert (new.complete_as_of, new.valid_as_of) == (
                    old.complete_as_of,
                    old.valid_as_of,
                )


RECORD_COLUMNS = {"id": "int", "day": "int", "value": "str"}
DECLARED = shapes()
START = datetime(2026, 1, 1, tzinfo=UTC)
KEYS = st.sets(st.integers(min_value=0, max_value=8), max_size=9)
ATTEMPTS = st.tuples(
    st.sampled_from(tuple(DECLARED)),
    KEYS,
    KEYS,
    st.integers(min_value=0, max_value=2),
    st.integers(min_value=0, max_value=2),
    st.sampled_from(("valid", "count", "stale", "replay")),
    st.integers(min_value=0, max_value=30),
)


def sequence_contract():
    return Contract(
        "records",
        key=("id",),
        shapes=DECLARED,
        window_column="day",
        types={"id": "int", "day": "int"},
        schema_hash=schema_hash(RECORD_COLUMNS),
        max_shrink_fraction=None,
    )


def baseline(store, c):
    rows = [{"id": key, "day": key % 3, "value": "initial"} for key in range(9)]
    manifest = Manifest(
        c.table,
        shape="all_complete_absence_none",
        produced_at=START,
        rows=len(rows),
        schema_hash=c.schema_hash,
        generation="g0",
    )
    receipt = store.deliver(c, _RowSource(rows, RECORD_COLUMNS), manifest, now=at(30))
    assert receipt.accepted
    return receipt, {row["id"]: row for row in rows}


def attempt(c, before, step, name, live, deleted, low, high, issue):
    shape = c.shape(name)
    window = (min(low, high), max(low, high)) if shape.coverage is Coverage.WINDOW else None
    covered = {key for key in range(9) if window is None or window[0] <= key % 3 <= window[1]}
    incoming = sorted(live & covered)
    rows = [{"id": key, "day": key % 3, "value": f"v{step}"} for key in incoming]
    manifest = Manifest(
        c.table,
        shape=name,
        produced_at=START if issue == "stale" else START + timedelta(minutes=step),
        window=window,
        since=None
        if shape.complete
        else (before.generation if shape.checkpoint is Checkpoint.GENERATION else START),
        generation=f"g{step}"
        if shape.checkpoint is Checkpoint.GENERATION or (shape.complete and shape.coverage is Coverage.ALL)
        else None,
        rows=len(rows) + int(issue == "count"),
        schema_hash=c.schema_hash,
        keys=tuple((key,) for key in incoming) if shape.coverage is Coverage.KEYS else (),
        tombstones=tuple((key,) for key in sorted((deleted & covered) - live))
        if shape.deletions is Deletions.TOMBSTONE
        else (),
    )
    return rows, manifest


def expected_rows(previous, rows, manifest, shape):
    result = dict(previous)
    if shape.complete:
        window = manifest.window
        result = {
            key: row
            for key, row in result.items()
            if window is not None and not window[0] <= row["day"] <= window[1]
        }
    result.update({row["id"]: row for row in rows})
    for key in manifest.tombstones:
        result.pop(key[0], None)
    return result


@pytest.mark.parametrize("name", tuple(DECLARED))
@settings(max_examples=8, deadline=None)
@given(live=KEYS, deleted=KEYS, low=st.integers(0, 2), high=st.integers(0, 2))
def test_every_admitted_shape_publishes_its_declared_row_effect(name, live, deleted, low, high):
    c = sequence_contract()
    with DuckDBStore(":memory:") as store:
        first, previous = baseline(store, c)
        rows, manifest = attempt(c, first.ledger, 1, name, live, deleted, low, high, "valid")
        receipt = store.deliver(c, _RowSource(rows, RECORD_COLUMNS), manifest, now=at(30))
        assert receipt.accepted, receipt.checks
        expected = expected_rows(previous, rows, manifest, c.shape(name))
        assert store.read(c.table) == [expected[key] for key in sorted(expected)]
        invariants(first.ledger, receipt.ledger, manifest, c.shape(name))
        assert Ledger.from_receipts(c, store.receipts(c.table)) == receipt.ledger


@settings(max_examples=25, deadline=None)
@given(attempts=st.lists(ATTEMPTS, min_size=1, max_size=15))
def test_delivery_histories_preserve_rows_evidence_and_old_publications(attempts):
    c = sequence_contract()
    with DuckDBStore(":memory:") as store:
        first, expected = baseline(store, c)
        accepted = [(first, store.read(c.table))]
        for step, (name, live, deleted, low, high, issue, index) in enumerate(attempts, 1):
            before = store.load_ledger(c)
            retained = store.receipts(c.table)
            old, snapshot = accepted[index % len(accepted)]
            if issue == "replay":
                receipt = store.deliver(c, object(), old.manifest, now=at(30))
                assert receipt == replace(old, outcome="replayed")
                assert receipt.identity == old.identity
                assert store.receipts(c.table) == retained
                assert store.load_ledger(c) == before
            else:
                rows, manifest = attempt(c, before, step, name, live, deleted, low, high, issue)
                receipt = store.deliver(c, _RowSource(rows, RECORD_COLUMNS), manifest, now=at(30))
                assert receipt.accepted is (issue == "valid"), receipt.checks
                assert store.receipts(c.table) == (*retained, receipt)
                if issue == "valid":
                    invariants(before, receipt.ledger, manifest, c.shape(name))
                    expected = expected_rows(expected, rows, manifest, c.shape(name))
                    accepted.append((receipt, [expected[key] for key in sorted(expected)]))
                else:
                    assert store.load_ledger(c) == before
            assert store.read(c.table) == [expected[key] for key in sorted(expected)]
            assert store.read(c.table, old.publication_id) == snapshot
            assert Ledger.from_receipts(c, store.receipts(c.table)) == store.load_ledger(c)


@settings(max_examples=25, deadline=None)
@given(
    attempts=st.lists(
        st.tuples(
            st.integers(min_value=-1, max_value=1),
            st.lists(st.sampled_from(("same", "a", "b")), max_size=4),
            st.integers(min_value=0, max_value=20),
        ),
        min_size=1,
        max_size=15,
    )
)
def test_append_intervals_reject_gaps_and_overlaps_without_losing_duplicates(attempts):
    columns = {"event": "str"}
    c = Contract(
        "events",
        key=(),
        shapes={
            "log": Shape(
                coverage=Coverage.ALL,
                completeness=Completeness.CHANGED_ONLY,
                deletions=Deletions.NONE,
                checkpoint=Checkpoint.TIMESTAMP,
            )
        },
        schema_hash=schema_hash(columns),
        start_at=START,
    )
    events = []
    boundary = START + timedelta(minutes=1)
    with DuckDBStore(":memory:") as store:
        first = store.deliver(
            c,
            _RowSource([], columns),
            Manifest(
                c.table,
                shape="log",
                produced_at=boundary,
                since=START,
                rows=0,
                schema_hash=c.schema_hash,
            ),
            now=at(30),
        )
        assert first.accepted
        accepted = [(first, [])]
        for offset, batch, index in attempts:
            before = store.load_ledger(c)
            retained = store.receipts(c.table)
            manifest = Manifest(
                c.table,
                shape="log",
                produced_at=boundary + timedelta(hours=1),
                since=boundary + timedelta(seconds=offset),
                rows=len(batch),
                schema_hash=c.schema_hash,
            )
            rows = [{"event": value} for value in batch]
            receipt = store.deliver(c, _RowSource(rows, columns), manifest, now=at(30))
            assert receipt.accepted is (offset == 0), receipt.checks
            assert store.receipts(c.table) == (*retained, receipt)
            if offset == 0:
                boundary = manifest.produced_at
                events.extend(rows)
                accepted.append((receipt, sorted(events, key=lambda row: row["event"])))
            else:
                assert store.load_ledger(c) == before
            old, snapshot = accepted[index % len(accepted)]
            retained = store.receipts(c.table)
            assert store.deliver(c, object(), old.manifest, now=at(30)) == replace(old, outcome="replayed")
            assert store.receipts(c.table) == retained
            assert store.read(c.table, old.publication_id) == snapshot
            assert store.read(c.table) == sorted(events, key=lambda row: row["event"])
            assert Ledger.from_receipts(c, store.receipts(c.table)) == store.load_ledger(c)


def test_replay_rejection_overlapping_windows_and_generation_breaks_interleave_without_new_publications(
    tmp_path,
):
    from dataclasses import replace

    from tests.integration.support import row, sidecar
    from tests.support import contract

    c = contract()
    with DuckDBStore(":memory:") as store:
        source = csv_file(tmp_path, "base.csv", [row(1), row(2)])
        first = store.deliver(
            c, _FileSource(source, COLUMNS), sidecar(source, c, "full", 18, 2, generation="g1"), now=at(30)
        )
        source = csv_file(tmp_path, "feed.csv", [row(2, value="changed")])
        manifest = sidecar(source, c, "feed", 19, 1, since="g1", generation="g2", keys=((2,),))
        second = store.deliver(c, _FileSource(source, COLUMNS), manifest, now=at(30))
        assert first.accepted and second.accepted
        bad_window = Manifest(
            c.table,
            "daily",
            first.manifest.produced_at,
            window=(row(1)["day"], row(2)["day"]),
            rows=1,
            schema_hash=c.schema_hash,
        )
        broken = replace(manifest, produced_at=manifest.produced_at + timedelta(days=1), since="restored")
        for attempt in (first.manifest, bad_window, broken, manifest, bad_window, broken, first.manifest):
            receipt = store.deliver(c, _FileSource(source, COLUMNS), attempt, now=at(30))
            if attempt.identity == first.manifest.identity:
                assert receipt == replace(first, outcome="replayed")
            elif attempt.identity == manifest.identity:
                assert receipt == replace(second, outcome="replayed")
            else:
                assert not receipt.accepted
            assert store.load_ledger(c) == second.ledger == Ledger.from_receipts(c, store.receipts(c.table))
            assert store.read(c.table) == [row(1), row(2, value="changed")]
        assert sum(r.accepted for r in store.receipts(c.table)) == 2
