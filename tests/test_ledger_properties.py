"""Evidence laws over accepted deliveries and integer window boundaries."""

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from dogana import (
    Claims,
    Contract,
    Deletions,
    Ledger,
    Manifest,
    Outcome,
    Region,
    RejectedDelivery,
    deliver,
)
from tests.memory_store import MemoryStore
from tests.support import FULL, RECENT, SINCE, WINDOW

CONTRACT = Contract(
    "items",
    ("id",),
    {
        "full": FULL,
        "window": replace(WINDOW, window_size=None),
        "patch": RECENT,
        "deletes": replace(RECENT, deletions=Deletions.TOMBSTONE),
        "since": SINCE,
        "global_deletes": replace(SINCE, deletions=Deletions.TOMBSTONE),
    },
    window_column="bucket",
    types={"id": "int", "bucket": "int", "deleted": "bool"},
    deleted_column="deleted",
)
LAWS = settings(max_examples=80, deadline=None, database=None)
Evidence = tuple[datetime, datetime] | None
History = tuple[Manifest, ...]
Partition = tuple[tuple[int | None, Evidence], ...]


def at(tick: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=tick)


def evidence(region: Region) -> Evidence:
    if region.complete_as_of is None:
        assert region.deletions_as_of is None
        return None
    assert region.deletions_as_of is not None
    return region.complete_as_of, region.deletions_as_of


def advance(old: Evidence, manifest: Manifest) -> Evidence:
    t = manifest.produced_at
    if manifest.shape in ("full", "window"):
        return t, t
    assert old is not None and isinstance(manifest.since, datetime)
    valid = t if "deletes" in manifest.shape and manifest.since <= old[1] else old[1]
    return t, valid


def contains(manifest: Manifest, point: int) -> bool:
    if manifest.window is None:
        return True
    low, high = manifest.window
    assert isinstance(low, int) and isinstance(high, int)
    return low <= point <= high


def boundary_oracle(history: History) -> Partition:
    """Refine once by all delivered endpoints, then replay each cell independently."""
    cuts: set[int] = set()
    for manifest in history:
        if manifest.window is not None:
            low, high = manifest.window
            assert isinstance(low, int) and isinstance(high, int)
            cuts.update((low, high + 1))
    result: list[tuple[int | None, Evidence]] = []
    for cut in [None, *sorted(cuts)]:
        point = min(cuts, default=0) - 1 if cut is None else cut
        current: Evidence = None
        for manifest in history:
            if contains(manifest, point):
                current = advance(current, manifest)
        if not result or result[-1][1] != current:
            result.append((cut, current))
    return tuple(result)


def partition(ledger: Ledger) -> Partition:
    """Normalize public integer endpoints without merging the returned regions."""
    result: list[tuple[int | None, Evidence]] = []
    previous_end: int | None = None
    for index, region in enumerate(ledger.regions):
        assert region.low is None or isinstance(region.low, int)
        assert region.high is None or isinstance(region.high, int)
        low = None if region.low is None else region.low + int(region.low_open)
        high = None if region.high is None else region.high + int(not region.high_open)
        assert low == previous_end
        assert index == 0 or low is not None
        assert high is None or low is None or low < high
        result.append((low, evidence(region)))
        previous_end = high
    assert previous_end is None
    return tuple(result)


@st.composite
def accepted_histories(draw: st.DrawFn) -> History:
    history = [
        Manifest(CONTRACT.table, "window", at(0), window=(-3, -3)),
        Manifest(CONTRACT.table, "window", at(1), window=(3, 3)),
    ]
    known: dict[int, Evidence] = {point: None for point in range(-7, 8)}
    known[-3], known[3] = (at(0), at(0)), (at(1), at(1))
    commands = draw(
        st.lists(
            st.tuples(
                st.sampled_from(tuple(CONTRACT.shapes)),
                st.integers(-6, 6),
                st.integers(-6, 6),
                st.integers(1, 8),
                st.booleans(),
                st.integers(0, 3),
            ),
            max_size=18,
        )
    )
    tick = 1
    for name, left, right, increment, cover_deletions, lookback in commands:
        tick += increment
        window = None if name in ("full", "since", "global_deletes") else (min(left, right), max(left, right))
        points = list(known) if window is None else list(range(window[0], window[1] + 1))
        old = [known[point] for point in points]
        since = None
        if name not in ("full", "window"):
            if any(value is None for value in old):
                name = "full" if window is None else "window"
            else:
                coordinate = 1 if cover_deletions else 0
                since = min(value[coordinate] for value in old if value is not None)
                since -= timedelta(minutes=lookback)
        manifest = Manifest(CONTRACT.table, name, at(tick), window=window, since=since)
        history.append(manifest)
        for point in points:
            known[point] = advance(known[point], manifest)
    return tuple(history)


