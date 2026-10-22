"""Exact interval accounting, including unknown parts of the domain."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from functools import cmp_to_key
from hashlib import sha256
from typing import TYPE_CHECKING

from .contract import (
    Checkpoint,
    Contract,
    Coverage,
    Deletions,
    Manifest,
    Scalar,
    Window,
    bounds,
    identifier,
    scalar_compare,
)
from .errors import InconsistentPublication as InconsistentPublication
from .errors import InvalidContract
from .errors import RejectedDelivery as RejectedDelivery
from .wire import Wire, utc

if TYPE_CHECKING:
    from .gate import Receipt


class State(StrEnum):
    UNKNOWN = "unknown"
    COMPLETE = "complete"
    PATCHED = "patched"
    EXPIRED = "expired"


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
    if isinstance(value, date):
        return date.min
    return None


def _maximum(value: Scalar) -> Scalar | None:
    if isinstance(value, datetime):
        return datetime.max.replace(tzinfo=UTC)
    if isinstance(value, date):
        return date.max
    return None


def _nonempty(low: Scalar | None, high: Scalar | None, low_open: bool, high_open: bool) -> bool:
    if low is None:
        if high is None:
            return True
        low = _minimum(high)
        if low is None:
            return True
        low_open = False
    first = _successor(low) if low_open else low
    if first is None:
        return False
    if high is None:
        return True
    comparison = scalar_compare(first, high)
    return comparison < 0 or (comparison == 0 and not high_open)


@dataclass(frozen=True)
class Region(Wire):
    low: Scalar | None = None
    high: Scalar | None = None
    complete_as_of: datetime | None = None
    valid_as_of: datetime | None = None
    low_open: bool = False
    high_open: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.low is not None and self.high is not None:
            bounds((self.low, self.high))
        if not _nonempty(self.low, self.high, self.low_open, self.high_open):
            raise InvalidContract("region is empty in its scalar domain")
        if self.low is None and self.low_open or self.high is None and self.high_open:
            raise InvalidContract("unbounded endpoints cannot be open")
        if (self.complete_as_of is None) != (self.valid_as_of is None):
            raise InvalidContract("region dates must be present or absent together")
        if self.complete_as_of is not None and self.valid_as_of is not None:
            if self.valid_as_of > self.complete_as_of:
                raise InvalidContract("validity cannot be newer than completeness")

    def state(self, now: datetime, limit: timedelta) -> State:
        now = utc(now)
        if not isinstance(limit, timedelta) or limit < timedelta(0):
            raise InvalidContract("freshness limit must be nonnegative")
        if self.complete_as_of is None:
            return State.UNKNOWN
        if now - self.complete_as_of > limit:
            return State.EXPIRED
        return State.COMPLETE if self.complete_as_of == self.valid_as_of else State.PATCHED

    def intersection(self, other: Region) -> Region | None:
        low, lo = self.low, self.low_open
        if low is None or (other.low is not None and scalar_compare(other.low, low) > 0):
            low, lo = other.low, other.low_open
        elif low == other.low:
            lo = lo or other.low_open
        high, hi = self.high, self.high_open
        if high is None or (other.high is not None and scalar_compare(other.high, high) < 0):
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
        if (
            self.high is None
            or other.low is None
            or _region_order(self, other) >= 0
            or self.intersection(other) is not None
        ):
            return False
        return not _nonempty(self.high, other.low, not self.high_open, not other.low_open)


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
    def valid_as_of(self) -> datetime | None:
        dates = [r.region.valid_as_of for r in self.regions]
        return None if any(t is None for t in dates) else min(t for t in dates if t is not None)

    def in_state(self, state: State) -> tuple[RegionClaim, ...]:
        return tuple(r for r in self.regions if r.state is state)

    @classmethod
    def combine(cls, *claims: Claims) -> Claims:
        """Return the weakest input bounds, without asserting a common snapshot."""
        return cls(tuple(r for claim in claims for r in claim.regions))


@dataclass(frozen=True)
class Ledger(Wire):
    contract: Contract
    regions: tuple[Region, ...] = (Region(),)
    generation: str | None = None
    publication_id: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.regions:
            raise InvalidContract("a ledger needs regions")
        finite = [v for r in self.regions for v in (r.low, r.high) if v is not None]
        if len({type(v) for v in finite}) > 1:
            raise InvalidContract("ledger regions must share one scalar domain")
        for value in finite:
            self._check_window_type((value, value))
        first, last = self.regions[0], self.regions[-1]
        starts = first.low is None or (not first.low_open and first.low == _minimum(first.low))
        ends = last.high is None or (not last.high_open and last.high == _maximum(last.high))
        if (
            not starts
            or not ends
            or not all(a.touches(b) for a, b in zip(self.regions, self.regions[1:], strict=False))
        ):
            raise InvalidContract("ledger regions must partition the entire ordered domain")
        for token in (self.generation, self.publication_id):
            if token is not None and not token:
                raise InvalidContract("generation and publication identities must be nonempty")

    def with_contract(self, contract: Contract) -> Ledger:
        if not self.contract.compatible_with(contract):
            raise InvalidContract("contract changes fields bound to the publication history")
        return self if self.contract == contract else replace(self, contract=contract)

    @classmethod
    def from_receipts(cls, contract: Contract, receipts: Iterable[Receipt]) -> Ledger:
        ledger = cls(contract)
        for receipt in receipts:
            if not receipt.accepted:
                continue
            try:
                ledger = ledger.with_contract(receipt.ledger.contract)
                if receipt.base_publication_id != ledger.publication_id:
                    raise InconsistentPublication("accepted receipt has a different parent publication")
                after = ledger.apply(receipt.manifest)
            except (InvalidContract, RejectedDelivery) as exc:
                raise InconsistentPublication(f"accepted receipt breaks publication chain: {exc}") from exc
            if after.publication_id != receipt.publication_id or after != receipt.ledger:
                raise InconsistentPublication("accepted receipt breaks publication chain or ledger evidence")
            if receipt.transition is None or receipt.transition.before != ledger.regions:
                raise InconsistentPublication("accepted receipt has different preceding evidence")
            ledger = after
        return ledger.with_contract(contract)

    def _check_window_type(self, window: Window | None) -> None:
        if window is None:
            return
        column = next((c for c in self.contract.schema if c.name == self.contract.window_column), None)
        types = {"int": int, "str": str, "date": date, "datetime": datetime}
        if column is not None and type(window[0]) is not types[column.type]:
            raise InvalidContract("window bound types differ from the contract schema")

    def covered(self, window: Window | None = None) -> tuple[Region, ...]:
        window = bounds(window)
        self._check_window_type(window)
        coverage = Region(*window) if window is not None else Region()
        return tuple(part for r in self.regions if (part := r.intersection(coverage)) is not None)

    def apply(self, manifest: Manifest) -> Ledger:
        shape = manifest.check(self.contract)
        target = self.covered(manifest.window)
        t = manifest.produced_at
        if any(r.complete_as_of is not None and t <= r.complete_as_of for r in target):
            raise RejectedDelivery("delivery time must be later than every affected region")
        if not shape.complete:
            bootstrap = (
                not self.contract.key
                and self.publication_id is None
                and all(r.complete_as_of is None for r in self.regions)
                and shape.coverage is Coverage.ALL
            )
            if any(r.complete_as_of is None for r in target) and not bootstrap:
                raise RejectedDelivery("changed-only coverage needs a known base throughout")
            if shape.checkpoint is Checkpoint.GENERATION:
                if self.generation is None or manifest.since != self.generation:
                    raise RejectedDelivery(
                        "generation chain differs from the base; request a complete delivery"
                    )
            else:
                since = manifest.since
                if not isinstance(since, datetime):
                    raise InvalidContract("timestamp checkpoints must be datetimes")
                if since > t:
                    raise RejectedDelivery("since is after the source read boundary")
                if not self.contract.key:
                    boundaries = {self.contract.start_at} if bootstrap else {r.complete_as_of for r in target}
                    if boundaries != {since}:
                        raise RejectedDelivery("append-only intervals must be contiguous without overlap")
                elif any(r.complete_as_of is not None and since > r.complete_as_of for r in target):
                    raise RejectedDelivery("timestamp checkpoint leaves a gap after the base")
        coverage = Region(*manifest.window) if manifest.window is not None else Region()
        result = []
        for old in self.regions:
            part = old.intersection(coverage)
            if part is None:
                result.append(old)
                continue
            result.extend(old.outside(part))
            repairs = shape.deletions is Deletions.TOMBSTONE and (
                part.valid_as_of is not None
                and (
                    part.valid_as_of >= manifest.since
                    if isinstance(manifest.since, datetime)
                    else part.valid_as_of == part.complete_as_of
                )
            )
            valid = t if shape.complete or not self.contract.key or repairs else part.valid_as_of
            result.append(replace(part, complete_as_of=t, valid_as_of=valid))
        generation = self.generation
        if shape.coverage is not Coverage.WINDOW:
            if shape.complete or manifest.generation is not None:
                generation = manifest.generation
        publication = sha256(
            f"{self.publication_id}:{self.contract.identity}:{manifest.identity}".encode()
        ).hexdigest()
        return Ledger(self.contract, _merge(result), generation, publication)

    def claims(self, now: datetime, limit: timedelta | None = None, window: Window | None = None) -> Claims:
        now = utc(now)
        limit = self.contract.freshness if limit is None else limit
        if not isinstance(limit, timedelta) or limit < timedelta(0):
            raise InvalidContract("freshness limit must be nonnegative")
        return Claims(
            tuple(RegionClaim(self.contract.table, r, r.state(now, limit)) for r in self.covered(window))
        )


def _region_order(left: Region, right: Region) -> int:
    if left.low is None:
        return -int(right.low is not None)
    if right.low is None:
        return 1
    comparison = scalar_compare(left.low, right.low)
    return comparison or ((left.low_open > right.low_open) - (left.low_open < right.low_open))


def _merge(regions: list[Region]) -> tuple[Region, ...]:
    ordered = sorted(regions, key=cmp_to_key(_region_order))
    result: list[Region] = []
    for region in ordered:
        if (
            result
            and result[-1].touches(region)
            and (result[-1].complete_as_of, result[-1].valid_as_of)
            == (region.complete_as_of, region.valid_as_of)
        ):
            result[-1] = replace(result[-1], high=region.high, high_open=region.high_open)
        else:
            result.append(region)
    return tuple(result)
