"""Exhaust the finite source and delivery products, rather than sample traces.

The core uses two instants (October 2 and 3), three immutable window values,
zero to two distinct keys, and every delivery word of length at most three.
Rejected steps are stutters: after checking their receipt and unchanged state,
we erase them before exploring suffixes. Every erased word has a shorter word
with the same rows, evidence and accepted receipt chain. Rejected receipts are
ignored by from_receipts. This explores all words, including repeated refusals,
without repeatedly walking identical suffixes.

Caches share only identical accounting inputs or identical row/oracle states;
no source history, accepted prefix or delivery choice is sampled or discarded.
The larger Hypothesis check also covers six instants and longer histories.
"""

from collections import Counter
from copy import copy
from dataclasses import dataclass
from datetime import timedelta
from functools import cache
from itertools import combinations_with_replacement, product

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dogana import (
    ApplyPlan,
    Check,
    Checkpoint,
    Completeness,
    Contract,
    Coverage,
    Deletions,
    Gate,
    Ledger,
    RejectedDelivery,
    Shape,
)
from tests.memory_store import RowSource, apply_rows, check_rows, split_rows
from tests.support import at

INSTANTS = (2, 3)
VALUES = (1, 2, 3)
WINDOWS = tuple((a, b) for a in VALUES for b in VALUES if a <= b)
NOW = at(6)
DEPTH = 3


@dataclass(frozen=True)
class Life:
    """One key, initially created, optionally revised once, then physically deleted."""

    window: int
    created: int
    updated: int
    deleted: int | None

    def live(self, instant):
        return self.created <= instant and (self.deleted is None or instant < self.deleted)

    def row(self, key, instant, *, sequence=False, keyless=False):
        revision = self.updated if self.updated <= instant else self.created
        row = {"bucket": self.window, "updated_at": revision if sequence else at(revision)}
        if not keyless:
            row["id"] = key
        return row


def source_rows(history, instant, *, sequence=False, keyless=False):
    return [
        life.row(key, instant, sequence=sequence, keyless=keyless)
        for key, life in enumerate(history, 1)
        if life.live(instant)
    ]


def lives(instants=INSTANTS):
    return tuple(
        Life(window, created, updated, deleted)
        for window, created, updated, deleted in product(VALUES, instants, instants, (*instants, None))
        if created <= updated and (deleted is None or updated <= deleted)
    )


def histories():
    variants = lives()
    return ((), *((life,) for life in variants), *product(variants, repeat=2))


def make_contract(kind):
    shapes = {}
    checkpoints = {
        "append": (Checkpoint.TIMESTAMP,),
        Checkpoint.GENERATION: (Checkpoint.TIMESTAMP, Checkpoint.GENERATION),
        Checkpoint.SEQUENCE: (Checkpoint.SEQUENCE,),
    }[kind]
    for coverage in (Coverage.ALL, Coverage.WINDOW):
        name = "full" if coverage is Coverage.ALL else "window"
        shapes[name] = Shape(
            coverage=coverage,
            completeness=Completeness.COMPLETE,
            deletions=Deletions.ABSENCE,
            checkpoint=Checkpoint.NONE,
        )
        for checkpoint in checkpoints:
            if coverage is Coverage.WINDOW and checkpoint != Checkpoint.TIMESTAMP:
                continue
            for deletions in (Deletions.NONE, Deletions.TOMBSTONE):
                if kind == "append" and (coverage is Coverage.WINDOW or deletions is Deletions.TOMBSTONE):
                    continue
                shapes[f"{coverage}_{checkpoint}_{deletions}"] = Shape(
                    coverage=coverage,
                    completeness=Completeness.CHANGED_ONLY,
                    deletions=deletions,
                    checkpoint=checkpoint,
                )
    return Contract(
        "events",
        () if kind == "append" else ("id",),
        shapes,
        window_column="bucket",
        cursor_column="updated_at",
        deleted_column=None if kind == "append" else "deleted",
        types={
            "bucket": "int",
            "updated_at": "int" if kind is Checkpoint.SEQUENCE else "datetime",
            **({} if kind == "append" else {"id": "int", "deleted": "bool"}),
        },
        start_at=at(1) if kind == "append" else None,
    )


@dataclass(frozen=True)
class Read:
    shape: str
    instant: int
    window: tuple[int, int] | None = None
    since: int | None = None
    issue: bool = False


