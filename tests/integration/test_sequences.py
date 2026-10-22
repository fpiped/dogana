import random
from datetime import UTC, datetime, timedelta
from itertools import product

import pytest

pytest.importorskip("duckdb")

from dogana import (
    Checkpoint,
    Column,
    Completeness,
    Contract,
    Coverage,
    Deletions,
    Ledger,
    Manifest,
    Region,
    Shape,
)
from dogana.reference.duckdb import DuckDBStore
from tests.integration.support import csv_file


def shapes():
    result = {}
    for properties in product(Coverage, Completeness, Deletions, Checkpoint):
        try:
            shape = Shape(*properties)
        except ValueError:
            continue
        result["_".join(properties)] = shape
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
                assert (new.complete_as_of, new.valid_as_of, new.generation) == (
                    old.complete_as_of,
                    old.valid_as_of,
                    old.generation,
                )


def test_seeded_delivery_sequences_cover_all_fourteen_shapes_and_fold_after_every_attempt(tmp_path):
    rng = random.Random(8128)
    declared = shapes()
    full = "all_complete_absence_none"
    c = Contract(
        "records",
        ("id",),
        declared,
        "day",
        (Column("id", "int"), Column("day", "int"), Column("value", "str")),
    )
    accepted_shapes, replayed, rejected = set(), 0, 0
    expected, previous = {}, []
    with DuckDBStore(":memory:") as store:
        order = [name for name in declared for name in (full, name)] + rng.choices(list(declared), k=50)
        for step, name in enumerate(order):
            before = store.load_ledger(c)
            if previous and step >= 28 and rng.random() < 0.15:
                old = rng.choice(previous)
                assert store.deliver(c, object(), old.manifest) == old
                replayed += 1
                assert Ledger.from_receipts(c, store.receipts(c.table)) == before
                continue
            shape = c.shape(name)
            until = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=step)
            window = (rng.randint(0, 3), rng.randint(4, 8)) if shape.coverage == "window" else None
            keys = [k for k in range(9) if window is None or window[0] <= k <= window[1]]
            incoming = rng.sample(keys, rng.randint(0, len(keys)))
            rows = [{"id": k, "day": k, "value": f"v{step}"} for k in incoming]
            tombstones = tuple((k,) for k in keys if k not in incoming and rng.random() < 0.5)
            since = None
            if not shape.complete:
                since = (
                    before.generation
                    if shape.checkpoint == "generation"
                    else datetime(2025, 1, 1, tzinfo=UTC)
                )
                if step >= 28 and rng.random() < 0.2:
                    since = "broken" if shape.checkpoint == "generation" else until + timedelta(days=1)
            if step >= 28 and rng.random() < 0.2:
                until -= timedelta(days=1)
            manifest = Manifest(
                c.table,
                name,
                until,
                window=window,
                since=since,
                generation=f"g{step}",
                rows=len(rows),
                schema_hash=c.schema_hash,
                keys=tuple((k,) for k in incoming) if shape.coverage == "keys" else (),
                tombstones=tombstones if shape.deletions == "tombstone" else (),
            )
            source = csv_file(tmp_path, "incoming.csv", rows)
            receipt = store.deliver(c, source, manifest)
            after = store.load_ledger(c)
            if receipt.accepted:
                accepted_shapes.add(name)
                invariants(before, after, manifest, shape)
                if shape.complete:
                    expected = {
                        k: r
                        for k, r in expected.items()
                        if window is not None and not window[0] <= r["day"] <= window[1]
                    }
                expected.update({r["id"]: r for r in rows})
                for key in manifest.tombstones:
                    expected.pop(key[0], None)
                previous.append(receipt)
            else:
                rejected += 1
                assert after == before
            assert store.read(c.table) == sorted(expected.values(), key=lambda r: r["id"])
            assert Ledger.from_receipts(c, store.receipts(c.table)) == after
        assert accepted_shapes == set(declared)
        assert replayed > 0 and rejected > 0


def test_seeded_append_intervals_replays_and_gaps_keep_retained_history_exact(tmp_path):
    rng = random.Random(37)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    c = Contract(
        "events",
        (),
        {"log": Shape("all", "changed_only", "none", "timestamp")},
        schema=(Column("event", "str"),),
        start_at=start,
    )
    events, accepted = [], []
    with DuckDBStore(":memory:") as store:
        for step in range(30):
            before = store.load_ledger(c)
            since = before.regions[0].complete_as_of or start
            gap = step > 0 and rng.random() < 0.3
            manifest = Manifest(
                c.table,
                "log",
                since + timedelta(hours=1),
                since=since + timedelta(seconds=1) if gap else since,
                rows=1,
                schema_hash=c.schema_hash,
            )
            rows = [{"event": f"event{step:02}"}]
            receipt = store.deliver(c, csv_file(tmp_path, "event.csv", rows, ("event",)), manifest)
            assert receipt.accepted is not gap
            if receipt.accepted:
                events.extend(rows)
                accepted.append(receipt)
            if accepted:
                old = rng.choice(accepted)
                assert store.deliver(c, object(), old.manifest) == old
            assert store.read(c.table) == sorted(events, key=lambda r: r["event"])
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
        first = store.deliver(c, source, sidecar(source, c, "full", 18, 2, generation="g1"))
        source = csv_file(tmp_path, "feed.csv", [row(2, value="changed")])
        manifest = sidecar(source, c, "feed", 19, 1, since="g1", generation="g2", keys=((2,),))
        second = store.deliver(c, source, manifest)
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
            receipt = store.deliver(c, source, attempt)
            if attempt.identity == first.manifest.identity:
                assert receipt == first
            elif attempt.identity == manifest.identity:
                assert receipt == second
            else:
                assert not receipt.accepted
            assert store.load_ledger(c) == second.ledger == Ledger.from_receipts(c, store.receipts(c.table))
            assert store.read(c.table) == [row(1), row(2, value="changed")]
        assert sum(r.accepted for r in store.receipts(c.table)) == 2
