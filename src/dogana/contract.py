"""Receiver declarations and source claims, independent of rows and storage."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType

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
    if len(value) != 2 or type(value[0]) is not type(value[1]):
        raise InvalidContract("a window has two bounds of one scalar type")
    if type(value[0]) not in (str, int, date, datetime):
        raise InvalidContract("window bounds must be strings, integers, dates or datetimes")
    low, high = value
    if isinstance(low, datetime):
        low, high = utc(low), utc(high)
    if high < low:
        raise InvalidContract("window high before low")
    return low, high


@dataclass(frozen=True)
class Column(Wire):
    name: str
    type: str

    def __post_init__(self):
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

    def __post_init__(self):
        try:
            for name, enum in (
                ("coverage", Coverage),
                ("completeness", Completeness),
                ("deletions", Deletions),
                ("checkpoint", Checkpoint),
            ):
                object.__setattr__(self, name, enum(getattr(self, name)))
        except (TypeError, ValueError) as exc:
            raise InvalidContract("shape properties must use the protocol's closed vocabularies") from exc
        if self.complete != (self.checkpoint is Checkpoint.NONE):
            raise InvalidContract("complete deliveries have no checkpoint; changed-only deliveries have one")
        if self.complete != (self.deletions is Deletions.ABSENCE):
            raise InvalidContract("only complete deliveries signal deletions by absence")
        if self.coverage is Coverage.KEYS and self.complete:
            raise InvalidContract("named-key coverage describes a change stream")
        if type(self.cost) not in (int, float) or not 0 <= self.cost < float("inf"):
            raise InvalidContract("cost must be finite and nonnegative")
        if self.window_size is not None and (
            self.coverage is not Coverage.WINDOW
            or not isinstance(self.window_size, timedelta)
            or self.window_size <= timedelta(0)
        ):
            raise InvalidContract("a positive window_size belongs to a window shape")

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
    max_shrink_fraction: float | None = None
    freshness: timedelta = timedelta(days=7)
    start_at: datetime | None = None

    def __post_init__(self):
        identifier(self.table)
        object.__setattr__(self, "key", tuple(self.key))
        object.__setattr__(self, "schema", tuple(self.schema))
        object.__setattr__(self, "shapes", MappingProxyType(dict(self.shapes)))
        if not self.shapes or not all(
            isinstance(s, Shape) and isinstance(n, str) and n for n, s in self.shapes.items()
        ):
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
        names = [c.name for c in self.schema if isinstance(c, Column)]
        if len(names) != len(self.schema) or len(set(names)) != len(names):
            raise InvalidContract("schema columns must be distinct Column values")
        if self.schema and not set((*self.key, *([self.window_column] if windowed else []))) <= set(names):
            raise InvalidContract("key and window columns must be in the schema")
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
        if not isinstance(self.freshness, timedelta) or self.freshness < timedelta(0):
            raise InvalidContract("freshness must be a nonnegative duration")
        if self.max_shrink_fraction is not None and (
            type(self.max_shrink_fraction) not in (int, float) or not 0 <= self.max_shrink_fraction <= 1
        ):
            raise InvalidContract("max_shrink_fraction must be between zero and one")
        if self.start_at is not None:
            object.__setattr__(self, "start_at", utc(self.start_at))
        if not self.key:
            if self.start_at is None or any(
                s.complete
                or s.deletions is not Deletions.NONE
                or s.checkpoint is not Checkpoint.TIMESTAMP
                or s.coverage is not Coverage.ALL
                for s in self.shapes.values()
            ):
                raise InvalidContract(
                    "append-only history needs start_at and timestamp shapes without deletes"
                )
        elif self.start_at is not None:
            raise InvalidContract("start_at belongs to append-only history")

    def shape(self, name: str) -> Shape:
        try:
            return self.shapes[name]
        except KeyError:
            raise InvalidContract(f"{self.table} admits no shape named {name!r}") from None

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

    def __post_init__(self):
        object.__setattr__(self, "produced_at", utc(self.produced_at))
        if isinstance(self.since, datetime):
            object.__setattr__(self, "since", utc(self.since))
        for name in ("keys", "tombstones"):
            object.__setattr__(
                self,
                name,
                tuple(
                    tuple(utc(v) if isinstance(v, datetime) else v for v in key)
                    for key in getattr(self, name)
                ),
            )
        if self.window is not None:
            object.__setattr__(
                self, "window", tuple(utc(v) if isinstance(v, datetime) else v for v in self.window)
            )

    def check(self, contract: Contract) -> Shape:
        if self.table != contract.table:
            raise InvalidContract("manifest table differs from contract")
        shape = contract.shape(self.shape)
        if (shape.coverage is Coverage.WINDOW) != (self.window is not None):
            raise InvalidContract("only a window shape supplies a window")
        bounds(self.window)
        if shape.complete != (self.since is None):
            raise InvalidContract("only changed-only deliveries supply since")
        if shape.checkpoint is Checkpoint.TIMESTAMP and not isinstance(self.since, datetime):
            raise InvalidContract("timestamp checkpoints must be datetimes")
        if shape.checkpoint is Checkpoint.GENERATION and (
            not isinstance(self.since, str) or not self.since or not self.generation
        ):
            raise InvalidContract("generation feeds require nonempty since and generation tokens")
        if self.generation is not None and (not isinstance(self.generation, str) or not self.generation):
            raise InvalidContract("generation must be a nonempty string")
        if self.rows is not None and (type(self.rows) is not int or self.rows < 0):
            raise InvalidContract("rows must be a nonnegative integer")
        if self.schema_hash is not None and not isinstance(self.schema_hash, str):
            raise InvalidContract("schema_hash must be a string")
        if self.tombstones and shape.deletions is not Deletions.TOMBSTONE:
            raise InvalidContract("only tombstone shapes carry deleted keys")
        for group in (self.keys, self.tombstones):
            if len(set(group)) != len(group) or any(
                len(k) != len(contract.key) or not k or any(v is None for v in k) for k in group
            ):
                raise InvalidContract("named keys must be unique, non-null and have the contract's width")
        types = {"int": int, "str": str, "date": date, "datetime": datetime}
        declared = {c.name: c.type for c in contract.schema}
        for key in (*self.keys, *self.tombstones):
            if any(type(value) not in (str, int, date, datetime) for value in key):
                raise InvalidContract("named key components must be ordered scalars")
            if declared and any(
                type(value) is not types[declared[name]]
                for name, value in zip(contract.key, key, strict=True)
            ):
                raise InvalidContract("named key types differ from the contract schema")
            for value in key:
                if isinstance(value, datetime):
                    utc(value)
        if set(self.keys) & set(self.tombstones):
            raise InvalidContract("a key cannot be both live and deleted")
        return shape
