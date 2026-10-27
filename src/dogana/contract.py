"""Receiver declarations and source claims, independent of rows and storage."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, TypeAlias, assert_never

from .wire import InvalidContract, Wire, utc

Scalar = str | int | date | datetime
Window = tuple[Scalar, Scalar]


class Coverage(StrEnum):
    ALL = "all"
    WINDOW = "window"


class Completeness(StrEnum):
    COMPLETE = "complete"
    CHANGED_ONLY = "changed_only"


class Deletions(StrEnum):
    ABSENCE = "absence"
    TOMBSTONE = "tombstone"
    NONE = "none"


class Checkpoint(StrEnum):
    GENERATION = "generation"
    SEQUENCE = "sequence"
    TIMESTAMP = "timestamp"
    NONE = "none"


def identifier(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise InvalidContract("identifiers must contain letters, digits or underscores")
    if value.casefold().startswith("__dogana_"):
        raise InvalidContract("the __dogana_ prefix is reserved")


def bounds(value: Window | None) -> Window | None:
    if value is None:
        return None
    if not isinstance(value, (tuple, list)) or len(value) != 2 or type(value[0]) is not type(value[1]):
        raise InvalidContract("a window has two bounds of one scalar type")
    if type(value[0]) not in (str, int, date, datetime):
        raise InvalidContract("window bounds must be strings, integers, dates or datetimes")
    low, high = value
    if isinstance(low, datetime) and isinstance(high, datetime):
        low, high = utc(low), utc(high)
    if scalar_compare(high, low) < 0:
        raise InvalidContract("window high before low")
    return low, high


def scalar_compare(left: Scalar, right: Scalar) -> int:
    if type(left) is not type(right):
        raise InvalidContract("scalar types differ")
    if isinstance(left, datetime) and isinstance(right, datetime):
        left, right = utc(left), utc(right)
        return (left > right) - (left < right)
    if isinstance(left, date) and isinstance(right, date):
        return (left > right) - (left < right)
    if type(left) is int and type(right) is int:
        return (left > right) - (left < right)
    if type(left) is str and type(right) is str:
        try:
            left.encode("utf-8")
            right.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InvalidContract("bounds must contain only Unicode scalar values") from exc
        return (left > right) - (left < right)
    raise InvalidContract("values must be ordered scalars")


@dataclass(frozen=True)
class LogicalType:
    python_type: type
    ordered: bool


LOGICAL_TYPES = MappingProxyType(
    {
        "int": LogicalType(int, True),
        "float": LogicalType(float, False),
        "str": LogicalType(str, True),
        "bool": LogicalType(bool, False),
        "date": LogicalType(date, True),
        "datetime": LogicalType(datetime, True),
    }
)


@dataclass(frozen=True)
class Complete:
    coverage: Literal[Coverage.ALL, Coverage.WINDOW]


@dataclass(frozen=True)
class Changed:
    coverage: Coverage
    checkpoint: Literal[Checkpoint.TIMESTAMP, Checkpoint.GENERATION, Checkpoint.SEQUENCE]
    deletions: Literal[Deletions.TOMBSTONE, Deletions.NONE]


@dataclass(frozen=True, kw_only=True)
class Shape(Wire):
    coverage: Coverage
    completeness: Completeness
    deletions: Deletions
    checkpoint: Checkpoint
    cost: float = 1
    window_size: timedelta | None = None
    lookback: timedelta | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _ = self.semantics
        if self.cost < 0:
            raise InvalidContract("cost must be finite and nonnegative")
        if self.window_size is not None and (
            self.coverage is not Coverage.WINDOW or self.window_size <= timedelta(0)
        ):
            raise InvalidContract("a positive window_size belongs to a window shape")
        if self.lookback is not None and (
            self.checkpoint is not Checkpoint.TIMESTAMP or self.lookback < timedelta(0)
        ):
            raise InvalidContract("a nonnegative lookback belongs to a timestamp shape")

    @property
    def semantics(self) -> Complete | Changed:
        match self.completeness, self.coverage, self.checkpoint, self.deletions:
            case Completeness.COMPLETE, (
                Coverage.ALL | Coverage.WINDOW
            ) as coverage, Checkpoint.NONE, Deletions.ABSENCE:
                return Complete(coverage)
            case Completeness.CHANGED_ONLY, coverage, (
                Checkpoint.TIMESTAMP | Checkpoint.GENERATION | Checkpoint.SEQUENCE
            ) as checkpoint, (Deletions.TOMBSTONE | Deletions.NONE) as deletions if (
                coverage is not Coverage.WINDOW or checkpoint is Checkpoint.TIMESTAMP
            ):
                return Changed(coverage, checkpoint, deletions)
        raise InvalidContract(
            f"invalid shape: completeness={self.completeness!r}, coverage={self.coverage!r}, "
            f"checkpoint={self.checkpoint!r}, deletions={self.deletions!r}"
        )

    @property
    def complete(self) -> bool:
        return isinstance(self.semantics, Complete)

    @classmethod
    def full(cls, *, cost: float = 1) -> Shape:
        return cls(
            coverage=Coverage.ALL,
            completeness=Completeness.COMPLETE,
            deletions=Deletions.ABSENCE,
            checkpoint=Checkpoint.NONE,
            cost=cost,
        )

    @classmethod
    def window(cls, *, size: timedelta | None = None, cost: float = 1) -> Shape:
        return cls(
            coverage=Coverage.WINDOW,
            completeness=Completeness.COMPLETE,
            deletions=Deletions.ABSENCE,
            checkpoint=Checkpoint.NONE,
            window_size=size,
            cost=cost,
        )

    @classmethod
    def changes(
        cls,
        *,
        checkpoint: Checkpoint = Checkpoint.TIMESTAMP,
        deletions: Deletions = Deletions.NONE,
        coverage: Coverage = Coverage.ALL,
        cost: float = 1,
        lookback: timedelta | None = None,
        window_size: timedelta | None = None,
    ) -> Shape:
        return cls(
            coverage=coverage,
            completeness=Completeness.CHANGED_ONLY,
            deletions=deletions,
            checkpoint=checkpoint,
            cost=cost,
            lookback=lookback,
            window_size=window_size,
        )

    @classmethod
    def feed(cls, *, checkpoint: Checkpoint = Checkpoint.GENERATION, cost: float = 1) -> Shape:
        return cls.changes(checkpoint=checkpoint, deletions=Deletions.TOMBSTONE, cost=cost)

    @classmethod
    def append(cls, *, cost: float = 1) -> Shape:
        return cls.changes(cost=cost)

    def window_ending_at(self, until: datetime, logical_type: str = "datetime") -> Window:
        until = utc(until)
        if self.window_size is None or logical_type not in ("date", "datetime"):
            raise InvalidContract(
                f"window_size={self.window_size!r} needs a date or datetime domain: {logical_type!r}"
            )
        if logical_type == "date":
            if self.window_size % timedelta(days=1):
                raise InvalidContract(f"DATE window_size={self.window_size!r} must contain whole days")
            end = until.date()
            return end - (self.window_size - timedelta(days=1)), end
        return until - self.window_size, until


@dataclass(frozen=True)
class Contract(Wire):
    table: str
    key: tuple[str, ...]
    shapes: Mapping[str, Shape]
    _: KW_ONLY
    window_column: str | None = None
    types: Mapping[str, str] = field(default_factory=dict)
    schema_hash: str | None = None
    max_shrink_fraction: float | None = 0.5
    freshness: timedelta = timedelta(days=7)
    start_at: datetime | None = None
    cursor_column: str | None = None
    deleted_column: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        identifier(self.table)
        if not self.shapes or any(not name for name in self.shapes):
            raise InvalidContract("a contract admits at least one named shape")
        for name in self.key:
            identifier(name)
        if len(set(self.key)) != len(self.key):
            raise InvalidContract("duplicate key column")
        windowed = any(s.coverage is Coverage.WINDOW for s in self.shapes.values())
        if windowed != (self.window_column is not None):
            raise InvalidContract("a window shape needs exactly one window column")
        if self.window_column is not None:
            identifier(self.window_column)
        bound_columns = set(self.key)
        if self.window_column is not None:
            bound_columns.add(self.window_column)
        if self.cursor_column is not None:
            identifier(self.cursor_column)
            bound_columns.add(self.cursor_column)
        tombstones = any(s.deletions is Deletions.TOMBSTONE for s in self.shapes.values())
        if tombstones != (self.deleted_column is not None):
            raise InvalidContract("a tombstone shape needs exactly one deleted column")
        if self.deleted_column is not None:
            identifier(self.deleted_column)
            if self.deleted_column in bound_columns:
                raise InvalidContract("the deleted column is a column of its own")
            bound_columns.add(self.deleted_column)
        if set(self.types) != bound_columns:
            raise InvalidContract("types must cover exactly the key, window, cursor and deleted columns")
        for name, logical_type in self.types.items():
            identifier(name)
            if logical_type not in LOGICAL_TYPES:
                raise InvalidContract(f"unsupported logical type {logical_type!r} for {name!r}")
        if self.deleted_column is not None and self.types[self.deleted_column] != "bool":
            raise InvalidContract("deleted_column must have type bool")
        checkpoints = {s.checkpoint for s in self.shapes.values()}
        cursor_type = self.types.get(self.cursor_column or "")
        if cursor_type not in (None, "datetime", "int"):
            raise InvalidContract("cursor_column must have type datetime or int")
        checked = {"datetime": Checkpoint.TIMESTAMP, "int": Checkpoint.SEQUENCE}.get(cursor_type or "")
        incremental = {s.checkpoint for s in self.shapes.values() if not s.complete}
        if cursor_type is not None and incremental and checked not in incremental:
            raise InvalidContract(f"a {cursor_type} cursor_column checks none of the changed-only shapes")
        if {Checkpoint.GENERATION, Checkpoint.SEQUENCE} <= checkpoints:
            raise InvalidContract("a table position is either an opaque generation or an ordered sequence")
        if any(
            not LOGICAL_TYPES[self.types[name]].ordered
            for name in set(self.key) | ({self.window_column} if self.window_column else set())
        ):
            raise InvalidContract("key and window columns must use ordered scalar logical types")
        window_type = self.types.get(self.window_column or "")
        if window_type not in (None, "date", "datetime") and any(
            shape.window_size is not None for shape in self.shapes.values()
        ):
            raise InvalidContract("duration windows need a date or datetime column")
        if window_type == "date" and any(
            shape.window_size is not None and shape.window_size % timedelta(days=1)
            for shape in self.shapes.values()
        ):
            raise InvalidContract("DATE window_size must contain a whole number of days")
        if self.freshness < timedelta(0):
            raise InvalidContract("freshness must be a nonnegative duration")
        if self.max_shrink_fraction is not None and not 0 <= self.max_shrink_fraction <= 1:
            raise InvalidContract("max_shrink_fraction must be between zero and one")
        if not self.key:
            if any(shape.lookback is not None for shape in self.shapes.values()):
                raise InvalidContract("lookback requires a keyed contract")
            changed = [s for s in self.shapes.values() if not s.complete]
            if any(
                s.deletions is not Deletions.NONE
                or s.checkpoint is not Checkpoint.TIMESTAMP
                or s.coverage is not Coverage.ALL
                for s in changed
            ):
                raise InvalidContract(
                    "without a key, changed-only shapes are append-only timestamp intervals"
                )
            if bool(changed) != (self.start_at is not None):
                raise InvalidContract("start_at exists exactly when an append-only shape exists")
        elif self.start_at is not None:
            raise InvalidContract("start_at belongs to append-only history")

    def shape(self, name: str) -> Shape:
        if type(name) is not str:
            raise InvalidContract("shape name must be a string")
        try:
            return self.shapes[name]
        except KeyError:
            raise InvalidContract(f"{self.table} admits no shape named {name!r}") from None

    def compatible_with(self, other: Contract) -> bool:
        """Whether both contracts interpret the same table history."""
        if not isinstance(other, Contract):
            raise InvalidContract("history compatibility requires a Contract")
        if (self.table, self.key, self.window_column) != (other.table, other.key, other.window_column):
            return False
        names = set(self.key)
        if self.window_column is not None:
            names.add(self.window_column)
        return all(self.types[name] == other.types[name] for name in names)

    def __hash__(self) -> int:
        return hash(
            (
                self.table,
                self.key,
                tuple(sorted(self.shapes.items())),
                self.window_column,
                tuple(sorted(self.types.items())),
                self.schema_hash,
                self.max_shrink_fraction,
                self.freshness,
                self.start_at,
                self.cursor_column,
                self.deleted_column,
            )
        )

    def manifest(
        self,
        shape: str,
        produced_at: datetime,
        *,
        window: Window | None = None,
        since: str | datetime | int | None = None,
        generation: str | int | None = None,
        rows: int | None = None,
        tombstones: int = 0,
        content_hash: str | None = None,
    ) -> Manifest:
        manifest = Manifest(
            self.table,
            shape,
            produced_at,
            window=window,
            since=since,
            generation=generation,
            rows=rows,
            schema_hash=self.schema_hash,
            tombstones=tombstones,
            content_hash=content_hash,
        )
        manifest.check(self)
        return manifest


@dataclass(frozen=True)
class Manifest(Wire):
    table: str
    shape: str
    produced_at: datetime
    _: KW_ONLY
    window: Window | None = None
    since: str | datetime | int | None = None
    generation: str | int | None = None
    rows: int | None = None
    schema_hash: str | None = None
    tombstones: int = 0
    content_hash: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        identifier(self.table)
        if not self.shape:
            raise InvalidContract("shape must be a nonempty string")
        object.__setattr__(self, "window", bounds(self.window))
        if self.rows is not None and self.rows < 0:
            raise InvalidContract("rows must be a nonnegative integer")
        if self.tombstones < 0:
            raise InvalidContract("tombstones must be a nonnegative integer")
        if isinstance(self.since, str) and not self.since:
            raise InvalidContract("since must be a nonempty generation token")
        if isinstance(self.generation, str) and not self.generation:
            raise InvalidContract("generation must be a nonempty string")
        if self.content_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", self.content_hash):
            raise InvalidContract("content_hash must be the lowercase hex SHA-256 of the delivered bytes")

    def check(self, contract: Contract) -> ParsedDelivery:
        if not isinstance(contract, Contract):
            raise InvalidContract("manifest validation requires a Contract")
        if self.table != contract.table:
            raise InvalidContract("manifest table differs from contract")
        shape = contract.shape(self.shape)
        if (shape.coverage is Coverage.WINDOW) != (self.window is not None):
            raise InvalidContract("only a window shape supplies a window")
        if self.window is not None and self.generation is not None:
            raise InvalidContract("a window delivery cannot issue a table generation")
        if shape.complete != (self.since is None):
            raise InvalidContract("only changed-only deliveries supply since")
        if shape.checkpoint is Checkpoint.TIMESTAMP and not isinstance(self.since, datetime):
            raise InvalidContract("timestamp checkpoints must be datetimes")
        if shape.checkpoint is Checkpoint.TIMESTAMP and self.since == self.produced_at and self._carries_rows:
            raise InvalidContract("an empty interval carries no rows")
        if shape.checkpoint is Checkpoint.GENERATION and (
            not isinstance(self.since, str) or not self.since or not isinstance(self.generation, str)
        ):
            raise InvalidContract("generation feeds require nonempty since and generation tokens")
        if shape.checkpoint is Checkpoint.SEQUENCE:
            if type(self.since) is not int or type(self.generation) is not int:
                raise InvalidContract("sequence reads require integer since and generation positions")
            if self.generation < self.since:
                raise InvalidContract("a sequence read cannot end before its start")
            if self.generation == self.since and self._carries_rows:
                raise InvalidContract("an empty interval carries no rows")
        positions = any(s.checkpoint is Checkpoint.SEQUENCE for s in contract.shapes.values())
        if self.generation is not None and (type(self.generation) is int) != positions:
            raise InvalidContract("a delivery issues the table's kind of position")
        if self.tombstones and shape.deletions is not Deletions.TOMBSTONE:
            raise InvalidContract("only tombstone shapes carry deleted keys")
        if self.window is not None:
            assert contract.window_column is not None
            if type(self.window[0]) is not LOGICAL_TYPES[contract.types[contract.window_column]].python_type:
                raise InvalidContract("window bound types differ from the contract types")
        if shape.complete:
            return (
                FullDelivery(self, shape) if self.window is None else WindowDelivery(self, shape, self.window)
            )
        if isinstance(self.since, str):
            assert isinstance(self.generation, str)
            return GenerationChanges(self, shape, self.since, self.generation)
        if type(self.since) is int:
            assert type(self.generation) is int
            return SequenceChanges(self, shape, self.since, self.generation)
        assert isinstance(self.since, datetime)
        if not contract.key:
            return AppendInterval(self, shape, self.since)
        coverage = WindowCoverage(self.window) if self.window is not None else AllCoverage()
        return TimestampChanges(self, shape, self.since, coverage)

    @property
    def _carries_rows(self) -> bool:
        return bool(self.rows or self.tombstones)


@dataclass(frozen=True)
class AllCoverage:
    pass


@dataclass(frozen=True)
class WindowCoverage:
    window: Window


DeliveryCoverage: TypeAlias = AllCoverage | WindowCoverage


@dataclass(frozen=True)
class FullDelivery:
    manifest: Manifest
    shape: Shape


@dataclass(frozen=True)
class WindowDelivery:
    manifest: Manifest
    shape: Shape
    window: Window


@dataclass(frozen=True)
class TimestampChanges:
    manifest: Manifest
    shape: Shape
    since: datetime
    coverage: DeliveryCoverage


@dataclass(frozen=True)
class GenerationChanges:
    manifest: Manifest
    shape: Shape
    since: str
    generation: str


@dataclass(frozen=True)
class AppendInterval:
    manifest: Manifest
    shape: Shape
    since: datetime


@dataclass(frozen=True)
class SequenceChanges:
    manifest: Manifest
    shape: Shape
    since: int
    through: int


ParsedDelivery: TypeAlias = (
    FullDelivery | WindowDelivery | TimestampChanges | GenerationChanges | AppendInterval | SequenceChanges
)


def coverage_window(delivery: ParsedDelivery) -> Window | None:
    """The window a delivery speaks about, or None for the whole table."""
    match delivery:
        case WindowDelivery(window=window) | TimestampChanges(coverage=WindowCoverage(window)):
            return window
        case FullDelivery() | TimestampChanges() | GenerationChanges() | AppendInterval() | SequenceChanges():
            return None
        case _:
            assert_never(delivery)


def cursor_interval(
    contract: Contract, delivery: ParsedDelivery
) -> tuple[datetime, datetime] | tuple[int, int] | None:
    """The interval (low, high] every cursor value must fall in, when the cursor column checks this read."""
    kind = contract.types.get(contract.cursor_column or "")
    match delivery:
        case TimestampChanges(since=since) | AppendInterval(since=since) if kind == "datetime":
            return since, delivery.manifest.produced_at
        case SequenceChanges(since=low, through=high) if kind == "int":
            return low, high
        case _:
            return None
