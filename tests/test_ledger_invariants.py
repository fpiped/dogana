"""Arbitrary delivery sequences preserve evidence and domain coverage."""

from datetime import UTC, datetime, timedelta

from hypothesis import example, given, settings
from hypothesis import strategies as st

from dogana import Contract, Ledger, Manifest, Region, RejectedDelivery
from dogana.contract import Window
from tests.support import FULL, RECENT, SINCE, WINDOW

CONTRACT = Contract(
    "moves",
    ("id",),
    {"full": FULL, "daily": WINDOW, "recent": RECENT, "since": SINCE},
    window_column="day",
    types={"id": "int", "day": "date"},
)
START = datetime(2026, 1, 1, tzinfo=UTC)


@st.composite
def manifests(draw: st.DrawFn) -> Manifest:
    shape = draw(st.sampled_from(tuple(CONTRACT.shapes)))
    produced = START + timedelta(minutes=draw(st.integers(0, 120_000)))
    window: Window | None = None
    if CONTRACT.shapes[shape].coverage == "window":
        low = START.date() + timedelta(days=draw(st.integers(1, 60)))
        window = low, low + timedelta(days=draw(st.integers(0, 30)))
    since = None
    if not CONTRACT.shapes[shape].complete:
        since = produced - timedelta(hours=draw(st.integers(0, 500)))
    return Manifest("moves", shape, produced, window=window, since=since, rows=0)


@settings(max_examples=200, deadline=None, database=None)
@given(history=st.lists(manifests(), min_size=1, max_size=40))
@example(
    history=[
        Manifest("moves", "full", START),
        Manifest("moves", "daily", START + timedelta(days=1), window=(START.date(), START.date())),
        Manifest("moves", "since", START + timedelta(days=2), since=START),
        Manifest("moves", "full", START),
    ]
)
def test_arbitrary_delivery_sequences_keep_every_ledger_invariant(history: list[Manifest]) -> None:
    ledger = Ledger(CONTRACT)
    for manifest in history:
        before = ledger.to_json()
        try:
            after = ledger.apply(manifest)
        except RejectedDelivery:
            assert ledger.to_json() == before
            continue
        coverage = Region(*manifest.window) if manifest.window else Region()
        assert after.regions[0].low is None and after.regions[-1].high is None
        assert all(a.touches(b) for a, b in zip(after.regions, after.regions[1:], strict=False))
        for region in after.regions:
            if region.complete_as_of is None:
                assert region.deletions_as_of is None
                continue
            assert region.deletions_as_of is not None and region.deletions_as_of <= region.complete_as_of
            if region.intersection(coverage) is not None:
                assert region.complete_as_of == manifest.produced_at
                if CONTRACT.shapes[manifest.shape].complete:
                    assert region.deletions_as_of == manifest.produced_at
        for new in after.regions:
            for old in ledger.regions:
                if new.intersection(old) is None:
                    continue
                if new.intersection(coverage) is None:
                    before = (old.complete_as_of, old.deletions_as_of)
                    assert (new.complete_as_of, new.deletions_as_of) == before
                elif old.complete_as_of is not None:
                    assert new.complete_as_of is not None and new.complete_as_of >= old.complete_as_of
                    assert new.deletions_as_of is not None and old.deletions_as_of is not None
                    assert new.deletions_as_of >= old.deletions_as_of
        ledger = after
