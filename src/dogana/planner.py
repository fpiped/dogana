"""Derive extraction requests from required evidence, without running reads."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from .contract import Checkpoint, Coverage, Deletions, Manifest, Shape, Window, bounds, identifier
from .errors import InvalidContract, RejectedDelivery
from .errors import PlanningError as PlanningError
from .ledger import Claims, Ledger, Region
from .wire import Wire, utc


@dataclass(frozen=True)
class Requirement(Wire):
    """Minimum evidence dates for a table or closed window."""

    table: str
    complete_as_of: datetime
    valid_as_of: datetime | None = None
    window: Window | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        identifier(self.table)
        object.__setattr__(self, "window", bounds(self.window))


@dataclass(frozen=True)
class ExtractionRequest(Wire):
    """A source read boundary with optional coverage and checkpoint."""

    table: str
    shape: str
    until: datetime
    window: Window | None = None
    since: str | datetime | int | None = None
    cost: float = 1

    def __post_init__(self) -> None:
        super().__post_init__()
        identifier(self.table)
        if not self.shape:
            raise InvalidContract("an extraction request needs a shape name")
        object.__setattr__(self, "window", bounds(self.window))
        if isinstance(self.since, str) and not self.since:
            raise InvalidContract("generation checkpoints must be nonempty strings")
        if isinstance(self.since, datetime) and self.since > self.until:
            raise InvalidContract("since is after the source read boundary")
        if not 0 <= self.cost < float("inf"):
            raise InvalidContract("cost must be finite and nonnegative")

    @property
    def interval(self) -> Window | None:
        if self.window is not None:
            return self.window
        return (self.since, self.until) if isinstance(self.since, datetime) else None


def _rolling_window(ledger: Ledger, shape: Shape, until: datetime) -> Window:
    size = shape.window_size
    if size is None:
        raise PlanningError("a window shape needs a window or window_size")
    date_window = ledger.contract.types.get(ledger.contract.window_column or "") == "date"
    unit = timedelta(days=1) if date_window else timedelta(microseconds=1)
    try:
        low, high = shape.window_ending_at(until, "date" if date_window else "datetime")
        assert isinstance(low, date) and isinstance(high, date)
        start, end = low, high
        minimum = date.min if date_window else datetime.min.replace(tzinfo=until.tzinfo)
        if end == minimum:
            return start, end
        before = Region(high=end, high_open=True)
        anchors: list[date | datetime] = []
        for region in ledger.regions:
            if region.complete_as_of is None:
                continue
            part = region.intersection(before)
            if part is not None and isinstance(part.high, date):
                anchors.append(part.high if part.high_open else part.high + unit)
        if anchors:
            start = min(start, max(anchors))
    except OverflowError as exc:
        raise PlanningError("rolling window exceeds the date or datetime range") from exc
    return start, end


def _request(ledger: Ledger, shape_name: str, until: datetime, window: Window | None) -> ExtractionRequest:
    shape = ledger.contract.shape(shape_name)
    if shape.coverage is Coverage.WINDOW:
        if window is None:
            window = _rolling_window(ledger, shape, until)
    elif window is not None:
        raise PlanningError("only window shapes can extract a window")
    window = bounds(window)
    regions = ledger.covered(window)
    since: str | datetime | int | None = None
    if not shape.complete:
        dates = [r.complete_as_of for r in regions if r.complete_as_of is not None]
        if not ledger.contract.key and ledger.publication_id is None and not dates:
            since = ledger.contract.start_at
        elif len(dates) != len(regions):
            raise PlanningError("changed-only reads need a known base throughout coverage")
        elif shape.checkpoint in (Checkpoint.GENERATION, Checkpoint.SEQUENCE):
            since = ledger.generation
            if since is None:
                raise PlanningError("position continuity is unavailable; extract a complete base")
        else:
            try:
                since = min(dates) - (shape.lookback or timedelta(0))
            except OverflowError as exc:
                raise PlanningError("lookback exceeds the datetime range") from exc
        if isinstance(since, datetime) and since >= until:
            raise PlanningError("next read must advance the source boundary")
    return ExtractionRequest(ledger.contract.table, shape_name, until, window, since, shape.cost)


def _apply_request(ledger: Ledger, request: ExtractionRequest) -> Ledger:
    shape = ledger.contract.shape(request.shape)
    generation: str | int | None = None
    if shape.checkpoint is Checkpoint.GENERATION:
        generation = f"{ledger.generation}:next"
    elif shape.checkpoint is Checkpoint.SEQUENCE and type(request.since) is int:
        generation = request.since
    try:
        return ledger.apply(
            Manifest(
                request.table,
                request.shape,
                request.until,
                window=request.window,
                since=request.since,
                generation=generation,
            )
        )
    except (InvalidContract, RejectedDelivery) as exc:
        raise PlanningError(str(exc)) from exc


def next_extraction(
    ledger: Ledger, shape_name: str, until: datetime, *, window: Window | None = None
) -> ExtractionRequest:
    """Return an admissible read at until, extending rolling coverage across late runs."""
    request = _request(ledger, shape_name, utc(until), window)
    _apply_request(ledger, request)
    return request


def _satisfies(claims: Claims, requirement: Requirement) -> bool:
    complete, valid = claims.complete_as_of, claims.valid_as_of
    return (
        complete is not None
        and complete >= requirement.complete_as_of
        and (requirement.valid_as_of is None or (valid is not None and valid >= requirement.valid_as_of))
    )


def plan_delivery(ledger: Ledger, requirement: Requirement) -> ExtractionRequest | None:
    """Choose the cheapest admitted read that establishes the minimum evidence dates."""
    if ledger.contract.table != requirement.table:
        raise PlanningError("requirement table differs from contract")
    if _satisfies(ledger.claims(requirement.complete_as_of, window=requirement.window), requirement):
        return None
    required = max(requirement.complete_as_of, requirement.valid_as_of or requirement.complete_as_of)
    candidates: list[ExtractionRequest] = []
    for name, shape in ledger.contract.shapes.items():
        if shape.coverage is Coverage.WINDOW and requirement.window is None:
            continue
        window = requirement.window if shape.coverage is Coverage.WINDOW else None
        try:
            covered = ledger.covered(window)
            dates = [r.complete_as_of for r in covered if r.complete_as_of is not None]
            if not dates and not shape.complete and isinstance(ledger.contract.start_at, datetime):
                dates.append(ledger.contract.start_at)
            try:
                until = max(required, max(dates) + timedelta(microseconds=1)) if dates else required
            except OverflowError:
                continue
            request = _request(ledger, name, until, window)
            if (
                shape.checkpoint is Checkpoint.TIMESTAMP
                and shape.deletions is Deletions.TOMBSTONE
                and requirement.valid_as_of is not None
                and isinstance(request.since, datetime)
            ):
                insufficient = [
                    r.valid_as_of
                    for r in ledger.covered(requirement.window)
                    if r.valid_as_of is not None and r.valid_as_of < requirement.valid_as_of
                ]
                if insufficient:
                    request = replace(request, since=min(request.since, min(insufficient)))
            after = _apply_request(ledger, request)
            if _satisfies(after.claims(until, window=requirement.window), requirement):
                candidates.append(request)
        except (PlanningError, InvalidContract):
            continue
    if not candidates:
        raise PlanningError("no admitted shape can establish the required claims")
    return min(candidates, key=lambda r: (r.cost, r.shape))
