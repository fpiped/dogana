"""Ordered boundary cuts with evidence to their right."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from functools import total_ordering
from hashlib import sha256
from typing import TYPE_CHECKING, TypeAlias, assert_never

from .contract import (
    LOGICAL_TYPES,
    AppendInterval,
    Contract,
    Deletions,
    FullDelivery,
    GenerationChanges,
    Manifest,
    ParsedDelivery,
    Scalar,
    SequenceChanges,
    TimestampChanges,
    Window,
    WindowCoverage,
    WindowDelivery,
    bounds,
    coverage_window,
    identifier,
    scalar_compare,
)
from .errors import InconsistentPublication as InconsistentPublication
from .errors import InvalidContract, LedgerAhead, StampAhead
from .errors import RejectedDelivery as RejectedDelivery
from .wire import Wire, utc

if TYPE_CHECKING:
    from .gate import Receipt


class State(StrEnum):
    UNKNOWN = "unknown"
    COMPLETE = "complete"
    PATCHED = "patched"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Unknown:
    pass


@dataclass(frozen=True)
class Known:
    complete_as_of: datetime
    deletions_as_of: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "complete_as_of", utc(self.complete_as_of))
        object.__setattr__(self, "deletions_as_of", utc(self.deletions_as_of))
        if self.deletions_as_of > self.complete_as_of:
            raise InvalidContract("deletion evidence cannot be newer than completeness")


Evidence: TypeAlias = Unknown | Known


def _successor(value: Scalar) -> Scalar | None:
    if isinstance(value, str):
        return value + "\x00"
    if isinstance(value, int):
        return value + 1
    try:
        return value + (timedelta(microseconds=1) if isinstance(value, datetime) else timedelta(days=1))
    except OverflowError:
        return None


def _minimum(value: Scalar) -> Scalar | None:
    if isinstance(value, str):
        return ""
    if isinstance(value, datetime):
        return datetime.min.replace(tzinfo=UTC)
    return date.min if isinstance(value, date) else None


@total_ordering
@dataclass(frozen=True)
class _Cut:
    """A position before a scalar, or an end of the domain (rank -1 or 1)."""

    rank: int
    value: Scalar | None = None

    def __lt__(self, other: _Cut) -> bool:
        if self.rank != other.rank:
            return self.rank < other.rank
        if self.value is None or other.value is None:
            return False
        return scalar_compare(self.value, other.value) < 0


_START, _END = _Cut(-1), _Cut(1)


def _cut(value: Scalar | None, *, after: bool) -> _Cut:
    if value is None:
        return _END if after else _START
    if after:
        value = _successor(value)
        if value is None:
            return _END
    return _START if value == _minimum(value) else _Cut(0, value)


@dataclass(frozen=True)
class Region(Wire):
    low: Scalar | None = None
    high: Scalar | None = None
    complete_as_of: datetime | None = None
    deletions_as_of: datetime | None = None
    low_open: bool = False
    high_open: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.low is not None and self.high is not None:
            bounds((self.low, self.high))
        if self.low is None and self.low_open or self.high is None and self.high_open:
            raise InvalidContract("unbounded endpoints cannot be open")
        if self._bounds[0] >= self._bounds[1]:
            raise InvalidContract("region is empty in its scalar domain")
        _ = self.evidence

    @property
    def _bounds(self) -> tuple[_Cut, _Cut]:
        return _cut(self.low, after=self.low_open), _cut(self.high, after=not self.high_open)

    @property
    def evidence(self) -> Evidence:
        if self.complete_as_of is None and self.deletions_as_of is None:
            return Unknown()
        if self.complete_as_of is None or self.deletions_as_of is None:
            raise InvalidContract("region dates must be present or absent together")
        return Known(self.complete_as_of, self.deletions_as_of)

    def state(self, now: datetime, *, limit: timedelta) -> State:
        """The region's state at the reader's time; a region proven longer ago than `limit` is expired."""
        now = utc(now)
        if not isinstance(limit, timedelta) or limit < timedelta(0):
            raise InvalidContract("the age limit must be a nonnegative duration")
        match self.evidence:
            case Unknown():
                return State.UNKNOWN
            case Known(complete, deletions):
                if now - complete > limit:
                    return State.EXPIRED
                return State.COMPLETE if complete == deletions else State.PATCHED
            case _ as unreachable:
                assert_never(unreachable)

    def intersection(self, other: Region) -> Region | None:
        low, high = self._bounds
        other_low, other_high = other._bounds
        low, high = max(low, other_low), min(high, other_high)
        return _region(low, high, self.evidence) if low < high else None

    def touches(self, other: Region) -> bool:
        return self._bounds[1] == other._bounds[0]


_Steps: TypeAlias = tuple[tuple[_Cut, Evidence], ...]
_INITIAL_REGIONS = (Region(),)


def _region(low: _Cut, high: _Cut, evidence: Evidence) -> Region:
    complete, deletions = (
        (evidence.complete_as_of, evidence.deletions_as_of) if isinstance(evidence, Known) else (None, None)
    )
    low_value, low_after = _endpoint(low)
    high_value, high_after = _endpoint(high)
    return Region(
        low_value,
        high_value,
        complete,
        deletions,
        low_open=low_after,
        high_open=high.rank == 0 and not high_after,
    )


def _endpoint(cut: _Cut) -> tuple[Scalar | None, bool]:
    value = cut.value
    # A successor cut can fall between integers representable on the wire.
    if isinstance(value, int) and float(value) != value:
        return value - 1, True
    return value, False


def _steps(regions: tuple[Region, ...]) -> _Steps:
    result: list[tuple[_Cut, Evidence]] = []
    end = _START
    for region in regions:
        low, high = region._bounds
        if low != end:
            raise InvalidContract("ledger regions must partition the entire ordered domain")
        evidence = region.evidence
        if not result or result[-1][1] != evidence:
            result.append((low, evidence))
        end = high
    if end != _END:
        raise InvalidContract("ledger regions must partition the entire ordered domain")
    return tuple(result)


def _segments(steps: _Steps, low: _Cut = _START, high: _Cut = _END) -> Iterator[tuple[_Cut, _Cut, Evidence]]:
    ends = [cut for cut, _ in steps[1:]] + [_END]
    for (start, evidence), end in zip(steps, ends, strict=True):
        start, end = max(start, low), min(end, high)
        if start < end:
            yield start, end, evidence


def _window_cuts(window: Window | None) -> tuple[_Cut, _Cut]:
    return (_START, _END) if window is None else (_cut(window[0], after=False), _cut(window[1], after=True))


def _apply_steps(steps: _Steps, low: _Cut, high: _Cut, delivery: ParsedDelivery) -> _Steps:
    cuts = sorted(({cut for cut, _ in steps} | {low, high}) - {_END})
    result: list[tuple[_Cut, Evidence]] = []
    index = 0
    for cut in cuts:
        while index + 1 < len(steps) and steps[index + 1][0] <= cut:
            index += 1
        evidence = steps[index][1]
        if low <= cut < high:
            evidence = _transition(delivery, evidence)
        if not result or result[-1][1] != evidence:
            result.append((cut, evidence))
    return tuple(result)


@dataclass(frozen=True)
class RegionClaim(Wire):
    table: str
    region: Region
    state: State

    def __post_init__(self) -> None:
        super().__post_init__()
        identifier(self.table)


@dataclass(frozen=True)
class Claims(Wire):
    regions: tuple[RegionClaim, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.regions:
            raise InvalidContract("claims need at least one region")

    @property
    def state(self) -> State:
        return next(
            s
            for s in (State.UNKNOWN, State.EXPIRED, State.PATCHED, State.COMPLETE)
            if any(r.state is s for r in self.regions)
        )

    @property
    def complete_as_of(self) -> datetime | None:
        dates = [r.region.complete_as_of for r in self.regions]
        return None if any(t is None for t in dates) else min(t for t in dates if t is not None)

    @property
    def deletions_as_of(self) -> datetime | None:
        dates = [r.region.deletions_as_of for r in self.regions]
        return None if any(t is None for t in dates) else min(t for t in dates if t is not None)

    @property
    def exact_as_of(self) -> datetime | None:
        """The one instant at which every region equals the source, if there is one."""
        dates = {(r.region.complete_as_of, r.region.deletions_as_of) for r in self.regions}
        if len(dates) != 1:
            return None
        ((complete, deletions),) = dates
        return complete if complete is not None and complete == deletions else None

    def in_state(self, state: State) -> tuple[RegionClaim, ...]:
        return tuple(r for r in self.regions if r.state is state)


@dataclass(frozen=True)
class _LedgerView(Wire):
    contract: Contract
    regions: tuple[Region, ...] = _INITIAL_REGIONS
    generation: str | int | None = None
    publication_id: str | None = None


@dataclass(frozen=True, init=False)
class Ledger(Wire):
    contract: Contract
    _cuts: _Steps = field(repr=False)
    generation: str | int | None = None
    publication_id: str | None = None

    def __init__(
        self,
        contract: Contract,
        regions: tuple[Region, ...] = _INITIAL_REGIONS,
        generation: str | int | None = None,
        publication_id: str | None = None,
        *,
        _cuts: _Steps | None = None,
    ) -> None:
        view = _LedgerView(contract, regions, generation, publication_id)
        finite = [v for r in view.regions for v in (r.low, r.high) if v is not None]
        if len({type(v) for v in finite}) > 1:
            raise InvalidContract("ledger regions must share one scalar domain")
        object.__setattr__(self, "contract", view.contract)
        object.__setattr__(self, "generation", view.generation)
        object.__setattr__(self, "publication_id", view.publication_id)
        object.__setattr__(
            self,
            "_cuts",
            _cuts if regions is _INITIAL_REGIONS and _cuts is not None else _steps(view.regions),
        )
        finite.extend(cut.value for cut, _ in self._cuts if cut.value is not None)
        if finite:
            self._check_window_type((finite[0], finite[0]))
        for token in (self.generation, self.publication_id):
            if isinstance(token, str) and not token:
                raise InvalidContract("generation and publication identities must be nonempty")

    @classmethod
    def _wire_schema(cls) -> type[Wire]:
        return _LedgerView

    @property
    def regions(self) -> tuple[Region, ...]:
        return tuple(_region(low, high, evidence) for low, high, evidence in _segments(self._cuts))

    def with_contract(self, contract: Contract) -> Ledger:
        if not self.contract.compatible_with(contract):
            raise InvalidContract("contract changes fields bound to the publication history")
        return self if self.contract == contract else replace(self, contract=contract)

    def follow(self, receipt: Receipt) -> Ledger:
        """The ledger an accepted receipt leaves after this one, checked against what the receipt names."""
        if not receipt.accepted:
            return self
        try:
            ledger = self.with_contract(receipt.contract)
            if receipt.base_publication_id != ledger.publication_id:
                raise InconsistentPublication("accepted receipt has a different parent publication")
            after = ledger.apply(receipt.manifest)
        except (InvalidContract, RejectedDelivery) as exc:
            raise InconsistentPublication(f"accepted receipt breaks publication chain: {exc}") from exc
        if after.publication_id != receipt.publication_id or after.identity != receipt.ledger_identity:
            raise InconsistentPublication("accepted receipt breaks publication chain or ledger evidence")
        return after

    @classmethod
    def from_receipts(cls, contract: Contract, receipts: Iterable[Receipt]) -> Ledger:
        ledger = cls(contract)
        for receipt in receipts:
            ledger = ledger.follow(receipt)
        return ledger.with_contract(contract)

    @classmethod
    def reconcile(
        cls,
        contract: Contract,
        receipts: Iterable[Receipt],
        current: Ledger | None,
        stamp: str | None,
        *,
        repair: bool = True,
        retained_head: Receipt | None = None,
    ) -> Ledger:
        """Reconcile with history; a store-verified retained head can certify a consistent read."""
        current = current.with_contract(contract) if current is not None else None
        if (
            not repair
            and stamp is not None
            and current is not None
            and current.publication_id == stamp
            and retained_head is not None
        ):
            if (
                not retained_head.accepted
                or retained_head.publication_id != stamp
                or not retained_head.contract.compatible_with(contract)
                or current.with_contract(retained_head.contract).identity != retained_head.ledger_identity
                or _publication(
                    retained_head.base_publication_id, retained_head.contract, retained_head.manifest
                )
                != stamp
            ):
                raise StampAhead("retained accepted head differs from the current publication")
            return current
        accepted = tuple(receipt for receipt in receipts if receipt.accepted)
        if stamp is None and current is not None and current.publication_id is not None:
            raise LedgerAhead("ledger ahead of missing publication stamp")
        try:
            rebuilt = cls.from_receipts(contract, accepted)
        except InconsistentPublication as exc:
            raise StampAhead(f"publication stamp lacks a valid accepted receipt chain: {exc}") from exc
        heads = [receipt.publication_id for receipt in accepted]
        if rebuilt.publication_id != stamp:
            if stamp is None or stamp in heads:
                raise LedgerAhead("accepted evidence is ahead of the publication stamp")
            raise StampAhead("publication stamp has no matching accepted receipt")
        if current is not None and current.publication_id == stamp:
            if current != rebuilt:
                raise StampAhead("current ledger evidence differs from its accepted history")
            return current
        if current is not None:
            head = next((r for r in accepted if r.publication_id == current.publication_id), None)
            if (
                current != cls(contract)
                if head is None
                else current.with_contract(head.contract).identity != head.ledger_identity
            ):
                raise StampAhead("current ledger is unrelated to the publication stamp")
        if not repair and stamp is not None:
            raise StampAhead("publication stamp is ahead of the ledger; repair from accepted receipts")
        return rebuilt

    def _check_window_type(self, window: Window | None) -> None:
        if window is None:
            return
        logical_type = self.contract.types.get(self.contract.window_column or "")
        if logical_type is not None and type(window[0]) is not LOGICAL_TYPES[logical_type].python_type:
            raise InvalidContract("window bound types differ from the contract types")
        if any(cut.value is not None and type(cut.value) is not type(window[0]) for cut, _ in self._cuts):
            raise InvalidContract("window bound types differ from the ledger domain")

    def covered(self, window: Window | None = None) -> tuple[Region, ...]:
        window = bounds(window)
        self._check_window_type(window)
        return tuple(
            _region(low, high, evidence)
            for low, high, evidence in _segments(self._cuts, *_window_cuts(window))
        )

    def apply(self, manifest: Manifest) -> Ledger:
        if not isinstance(manifest, Manifest):
            raise TypeError(
                f"Ledger.apply takes a Manifest, not {type(manifest).__name__}; "
                "rows reach a store through dogana.deliver"
            )
        try:
            delivery = manifest.check(self.contract)
            window = coverage_window(delivery)
            self._check_window_type(window)
        except InvalidContract as exc:
            raise _rejection(manifest, str(exc)) from exc
        low, high = _window_cuts(window)
        target = tuple(evidence for _, _, evidence in _segments(self._cuts, low, high))
        for name, passed, detail in _guards(self, delivery, target):
            if not passed:
                raise _rejection(manifest, f"{name}: {detail}")
        return Ledger(
            self.contract,
            generation=_cursor(self.generation, delivery),
            publication_id=_publication(self.publication_id, self.contract, manifest),
            _cuts=_apply_steps(self._cuts, low, high, delivery),
        )

    def claims(self, now: datetime, *, limit: timedelta, window: Window | None = None) -> Claims:
        """Each region's state at the reader's time `now`, expiring those proven longer ago than `limit`.

        The limit is the reader's: `timedelta.max` expires no region.
        """
        now = utc(now)
        return Claims(
            tuple(
                RegionClaim(self.contract.table, r, r.state(now, limit=limit)) for r in self.covered(window)
            )
        )


def _guards(
    ledger: Ledger, delivery: ParsedDelivery, target: tuple[Evidence, ...]
) -> tuple[tuple[str, bool, str], ...]:
    known = tuple(evidence for evidence in target if isinstance(evidence, Known))
    bootstrap = (
        isinstance(delivery, AppendInterval)
        and ledger.publication_id is None
        and all(isinstance(evidence, Unknown) for _, evidence in ledger._cuts)
    )
    return (
        (
            "monotone_time",
            all(delivery.manifest.produced_at > e.complete_as_of for e in known),
            "delivery time must be later than every affected region",
        ),
        (
            "known_base",
            isinstance(delivery, (FullDelivery, WindowDelivery)) or bootstrap or len(known) == len(target),
            "changed-only coverage needs a known base throughout",
        ),
        (
            "generation_chain",
            not isinstance(delivery, GenerationChanges) or delivery.since == ledger.generation,
            "generation chain differs from the base; request a complete delivery",
        ),
        (
            "sequence_chain",
            not isinstance(delivery, SequenceChanges) or _continues(ledger, delivery),
            "a sequence read must continue the table's position without a gap; request a complete delivery",
        ),
        (
            "since_before_boundary",
            not isinstance(delivery, (TimestampChanges, AppendInterval))
            or delivery.since <= delivery.manifest.produced_at,
            "since is after the source read boundary",
        ),
        (
            "append_contiguous",
            not isinstance(delivery, AppendInterval)
            or ({ledger.contract.start_at} if bootstrap else {e.complete_as_of for e in known})
            == {delivery.since},
            "append-only intervals must be contiguous without overlap",
        ),
        (
            "no_gap_after_base",
            not isinstance(delivery, TimestampChanges)
            or all(delivery.since <= e.complete_as_of for e in known),
            "timestamp checkpoint leaves a gap after the base",
        ),
    )


def _continues(ledger: Ledger, delivery: SequenceChanges) -> bool:
    """A keyed read may start before the table's position, never after it, and never ends before it."""
    position = ledger.generation
    return type(position) is int and delivery.since <= position <= delivery.through


