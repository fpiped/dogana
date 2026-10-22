"""Random delivery sequences never break the ledger's invariants."""

import random
from datetime import UTC, date, datetime, timedelta

from dogana import Contract, Ledger, Manifest, Region, RejectedDelivery, Shape

CONTRACT = Contract(
    "moves",
    ("id",),
    {
        "full": Shape("all", "complete", "absence", "none"),
        "daily": Shape("window", "complete", "absence", "none"),
        "recent": Shape("window", "changed_only", "none", "timestamp"),
        "since": Shape("all", "changed_only", "none", "timestamp"),
    },
    window_column="day",
)


def random_manifest(rng: random.Random) -> Manifest:
    shape = rng.choice(list(CONTRACT.shapes))
    produced = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=rng.randint(0, 120_000))
    window = None
    if CONTRACT.shapes[shape].coverage == "window":
        low = rng.randint(1, 60)
        window = (
            date(2026, 1, 1) + timedelta(days=low),
            date(2026, 1, 1) + timedelta(days=low + rng.randint(0, 30)),
        )
    since = None if CONTRACT.shapes[shape].complete else produced - timedelta(hours=rng.randint(0, 500))
    return Manifest("moves", shape, produced, window=window, since=since, rows=0)


def test_random_delivery_sequences_keep_every_ledger_invariant():
    rng = random.Random(7)
    accepted = 0
    for _ in range(200):
        ledger = Ledger(CONTRACT)
        for _ in range(40):
            manifest = random_manifest(rng)
            try:
                after = ledger.apply(manifest)
            except RejectedDelivery:
                continue
            accepted += 1
            coverage = Region(*manifest.window) if manifest.window else Region()
            assert after.regions[0].low is None and after.regions[-1].high is None
            assert all(a.touches(b) for a, b in zip(after.regions, after.regions[1:], strict=False))
            for region in after.regions:
                if region.complete_as_of is None:
                    continue
                assert region.valid_as_of <= region.complete_as_of
                if region.intersection(coverage) is not None:
                    assert region.complete_as_of == manifest.produced_at
                    if CONTRACT.shapes[manifest.shape].complete:
                        assert region.valid_as_of == manifest.produced_at
            for new in after.regions:
                for old in ledger.regions:
                    if new.intersection(old) is None:
                        continue
                    if new.intersection(coverage) is None:
                        assert (new.complete_as_of, new.valid_as_of) == (old.complete_as_of, old.valid_as_of)
                    elif old.complete_as_of is not None:
                        assert new.complete_as_of >= old.complete_as_of
                        assert new.valid_as_of >= old.valid_as_of
            ledger = after
    assert accepted > 500