def reads(contract, instants=INSTANTS):
    result = []
    for name, shape in contract.shapes.items():
        for instant in instants:
            for window in WINDOWS if shape.coverage is Coverage.WINDOW else (None,):
                if shape.complete:
                    for issue in (False, True) if window is None and contract.key else (False,):
                        result.append(Read(name, instant, window, issue=issue))
                else:
                    checkpoints = (1, *instants) if not contract.key else instants
                    for since in checkpoints:
                        if since <= instant:
                            result.append(Read(name, instant, window, since, shape.checkpoint != "timestamp"))
    return tuple(result)


def freeze_rows(rows):
    """Keep duplicates, including equal keyless events, while ignoring row order."""
    return tuple(sorted(tuple(sorted(row.items())) for row in rows))


@cache
def honest_file(history, read, kind):
    contract = CONTRACTS[kind]
    shape = contract.shapes[read.shape]
    selected = []
    for key, life in enumerate(history, 1):
        if read.window is not None and not read.window[0] <= life.window <= read.window[1]:
            continue
        if life.live(read.instant):
            row = life.row(key, read.instant, sequence=kind is Checkpoint.SEQUENCE, keyless=kind == "append")
            revision = life.updated if life.updated <= read.instant else life.created
            if shape.complete or read.since < revision <= read.instant:
                selected.append(row)
        elif (
            not shape.complete
            and shape.deletions is Deletions.TOMBSTONE
            and life.deleted is not None
            and read.since < life.deleted <= read.instant
        ):
            selected.append({"id": key, "deleted": True})
    if any(row.get("deleted") is True for row in selected):
        selected = [row if row.get("deleted") is True else {**row, "deleted": False} for row in selected]
    rows = RowSource(selected)
    live, deleted, unmarked = split_rows(contract, rows)
    assert not unmarked
    since = read.since
    if since is not None:
        since = (
            f"g{since}"
            if shape.checkpoint is Checkpoint.GENERATION
            else (since if shape.checkpoint is Checkpoint.SEQUENCE else at(since))
        )
    generation = None
    if read.issue:
        generation = read.instant if kind is Checkpoint.SEQUENCE else f"g{read.instant}"
    manifest = contract.manifest(
        read.shape,
        at(read.instant),
        window=read.window,
        since=since,
        generation=generation,
        rows=len(live),
        tombstones=len(deleted),
    )
    return manifest, freeze_rows(rows), freeze_rows(live), tuple(deleted)


CONTRACTS = {kind: make_contract(kind) for kind in (Checkpoint.GENERATION, Checkpoint.SEQUENCE, "append")}


@cache
def checked_apply(kind, manifest, delivered, incoming, deleted, published):
    contract = CONTRACTS[kind]
    checks = check_rows(contract, manifest, RowSource(map(dict, delivered)), list(map(dict, published)))
    assert all(check.passed for check in checks), checks
    result = freeze_rows(
        apply_rows(
            list(map(dict, published)),
            list(map(dict, incoming)),
            ApplyPlan.build(contract, manifest),
            deleted,
        )
    )
    if manifest.window is not None:
        low, high = manifest.window

        def outside(rows):
            return tuple(row for row in rows if not low <= dict(row)["bucket"] <= high)

        assert outside(result) == outside(published), "P4: changed rows outside coverage"
    return result


@pytest.fixture(autouse=True)
def release_model_caches():
    yield
    honest_file.cache_clear()
    checked_apply.cache_clear()