def _transition(delivery: ParsedDelivery, old: Evidence) -> Known:
    t = delivery.manifest.produced_at
    match delivery:
        case FullDelivery() | WindowDelivery() | AppendInterval():
            return Known(t, t)
        case TimestampChanges() | GenerationChanges() | SequenceChanges():
            assert isinstance(old, Known)
            repairs = delivery.shape.deletions is Deletions.TOMBSTONE and (
                delivery.since <= old.deletions_as_of
                if isinstance(delivery, TimestampChanges)
                else old.deletions_as_of == old.complete_as_of
            )
            return Known(t, t if repairs else old.deletions_as_of)
        case _:
            assert_never(delivery)


def _cursor(previous: str | int | None, delivery: ParsedDelivery) -> str | int | None:
    match delivery:
        case FullDelivery():
            return delivery.manifest.generation
        case GenerationChanges():
            return delivery.generation
        case SequenceChanges():
            return delivery.through
        case WindowDelivery() | TimestampChanges(coverage=WindowCoverage()):
            return previous
        case TimestampChanges() | AppendInterval():
            return previous if delivery.manifest.generation is None else delivery.manifest.generation
        case _:
            assert_never(delivery)


def _publication(previous: str | None, contract: Contract, manifest: Manifest) -> str:
    return sha256(f"{previous}:{contract.identity}:{manifest.identity}".encode()).hexdigest()


def _rejection(manifest: Manifest, detail: str) -> RejectedDelivery:
    return RejectedDelivery(
        f"table={manifest.table!r} shape={manifest.shape!r} "
        f"produced_at={manifest.produced_at.isoformat()} since={manifest.since} "
        f"window={manifest.window!r}: {detail}"
    )