def fold(ledger: Ledger, manifests: Iterable[Manifest]) -> Ledger:
    for manifest in manifests:
        ledger = ledger.apply(manifest)
    return ledger


@LAWS
@given(history=accepted_histories(), split=st.integers(0, 20))
def test_ordered_delivery_folds_are_deterministic(history: History, split: int) -> None:
    left = right = Ledger(CONTRACT)
    for manifest in history:
        left = left.apply(manifest)
        right = right.apply(Manifest.from_json(manifest.to_json()))
        assert left == right
        assert left.publication_id == right.publication_id
    assert fold(fold(Ledger(CONTRACT), history[:split]), history[split:]) == left


@LAWS
@given(history=accepted_histories())
def test_accepted_windows_match_an_independent_boundary_oracle(history: History) -> None:
    ledger = Ledger(CONTRACT)
    assert partition(ledger) == boundary_oracle(())
    for length, manifest in enumerate(history, 1):
        ledger = ledger.apply(manifest)
        assert partition(ledger) == boundary_oracle(history[:length])
        assert Ledger.from_json(ledger.to_json()) == ledger
        if length == 2:
            assert ledger.claims(at(1), window=(-2, 2)).complete_as_of is None


def below(left: Evidence, right: Evidence) -> bool:
    return left is None or right is not None and left[0] <= right[0] and left[1] <= right[1]


@LAWS
@given(history=accepted_histories())
def test_accepted_deliveries_raise_covered_evidence_and_preserve_every_other_cell(history: History) -> None:
    ledger = Ledger(CONTRACT)
    for manifest in history:
        after = ledger.apply(manifest)
        cuts = {cut for cut, _ in (*partition(ledger), *partition(after)) if cut is not None}
        for point in {min(cuts, default=0) - 1, *cuts}:
            before_claim = ledger.claims(manifest.produced_at, window=(point, point))
            after_claim = after.claims(manifest.produced_at, window=(point, point))
            old, new = evidence(before_claim.regions[0].region), evidence(after_claim.regions[0].region)
            if contains(manifest, point):
                assert below(old, new)
                assert new is not None and new[0] == manifest.produced_at
                if old is not None:
                    assert new[0] > old[0]
            else:
                assert new == old
        ledger = after


@pytest.mark.parametrize("left_shape", ("window", "patch", "deletes"))
@pytest.mark.parametrize("right_shape", ("window", "patch", "deletes"))
@LAWS
@given(
    low=st.integers(-100, 100),
    width=st.integers(0, 10),
    gap=st.integers(1, 5),
    right_width=st.integers(0, 10),
    left_time=st.integers(2, 50),
    right_time=st.integers(2, 50),
    left_since=st.integers(0, 1),
    right_since=st.integers(0, 1),
)
def test_disjoint_complete_and_timestamp_windows_commute_only_on_evidence(
    left_shape: str,
    right_shape: str,
    low: int,
    width: int,
    gap: int,
    right_width: int,
    left_time: int,
    right_time: int,
    left_since: int,
    right_since: int,
) -> None:
    base = Ledger(CONTRACT).apply(Manifest(CONTRACT.table, "full", at(0), generation="base"))
    base = base.apply(Manifest(CONTRACT.table, "since", at(1), since=at(0)))
    right_low = low + width + gap
    left = Manifest(
        CONTRACT.table,
        left_shape,
        at(left_time),
        window=(low, low + width),
        since=None if left_shape == "window" else at(left_since),
    )
    right = Manifest(
        CONTRACT.table,
        right_shape,
        at(right_time),
        window=(right_low, right_low + right_width),
        since=None if right_shape == "window" else at(right_since),
    )
    forwards, backwards = base.apply(left).apply(right), base.apply(right).apply(left)
    assert partition(forwards) == partition(backwards)
    assert forwards.generation == backwards.generation == "base"
    assert forwards.publication_id != backwards.publication_id


