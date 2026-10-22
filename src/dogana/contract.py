"""Receiver declarations and source claims, independent of rows and storage."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from hashlib import sha256

from .wire import InvalidContract, Wire, utc

Scalar = str | int | date | datetime
Window = tuple[Scalar, Scalar]


class Coverage(StrEnum):
    ALL = "all"
    WINDOW = "window"
    KEYS = "keys"


class Completeness(StrEnum):
    COMPLETE = "complete"
    CHANGED_ONLY = "changed_only"


class Deletions(StrEnum):
    ABSENCE = "absence"
    TOMBSTONE = "tombstone"
    NONE = "none"


class Checkpoint(StrEnum):
    GENERATION = "generation"
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
class Column(Wire):
    name: str
    type: str

    def __post_init__(self) -> None:
        super().__post_init__()
        identifier(self.name)
        if self.type not in ("int", "float", "str", "bool", "date", "datetime"):
            raise InvalidContract("unsupported logical column type")


@dataclass(frozen=True)
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
        if self.complete != (self.checkpoint is Checkpoint.NONE):
            raise InvalidContract("complete deliveries have no checkpoint; changed-only deliveries have one")
        if self.complete != (self.deletions is Deletions.ABSENCE):
            raise InvalidContract("only complete deliveries signal deletions by absence")
        if self.coverage is Coverage.KEYS and self.complete:
            raise InvalidContract("named-key coverage describes a change stream")
        if self.coverage is Coverage.WINDOW and self.checkpoint is Checkpoint.GENERATION:
            raise InvalidContract("a generation checkpoint covers the whole table")
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
    def complete(self) -> bool:
        return self.completeness is Completeness.COMPLETE


@dataclass(frozen=True)
class Contract(Wire):
    table: str
    key: tuple[str, ...]
    shapes: Mapping[str, Shape]
    window_column: str | None = None
    schema: tuple[Column, ...] = ()
    max_shrink_fraction: float | None = 0.5
    freshness: timedelta = timedelta(days=7)
    start_at: datetime | None = None
    cursor_column: str | None = None

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
        names = [c.name for c in self.schema]
        if len(set(names)) != len(names):
            raise InvalidContract("schema columns must be distinct Column values")
        bound_columns = set(self.key)
        if self.window_column is not None:
            bound_columns.add(self.window_column)
        if self.schema and not bound_columns <= set(names):
            raise InvalidContract("key and window columns must be in the schema")
        if self.cursor_column is not None:
            identifier(self.cursor_column)
            if not any(c.name == self.cursor_column and c.type == "datetime" for c in self.schema):
                raise InvalidContract("cursor_column must name a datetime schema column")
        if any(
            c.type not in ("int", "str", "date", "datetime")
            for c in self.schema
            if c.name in self.key or c.name == self.window_column
        ):
            raise InvalidContract("key and window columns must use ordered scalar logical types")
        if any(
            c.name == self.window_column and c.type not in ("date", "datetime") for c in self.schema
        ) and any(shape.window_size is not None for shape in self.shapes.values()):
            raise InvalidContract("duration windows need a date or datetime column")
        if any(c.name == self.window_column and c.type == "date" for c in self.schema) and any(
            shape.window_size is not None and shape.window_size.total_seconds() % 86400
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
        return {c.name: c.type for c in self.schema if c.name in names} == {
            c.name: c.type for c in other.schema if c.name in names
        }

    def __hash__(self) -> int:
        return hash(
            (
                self.table,
                self.key,
                tuple(sorted(self.shapes.items())),
                self.window_column,
                self.schema,
                self.max_shrink_fraction,
                self.freshness,
                self.start_at,
                self.cursor_column,
            )
        )

    @property
    def schema_hash(self) -> str:
        value = [(c.name, c.type) for c in self.schema]
        return sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Manifest(Wire):
    table: str
    shape: str
    produced_at: datetime
    window: Window | None = None
    since: str | datetime | None = None
    generation: str | None = None
    rows: int | None = None
    schema_hash: str | None = None
    keys: tuple[tuple[Scalar, ...], ...] = ()
    tombstones: tuple[tuple[Scalar, ...], ...] = ()
    content_hash: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        identifier(self.table)
        if not self.shape:
            raise InvalidContract("shape must be a nonempty string")
        object.__setattr__(self, "window", bounds(self.window))
        if self.rows is not None and self.rows < 0:
            raise InvalidContract("rows must be a nonnegative integer")
        if isinstance(self.since, str) and not self.since:
            raise InvalidContract("since must be a nonempty generation token")
        if self.generation is not None and not self.generation:
            raise InvalidContract("generation must be a nonempty string")
        if self.content_hash is not None and not self.content_hash:
            raise InvalidContract("content_hash must be a nonempty string")
        for group in (self.keys, self.tombstones):
            if any(not key for key in group) or len(set(group)) != len(group):
                raise InvalidContract("named keys must be nonempty and unique")
        if set(self.keys) & set(self.tombstones):
            raise InvalidContract("a key cannot be both live and deleted")

    def check(self, contract: Contract) -> Shape:
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
        if shape.checkpoint is Checkpoint.TIMESTAMP and self.since == self.produced_at and self.rows:
            raise InvalidContract("an empty interval carries no rows")
        if shape.checkpoint is Checkpoint.GENERATION and (
            not isinstance(self.since, str) or not self.since or not self.generation
        ):
            raise InvalidContract("generation feeds require nonempty since and generation tokens")
        if self.tombstones and shape.deletions is not Deletions.TOMBSTONE:
            raise InvalidContract("only tombstone shapes carry deleted keys")
        for group in (self.keys, self.tombstones):
            if any(len(key) != len(contract.key) for key in group):
                raise InvalidContract("named keys must have the contract's width")
        types = {"int": int, "str": str, "date": date, "datetime": datetime}
        declared = {c.name: c.type for c in contract.schema}
        if (
            self.window is not None
            and contract.window_column is not None
            and contract.window_column in declared
        ):
            if type(self.window[0]) is not types[declared[contract.window_column]]:
                raise InvalidContract("window bound types differ from the contract schema")
        for key in (*self.keys, *self.tombstones):
            if declared and any(
                type(value) is not types[declared[name]]
                for name, value in zip(contract.key, key, strict=True)
            ):
                raise InvalidContract("named key types differ from the contract schema")
        return shape
