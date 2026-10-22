"""Derive extraction requests from required evidence, without running reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .contract import Checkpoint, Coverage, Manifest, Window, bounds
from .ledger import Ledger, RejectedDelivery
from .wire import InvalidContract, Wire, utc


class PlanningError(ValueError):
    """No admitted extraction can establish the requested evidence."""


@dataclass(frozen=True)
class Requirement(Wire):
    table: str
    complete_as_of: datetime
    valid_as_of: datetime | None = None
    window: Window | None = None

    def __post_init__(self):
        object.__setattr__(self, "complete_as_of", utc(self.complete_as_of))
        if self.valid_as_of is not None:
            object.__setattr__(self, "valid_as_of", utc(self.valid_as_of))
        object.__setattr__(self, "window", bounds(self.window))


@dataclass(frozen=True)
class ExtractionRequest(Wire):
    table: str
    shape: str
    until: datetime
    window: Window | None = None
    since: str | datetime | None = None
    cost: float = 1

    def __post_init__(self):
        object.__setattr__(self, "until", utc(self.until))
        object.__setattr__(self, "window", bounds(self.window))
        if isinstance(self.since, datetime):
            object.__setattr__(self, "since", utc(self.since))

    @property
    def interval(self) -> tuple | None:
        if self.window is not None:
            return self.window
        return (self.since, self.until) if isinstance(self.since, datetime) else None


def next_extraction(
    ledger: Ledger, shape_name: str, until: datetime, *, window: Window | None = None
) -> ExtractionRequest:
    until = utc(until)
    shape = ledger.contract.shape(shape_name)
    if shape.coverage is Coverage.WINDOW:
        if window is None:
            if shape.window_size is None:
                raise PlanningError("a window shape needs a window or window_size")
            window = (until - shape.window_size, until)
            column = next(
                (c for c in ledger.contract.schema if c.name == ledger.contract.window_column), None
            )
            if column is not None and column.type == "date":
                window = ((until - shape.window_size + timedelta(days=1)).date(), until.date())
    elif window is not None:
        raise PlanningError("only window shapes can extract a window")
    bounds(window)
    regions = ledger.covered(window)
    since = None
    if not shape.complete:
        if not ledger.contract.key and not ledger.accepted and shape.coverage is Coverage.ALL:
            since = ledger.contract.start_at
        elif any(r.complete_as_of is None for r in regions):
            raise PlanningError("changed-only reads need a known base throughout coverage")
        elif shape.checkpoint is Checkpoint.GENERATION:
            since = ledger.generation
            if since is None or any(r.generation != since for r in regions):
                raise PlanningError("generation continuity is unavailable; extract a complete base")
        else:
            since = min(r.complete_as_of for r in regions)
        if isinstance(since, datetime) and since >= until:
            raise PlanningError("next read must advance the source boundary")
    if any(r.complete_as_of is not None and until <= r.complete_as_of for r in regions):
        raise PlanningError("next read must be newer than covered evidence")
    return ExtractionRequest(ledger.contract.table, shape_name, until, window, since, shape.cost)


def plan_delivery(ledger: Ledger, requirement: Requirement) -> ExtractionRequest | None:
    if ledger.contract.table != requirement.table:
        raise PlanningError("requirement table differs from contract")
    claims = ledger.claims(requirement.complete_as_of, window=requirement.window)
    if (
        claims.complete_as_of is not None
        and claims.complete_as_of >= requirement.complete_as_of
        and (requirement.valid_as_of is None or claims.valid_as_of >= requirement.valid_as_of)
    ):
        return None
    until = max(requirement.complete_as_of, requirement.valid_as_of or requirement.complete_as_of)
    candidates = []
    for name, shape in ledger.contract.shapes.items():
        try:
            if shape.coverage is Coverage.WINDOW and requirement.window is None:
                continue
            window = requirement.window if shape.coverage is Coverage.WINDOW else None
            request = next_extraction(ledger, name, until, window=window)
            simulated = ledger.apply(
                Manifest(
                    ledger.contract.table,
                    name,
                    request.until,
                    window=request.window,
                    since=request.since,
                    generation="planned-generation" if shape.checkpoint is Checkpoint.GENERATION else None,
                )
            )
            proved = simulated.claims(until, window=requirement.window)
            if proved.complete_as_of is None or proved.complete_as_of < requirement.complete_as_of:
                continue
            if requirement.valid_as_of is not None and (
                proved.valid_as_of is None or proved.valid_as_of < requirement.valid_as_of
            ):
                continue
            candidates.append(request)
        except (PlanningError, InvalidContract, RejectedDelivery):
            continue
    if not candidates:
        raise PlanningError("no admitted shape can establish the required claims")
    return min(candidates, key=lambda r: (r.cost, r.shape))