@LAWS
@given(
    snapshots=st.lists(st.lists(st.integers(-10, 10), unique=True, max_size=5), min_size=2, max_size=6),
    replay_index=st.integers(0, 20),
    repeats=st.integers(1, 4),
)
def test_store_replay_preserves_the_current_publication_and_original_receipt(
    snapshots: list[list[int]],
    replay_index: int,
    repeats: int,
) -> None:
    store = MemoryStore()
    for tick, keys in enumerate(snapshots):
        rows = [{"id": key, "bucket": key} for key in keys]
        manifest = Manifest(
            CONTRACT.table,
            "full",
            at(tick),
            rows=len(rows),
            schema_hash=CONTRACT.schema_hash,
        )
        assert deliver(store, CONTRACT, rows, manifest, at(tick + 1)).accepted
    receipts = store.receipts(CONTRACT.table)
    index = replay_index % (len(receipts) - 1)
    original = receipts[index]
    assert original.outcome is Outcome.ACCEPTED and not original.replayed
    before = store.read(CONTRACT.table), store.load_ledger(CONTRACT), store.publication_stamp(CONTRACT.table)
    for _ in range(repeats):
        replay = deliver(store, CONTRACT, object(), original.manifest, at(100))
        assert replay.accepted and replay.replayed
        assert replay.outcome is Outcome.REPLAYED
        assert replay.to_dict() == {**original.to_dict(), "outcome": "replayed"}
        assert replay.identity == original.identity
        assert replay.received_at == original.received_at
        assert store.receipts(CONTRACT.table) == receipts
        current = (
            store.read(CONTRACT.table),
            store.load_ledger(CONTRACT),
            store.publication_stamp(CONTRACT.table),
        )
        assert current == before
    assert Ledger.from_receipts(CONTRACT, receipts) == store.load_ledger(CONTRACT)
    with pytest.raises(RejectedDelivery):
        Ledger.from_receipts(CONTRACT, receipts[: index + 1]).apply(original.manifest)


@st.composite
def evidence_regions(draw: st.DrawFn) -> Region:
    if draw(st.booleans()):
        return Region()
    valid, complete = sorted(draw(st.tuples(st.integers(-100, 100), st.integers(-100, 100))))
    return Region(complete_as_of=at(complete), deletions_as_of=at(valid))


def join(left: Region, right: Region) -> Region:
    a, b = evidence(left), evidence(right)
    if a is None:
        return right
    if b is None:
        return left
    return Region(complete_as_of=max(a[0], b[0]), deletions_as_of=max(a[1], b[1]))


def meet(left: Region, right: Region) -> Region:
    claims = Claims.combine(*(Ledger(CONTRACT, (region,)).claims(at(200)) for region in (left, right)))
    return Region(complete_as_of=claims.complete_as_of, deletions_as_of=claims.deletions_as_of)


@LAWS
@given(left=evidence_regions(), right=evidence_regions(), third=evidence_regions())
@example(
    left=Region(complete_as_of=at(10), deletions_as_of=at(4)),
    right=Region(complete_as_of=at(8), deletions_as_of=at(7)),
    third=Region(),
)
def test_region_evidence_is_a_coordinate_lattice_with_unknown_bottom(
    left: Region,
    right: Region,
    third: Region,
) -> None:
    assert join(left, Region()) == left
    assert meet(left, Region()) == Region()
    assert join(left, left) == meet(left, left) == left
    assert join(left, right) == join(right, left)
    assert meet(left, right) == meet(right, left)
    assert join(join(left, right), third) == join(left, join(right, third))
    assert meet(meet(left, right), third) == meet(left, meet(right, third))
    assert meet(left, join(left, right)) == join(left, meet(left, right)) == left
    assert below(evidence(left), evidence(join(left, right)))
    assert below(evidence(meet(left, right)), evidence(left))
    assert Region.from_json(join(left, right).to_json()) == join(left, right)