class Model:
    def __init__(self, kind):
        self.kind = kind
        self.contract = CONTRACTS[kind]
        self.decisions = {}
        self.properties = set()
        self.regions = {}
        self.identities = {}
        self.steps = Counter()

    def decision(self, before, manifest, receipts):
        key = before.publication_id, manifest
        if key in self.decisions:
            return self.decisions[key]
        snapshot = copy(before)
        try:
            after = before.apply(manifest)
        except RejectedDelivery:
            after = before
            accepted = False
        else:
            accepted = True
        assert before == snapshot, "P6: Ledger.apply mutated its input"
        # Row checks are independently verified against actual files in walk().
        checks = (
            tuple(Check(r.name, True) for r in Gate.requirements(self.contract, manifest)) if accepted else ()
        )
        receipt = Gate.evaluate(before, manifest, checks, now=NOW)
        assert receipt.accepted == accepted, "Gate and Ledger.apply disagree"
        if not accepted:
            assert before.follow(receipt) is before, "P7: a refused receipt changed reconstruction"
            assert receipt.publication_id == before.publication_id
            if before.publication_id not in self.identities:
                self.identities[before.publication_id] = before.identity
            assert receipt.ledger_identity == self.identities[before.publication_id]
        else:
            assert Ledger.from_receipts(self.contract, (*receipts, receipt)) == after, (
                "P7: receipts reconstruct another ledger"
            )
            assert receipt.transition.plan == ApplyPlan.build(self.contract, manifest)
            partition_and_coverage(before.regions, after.regions, manifest.window)
        self.decisions[key] = after, receipt
        return after, receipt

    def walk(self, history, choices, depth=DEPTH, ledger=None, published=(), receipts=(), trace=()):
        ledger = Ledger(self.contract) if ledger is None else ledger
        for read in choices:
            manifest, delivered, incoming, deleted = honest_file(history, read, self.kind)
            try:
                after, receipt = self.decision(ledger, manifest, receipts)
                self.steps["attempted"] += 1
                if not receipt.accepted:
                    self.steps["rejected"] += 1
                    # The execution path must leave rows and evidence untouched.
                    assert after == ledger
                    continue
                self.steps["accepted"] += 1
                rows = checked_apply(self.kind, manifest, delivered, incoming, deleted, published)
                if after.publication_id not in self.regions:
                    self.regions[after.publication_id] = after.regions
                state = history, self.regions[after.publication_id], rows
                if state not in self.properties:
                    oracle_properties(history, rows, after, self.kind)
                    self.properties.add(state)
                if depth > 1:
                    # Refusals have no effect, so only accepted steps need another suffix walk.
                    self.walk(history, choices, depth - 1, after, rows, (*receipts, receipt), (*trace, read))
            except AssertionError as exc:
                raise AssertionError(
                    f"counterexample: kind={self.kind}, history={history!r}, trace={(*trace, read)!r}, "
                    f"regions={ledger.regions!r}, published={published!r}; "
                    f"manifest={manifest!r}; {exc}"
                ) from exc

    def run(self, history, sequence):
        ledger, published, receipts = Ledger(self.contract), (), ()
        for index, read in enumerate(sequence):
            manifest, delivered, incoming, deleted = honest_file(history, read, self.kind)
            try:
                after, receipt = self.decision(ledger, manifest, receipts)
                if receipt.accepted:
                    published = checked_apply(self.kind, manifest, delivered, incoming, deleted, published)
                    oracle_properties(history, published, after, self.kind)
                ledger, receipts = after, (*receipts, receipt)
            except AssertionError as exc:
                raise AssertionError(
                    f"counterexample: kind={self.kind}, history={history!r}, "
                    f"trace={sequence[: index + 1]!r}, "
                    f"regions={ledger.regions!r}, published={published!r}; manifest={manifest!r}; {exc}"
                ) from exc
        assert Ledger.from_receipts(self.contract, receipts) == ledger, "P7: mixed receipt chain differs"
        return ledger, published


def contains(region, value):
    return (region.low is None or value > region.low or value == region.low and not region.low_open) and (
        region.high is None or value < region.high or value == region.high and not region.high_open
    )


def evidence_at(regions, value):
    matched = [r for r in regions if contains(r, value)]
    assert len(matched) == 1, f"P3: {value} belongs to {matched}"
    return matched[0].complete_as_of, matched[0].deletions_as_of


def partition_and_coverage(before, after, window):
    # Every cut comes from an integer window endpoint. Probe each finite segment
    # and both unbounded tails, independently of Region.intersection/_bounds.
    endpoints = {v for r in (*before, *after) for v in (r.low, r.high) if v is not None}
    if window is not None:
        endpoints.update(window)
    probes = {0, 4}
    probes.update(v + delta for v in endpoints for delta in (-1, 0, 1))
    for value in probes:
        dates = evidence_at(after, value)
        assert (dates[0] is None) == (dates[1] is None), "P3: half-known region"
        assert dates[0] is None or dates[1] <= dates[0], "P3: d > c"
        if window is not None and not window[0] <= value <= window[1]:
            assert dates == evidence_at(before, value), "P4: changed evidence outside coverage"


