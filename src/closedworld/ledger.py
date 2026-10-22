"""The ledger of one table: disjoint regions, each with the two dates the protocol defines."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from .contract import Checkpoint, Contract, Coverage, Deletions, Manifest


class State(str, Enum):
    UNKNOWN = "unknown"
    COMPLETE = "complete"
    PATCHED = "patched"
    EXPIRED = "expired"


class RejectedDelivery(Exception):
    """The delivery cannot be applied without claiming more than it proves."""


@dataclass(frozen=True)
class Region:
    """An interval on the window column. None is unbounded on that side; an open flag excludes the bound."""

    low: Any
    high: Any
    complete_as_of: datetime
    valid_as_of: datetime
    low_open: bool = False
    high_open: bool = False

    def state(self, now: datetime, limit: timedelta) -> State:
        if now - self.complete_as_of > limit:
            return State.EXPIRED
        return State.COMPLETE if self.complete_as_of == self.valid_as_of else State.PATCHED

    def overlaps(self, low: Any, high: Any) -> bool:
        """Against the closed interval [low, high]."""
        return not (_ends_before(self.high, self.high_open, low) or _ends_before(high, False, self.low, self.low_open))

    def touches(self, other: Region) -> bool:
        """Adjacent without a gap: the shared bound belongs to exactly one of the two."""
        return self.high is not None and self.high == other.low and self.high_open != other.low_open


def _ends_before(high: Any, high_open: bool, low: Any, low_open: bool = False) -> bool:
    if high is None or low is None:
        return False
    return high < low or (high == low and (high_open or low_open))


@dataclass(frozen=True)
class Ledger:
    contract: Contract
    regions: tuple[Region, ...] = ()
    generation: str | None = None

    def apply(self, manifest: Manifest) -> Ledger:
        """The ledger after a delivery, or RejectedDelivery. Never raises a date above what is proved."""
        shape = manifest.check(self.contract)
        t = manifest.produced_at
        low, high = manifest.window if manifest.window else (None, None)
        generation = manifest.generation or self.generation
        if shape.complete:
            kept = tuple(part for r in self.regions for part in _outside(r, low, high))
            regions = kept + (Region(low, high, t, t),)
        else:
            if not self.regions:
                raise RejectedDelivery("a changed-only delivery needs a complete delivery to patch")
            if shape.checkpoint is Checkpoint.GENERATION and manifest.since != self.generation:
                raise RejectedDelivery(f"feed since {manifest.since!r} offered to a ledger at {self.generation!r}")
            windowed = shape.coverage is Coverage.WINDOW
            valid = shape.deletions is Deletions.TOMBSTONE
            regions = tuple(
                replace(r, complete_as_of=t, valid_as_of=t if valid else r.valid_as_of)
                if not windowed or r.overlaps(low, high) else r
                for r in self.regions
            )
        return Ledger(self.contract, tuple(sorted(regions, key=_order)), generation)

    def states(self, now: datetime, limit: timedelta) -> dict[Region, State]:
        return {r: r.state(now, limit) for r in self.regions}

    def covers_all(self) -> bool:
        """No value of the column is outside every region."""
        if not self.regions:
            return False
        first, last = self.regions[0], self.regions[-1]
        return first.low is None and last.high is None and all(
            a.touches(b) for a, b in zip(self.regions, self.regions[1:])
        )

    def claims(self, now: datetime, limit: timedelta) -> dict[str, Any]:
        """What a reader may rely on for the table as a whole: the weakest region decides."""
        if not self.regions:
            return {"state": State.UNKNOWN, "complete_as_of": None, "valid_as_of": None, "covers_all": False}
        states = set(self.states(now, limit).values())
        worst = next(s for s in (State.EXPIRED, State.PATCHED, State.COMPLETE) if s in states)
        covers_all = self.covers_all()
        return {
            "state": worst if covers_all or worst is not State.COMPLETE else State.UNKNOWN,
            "complete_as_of": min(r.complete_as_of for r in self.regions),
            "valid_as_of": min(r.valid_as_of for r in self.regions),
            "covers_all": covers_all,
        }


def _outside(r: Region, low: Any, high: Any) -> list[Region]:
    """The parts of r not covered by the closed interval [low, high]."""
    if not r.overlaps(low, high):
        return [r]
    parts = []
    if low is not None and (r.low is None or r.low < low):
        parts.append(replace(r, high=low, high_open=True))
    if high is not None and (r.high is None or high < r.high):
        parts.append(replace(r, low=high, low_open=True))
    return parts


def _order(r: Region) -> tuple:
    return (0, 0, 0) if r.low is None else (1, r.low, int(r.low_open))
