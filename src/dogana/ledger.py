"""Exact interval accounting, including unknown parts of the domain."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from hashlib import sha256

from .contract import Checkpoint, Contract, Coverage, Deletions, Manifest, Scalar, Window, bounds
from .wire import InvalidContract, Wire, utc


class State(StrEnum):
    UNKNOWN = "unknown"
    COMPLETE = "complete"
    PATCHED = "patched"
    EXPIRED = "expired"


class RejectedDelivery(ValueError):
    """A transition would claim evidence the delivery did not establish."""


@dataclass(frozen=True)
class Region(Wire):
    low: Scalar | None = None
    high: Scalar | None = None
    complete_as_of: datetime | None = None
    valid_as_of: datetime | None = None
    low_open: bool = False
    high_open: bool = False
    generation: str | None = None

    def __post_init__(self):
        for name in ("low", "high"):
            if isinstance(getattr(self, name), datetime):
                object.__setattr__(self, name, utc(getattr(self, name)))
        if self.low is not None and self.high is not None:
            bounds((self.low, self.high))
            if not _nonempty(self.low, self.high, self.low_open, self.high_open):
                raise InvalidContract("region is empty in its scalar domain")
        for value in (self.low, self.high):
            if value is not None and type(value) not in (str, int, datetime, date):
                raise InvalidContract("unsupported region bound")
        if (self.complete_as_of is None) != (self.valid_as_of is None):
            raise InvalidContract("region dates must be present or absent together")
        if self.complete_as_of is not None:
            object.__setattr__(self, "complete_as_of", utc(self.complete_as_of))
            object.__setattr__(self, "valid_as_of", utc(self.valid_as_of))
            if self.valid_as_of > self.complete_as_of:
                raise InvalidContract("validity cannot be newer than completeness")

    def state(self, now: datetime, limit: timedelta) -> State:
        now = utc(now)
        if self.complete_as_of is None:
            return State.UNKNOWN
        if now - self.complete_as_of > limit:
            return State.EXPIRED
        return State.COMPLETE if self.complete_as_of == self.valid_as_of else State.PATCHED

    def intersection(self, other: Region) -> Region | None:
        low, lo = self.low, self.low_open
        if low is None or (other.low is not None and other.low > low):
            low, lo = other.low, other.low_open
        elif low == other.low:
            lo = lo or other.low_open
        high, hi = self.high, self.high_open
        if high is None or (other.high is not None and other.high < high):
            high, hi = other.high, other.high_open
        elif high == other.high:
            hi = hi or other.high_open
        if not _nonempty(low, high, lo, hi):
            return None
        return replace(self, low=low, high=high, low_open=lo, high_open=hi)

    def outside(self, inside: Region) -> tuple[Region, ...]:
        parts = []
        if (
            inside.low is not None
            and (self.low, self.low_open) != (inside.low, inside.low_open)
            and _nonempty(self.low, inside.low, self.low_open, not inside.low_open)
        ):
            parts.append(replace(self, high=inside.low, high_open=not inside.low_open))
        if (
            inside.high is not None
            and (self.high, self.high_open) != (inside.high, inside.high_open)
            and _nonempty(inside.high, self.high, not inside.high_open, self.high_open)
        ):
            parts.append(replace(self, low=inside.high, low_open=not inside.high_open))
        return tuple(parts)

    def touches(self, other: Region) -> bool:
        if self.high is None or other.low is None:
            return False
        if self.high == other.low:
            return self.high_open != other.low_open
        unit = _unit(self.high)
        if unit is None or type(self.high) is not type(other.low):
            return False
        distance = other.low - self.high
        return distance == unit * (1 - self.high_open - other.low_open)


def _unit(value):
    if type(value) is int:
        return 1
    if type(value) is datetime:
        return timedelta(microseconds=1)
    if type(value) is date:
        return timedelta(days=1)
    return None


def _nonempty(low, high, low_open, high_open) -> bool:
    if low is None or high is None:
        return True
    if low > high or (low == high and (low_open or high_open)):
        return False
    unit = _unit(low)
    return unit is None or high - low >= unit * (low_open + high_open)


@dataclass(frozen=True)
class RegionClaim(Wire):
    table: str
    region: Region
    state: State

    def __post_init__(self):
        object.__setattr__(self, "state", State(self.state))


@dataclass(frozen=True)
class Claims(Wire):
    regions: tuple[RegionClaim, ...]

    def __post_init__(self):
        object.__setattr__(self, "regions", tuple(self.regions))
        if not self.regions or not all(isinstance(r, RegionClaim) for r in self.regions):
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
        return None if None in dates else min(dates)

    @property
    def valid_as_of(self) -> datetime | None:
        dates = [r.region.valid_as_of for r in self.regions]
        return None if None in dates else min(dates)

    def in_state(self, state: State) -> tuple[RegionClaim, ...]:
        return tuple(r for r in self.regions if r.state is state)

    @classmethod
    def combine(cls, *claims: Claims) -> Claims:
        return cls(tuple(r for claim in claims for r in claim.regions))


@dataclass(frozen=True)
class Ledger(Wire):
    contract: Contract
    regions: tuple[Region, ...] = (Region(),)
    generation: str | None = None
    accepted: tuple[str, ...] = ()
    publication_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "regions", tuple(self.regions))
        object.__setattr__(self, "accepted", tuple(self.accepted))
        if (
            not isinstance(self.contract, Contract)
            or not self.regions
            or not all(isinstance(r, Region) for r in self.regions)
        ):
            raise InvalidContract("a ledger needs a contract and regions")
        if (
            self.regions[0].low is not None
            or self.regions[-1].high is not None
            or not all(a.touches(b) for a, b in zip(self.regions, self.regions[1:], strict=False))
        ):
            raise InvalidContract("ledger regions must partition the entire ordered domain")
        if len(set(self.accepted)) != len(self.accepted):
            raise InvalidContract("accepted manifest identities must be unique")

    def covered(self, window: Window | None = None) -> tuple[Region, ...]:
        window = bounds(window)
        coverage = Region(*window) if window is not None else Region()
        try:
            return tuple(part for r in self.regions if (part := r.intersection(coverage)) is not None)
        except TypeError as exc:
            raise InvalidContract("window bounds differ from the ledger domain") from exc

    def apply(self, manifest: Manifest) -> Ledger:
        shape = manifest.check(self.contract)
        if manifest.identity in self.accepted:
            return self
        target = self.covered(manifest.window)
        t = manifest.produced_at
        if any(r.complete_as_of is not None and t <= r.complete_as_of for r in target):
            raise RejectedDelivery("delivery time must be later than every affected region")
        if not shape.complete:
            bootstrap = not self.contract.key and not self.accepted and shape.coverage is Coverage.ALL
            if any(r.complete_as_of is None for r in target) and not bootstrap:
                raise RejectedDelivery("changed-only coverage needs a known base throughout")
            if shape.checkpoint is Checkpoint.GENERATION:
                if (
                    self.generation is None
                    or manifest.since != self.generation
                    or any(r.generation != manifest.since for r in target)
                ):
                    raise RejectedDelivery(
                        "generation chain differs from the base; request a complete delivery"
                    )
            else:
                if manifest.since > t:
                    raise RejectedDelivery("since is after the source read boundary")
                if not self.contract.key:
                    boundaries = {self.contract.start_at} if bootstrap else {r.complete_as_of for r in target}
                    if boundaries != {manifest.since}:
                        raise RejectedDelivery("append-only intervals must be contiguous without overlap")
                elif any(manifest.since > r.complete_as_of for r in target):
                    raise RejectedDelivery("timestamp checkpoint leaves a gap after the base")
        coverage = Region(*manifest.window) if manifest.window is not None else Region()
        result = []
        for old in self.regions:
            part = old.intersection(coverage)
            if part is None:
                result.append(old)
                continue
            result.extend(old.outside(part))
            valid = (
                t
                if shape.complete
                or not self.contract.key
                or (shape.deletions is Deletions.TOMBSTONE and part.valid_as_of == part.complete_as_of)
                else part.valid_as_of
            )
            result.append(replace(part, complete_as_of=t, valid_as_of=valid, generation=manifest.generation))
        generation = (
            manifest.generation
            if shape.complete and shape.coverage is Coverage.ALL
            else (manifest.generation or self.generation)
        )
        publication = sha256(
            f"{self.publication_id}:{self.contract.identity}:{manifest.identity}".encode()
        ).hexdigest()
        return Ledger(
            self.contract, _merge(result), generation, self.accepted + (manifest.identity,), publication
        )

    def claims(self, now: datetime, limit: timedelta | None = None, window: Window | None = None) -> Claims:
        limit = self.contract.freshness if limit is None else limit
        if not isinstance(limit, timedelta) or limit < timedelta(0):
            raise InvalidContract("freshness limit must be nonnegative")
        return Claims(
            tuple(RegionClaim(self.contract.table, r, r.state(now, limit)) for r in self.covered(window))
        )


def _merge(regions: list[Region]) -> tuple[Region, ...]:
    ordered = sorted(
        regions, key=lambda r: (r.low is not None, r.low if r.low is not None else 0, r.low_open)
    )
    result = []
    for region in ordered:
        if (
            result
            and result[-1].touches(region)
            and (result[-1].complete_as_of, result[-1].valid_as_of, result[-1].generation)
            == (region.complete_as_of, region.valid_as_of, region.generation)
        ):
            result[-1] = replace(result[-1], high=region.high, high_open=region.high_open)
        else:
            result.append(region)
    return tuple(result)
