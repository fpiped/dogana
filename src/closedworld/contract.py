"""The delivery contract of a table, the shapes it admits and the manifest of one delivery.
See docs/protocol.md."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Coverage(str, Enum):
    ALL = "all"
    WINDOW = "window"
    KEYS = "keys"


class Completeness(str, Enum):
    COMPLETE = "complete"
    CHANGED_ONLY = "changed_only"


class Deletions(str, Enum):
    ABSENCE = "absence"
    TOMBSTONE = "tombstone"
    NONE = "none"


class Checkpoint(str, Enum):
    GENERATION = "generation"
    TIMESTAMP = "timestamp"
    NONE = "none"


class InvalidContract(ValueError):
    """The declaration or the manifest is not one the protocol admits."""


@dataclass(frozen=True)
class Shape:
    """How one kind of delivery is produced: the four properties and the rules that bind them."""

    coverage: Coverage
    completeness: Completeness
    deletions: Deletions
    checkpoint: Checkpoint

    def __post_init__(self) -> None:
        if self.complete != (self.checkpoint is Checkpoint.NONE):
            raise InvalidContract("a delivery is complete exactly when it has no checkpoint")
        if self.complete and self.deletions is not Deletions.ABSENCE:
            raise InvalidContract("a complete delivery signals deletions by absence")
        if not self.complete and self.deletions is Deletions.ABSENCE:
            raise InvalidContract("absence proves nothing in a changed-only delivery")
        if self.coverage is Coverage.KEYS and self.complete:
            raise InvalidContract("a delivery of named keys is complete only for those keys")

    @property
    def complete(self) -> bool:
        return self.completeness is Completeness.COMPLETE

    @property
    def append_only(self) -> bool:
        return not self.complete and self.deletions is Deletions.NONE


@dataclass(frozen=True)
class Contract:
    """A table's identity and the named shapes its deliveries may take."""

    table: str
    key: tuple[str, ...]
    shapes: dict[str, Shape] = field(compare=True)
    window_column: str | None = None

    def __post_init__(self) -> None:
        if not self.shapes:
            raise InvalidContract("a contract admits at least one shape")
        windowed = any(s.coverage is Coverage.WINDOW for s in self.shapes.values())
        if windowed != (self.window_column is not None):
            raise InvalidContract("a window shape needs the window column; without one the column is meaningless")
        if not self.key and not all(s.append_only for s in self.shapes.values()):
            raise InvalidContract("without a key a delivery can only append")

    def shape(self, name: str) -> Shape:
        try:
            return self.shapes[name]
        except KeyError:
            raise InvalidContract(f"{self.table} admits no shape named {name!r}") from None


@dataclass(frozen=True)
class Manifest:
    """What one delivery claims. `window` bounds are inclusive and compared on the contract's column."""

    table: str
    shape: str
    produced_at: datetime
    window: tuple[Any, Any] | None = None
    since: Any = None
    generation: str | None = None
    rows: int | None = None
    schema_hash: str | None = None

    def check(self, contract: Contract) -> Shape:
        """The shape of the claim against the contract; the rows are the adapter's business."""
        if self.table != contract.table:
            raise InvalidContract(f"manifest for {self.table} offered to the contract of {contract.table}")
        shape = contract.shape(self.shape)
        if (shape.coverage is Coverage.WINDOW) != (self.window is not None):
            raise InvalidContract("a window manifest for a window shape, and only then")
        if self.window is not None and self.window[1] < self.window[0]:
            raise InvalidContract("window high before low")
        if shape.complete != (self.since is None):
            raise InvalidContract("a changed-only delivery states what it changed since; a complete one does not")
        return shape