def oracle_properties(history, published, ledger, kind):
    rows = list(map(dict, published))
    for region in ledger.regions:
        c, d = region.complete_as_of, region.deletions_as_of
        if c is None:
            continue
        retained = [row for row in rows if contains(region, row["bucket"])]
        required = [
            row
            for row in source_rows(
                history, c.day, sequence=kind is Checkpoint.SEQUENCE, keyless=kind == "append"
            )
            if contains(region, row["bucket"])
        ]
        if ledger.contract.key:
            assert set(freeze_rows(required)) <= set(freeze_rows(retained)), (
                "P1",
                region,
                required,
                retained,
            )
            for row in retained:
                life = history[row["id"] - 1]
                revision = row["updated_at"] if kind is Checkpoint.SEQUENCE else row["updated_at"].day
                assert revision in (life.created, life.updated), ("P2", region, row, life)
                end = life.deleted
                if revision < life.updated:
                    end = life.updated if end is None else min(end, life.updated)
                assert revision <= c.day and (end is None or end > d.day), (
                    "P2",
                    region,
                    row,
                    life,
                    (revision, end),
                )
        else:
            assert not (Counter(freeze_rows(required)) - Counter(freeze_rows(retained))), ("P1", region)
            # Append sources never revise or delete. Each retained row must have existed by c.
            possible = source_rows(history, c.day, keyless=True)
            assert not (Counter(freeze_rows(retained)) - Counter(freeze_rows(possible))), ("P2", region)
    exact = ledger.claims(NOW, limit=timedelta.max).exact_as_of
    if exact is not None:
        expected = freeze_rows(
            source_rows(history, exact.day, sequence=kind is Checkpoint.SEQUENCE, keyless=kind == "append")
        )
        assert published == expected, ("P5", exact, published, expected)


@pytest.mark.parametrize("kind", (Checkpoint.GENERATION, Checkpoint.SEQUENCE))
def test_every_bounded_keyed_delivery_sequence_preserves_the_source_guarantee(kind):
    model = Model(kind)
    choices = reads(model.contract)
    sources = histories()
    for history in sources:
        model.walk(history, choices)
    print(
        f"{kind}: histories={len(sources)}, choices={len(choices)}, "
        f"steps={model.steps}, decisions={len(model.decisions)}"
    )
    assert len(sources) == 463
    assert len(choices) == (64 if kind is Checkpoint.GENERATION else 22)
    assert model.steps["accepted"] and model.steps["rejected"]


def test_every_bounded_append_sequence_preserves_events_including_duplicates():
    model = Model("append")
    events = tuple(Life(w, t, t, None) for w, t in product(VALUES, INSTANTS))
    sources = ((), *((e,) for e in events), *combinations_with_replacement(events, 2))
    choices = reads(model.contract)
    for history in sources:
        model.walk(history, choices)
    print(
        f"append: histories={len(sources)}, choices={len(choices)}, "
        f"steps={model.steps}, decisions={len(model.decisions)}"
    )
    assert len(sources) == 28
    assert len(choices) == 19
    assert model.steps["accepted"] and model.steps["rejected"]


def test_every_three_instant_history_preserves_both_dates_when_tombstones_leave_or_close_a_gap():
    variants = lives((2, 3, 4))
    sources = ((), *((life,) for life in variants), *product(variants, repeat=2))
    model = Model(Checkpoint.GENERATION)
    for history in sources:
        for since in (2, 3, 4):
            model.run(
                history,
                (
                    Read("full", 2),
                    Read("all_timestamp_none", 3, since=2),
                    Read("all_timestamp_tombstone", 4, since=since),
                ),
            )
    assert len(sources) == 2353


def test_an_honest_keyless_window_replacement_matches_no_published_keys():
    # Protocol, Plans, checks and receipts: "Keyless replacement matches no keys."
    history = (Life(1, 2, 2, None), Life(2, 2, 2, None))
    ledger, published = Model("append").run(history, (Read("full", 2), Read("window", 3, (1, 1))))
    assert ledger.claims(NOW, limit=timedelta.max).exact_as_of is None
    assert published == freeze_rows(source_rows(history, 3, keyless=True))


@st.composite
def larger_source(draw):
    result = []
    for _ in range(draw(st.integers(0, 3))):
        created = draw(st.integers(2, 6))
        deleted = draw(st.one_of(st.none(), st.integers(created, 6)))
        updated = draw(st.integers(created, deleted or 6))
        result.append(Life(draw(st.integers(1, 3)), created, updated, deleted))
    return tuple(result)


@settings(max_examples=60, deadline=None)
@given(
    history=larger_source(),
    kind=st.sampled_from((Checkpoint.GENERATION, Checkpoint.SEQUENCE)),
    data=st.data(),
)
def test_longer_delivery_sequences_preserve_the_same_source_guarantee(history, kind, data):
    choices = reads(CONTRACTS[kind], tuple(range(1, 7)))
    sequence = tuple(
        data.draw(st.sampled_from(tuple(read for read in choices if read.instant == instant)))
        for instant in range(2, 7)
    )
    model = Model(kind)
    model.run(history, (Read("full", 1, issue=True), *sequence))
