"""Publication decisions and executable row-operation values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .contract import Contract, Coverage, Manifest, Scalar, Window
from .ledger import Claims, Ledger, Region, RejectedDelivery
from .wire import InvalidContract, Wire


class Operation(StrEnum):
    REPLACE_ALL = "replace_all"
    REPLACE_WINDOW = "replace_window"
    UPSERT = "upsert"
    TOMBSTONE = "tombstone"
    APPEND = "append"


@dataclass(frozen=True)
class Action(Wire):
    operation: Operation
    window: Window | None = None
    keys: tuple[tuple[Scalar, ...], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "operation", Operation(self.operation))
        object.__setattr__(self, "keys", tuple(tuple(k) for k in self.keys))


@dataclass(frozen=True)
class ApplyPlan(Wire):
    table: str
    key: tuple[str, ...]
    window_column: str | None
    actions: tuple[Action, ...]

    def __post_init__(self):
        object.__setattr__(self, "key", tuple(self.key))
        object.__setattr__(self, "actions", tuple(self.actions))

    @classmethod
    def build(cls, contract: Contract, manifest: Manifest) -> ApplyPlan:
        shape = manifest.check(contract)
        if not contract.key:
            actions = (Action(Operation.APPEND),)
        elif shape.complete:
            actions = (
                Action(Operation.REPLACE_WINDOW, manifest.window)
                if manifest.window is not None
                else Action(Operation.REPLACE_ALL),
            )
        else:
            actions = (Action(Operation.UPSERT, keys=manifest.keys),)
            if manifest.tombstones:
                actions += (Action(Operation.TOMBSTONE, keys=manifest.tombstones),)
        return cls(contract.table, contract.key, contract.window_column, actions)


@dataclass(frozen=True)
class CheckRequirement(Wire):
    name: str


@dataclass(frozen=True)
class Check(Wire):
    name: str
    passed: bool
    detail: str = ""

    def __post_init__(self):
        if (
            type(self.passed) is not bool
            or not isinstance(self.name, str)
            or not isinstance(self.detail, str)
        ):
            raise InvalidContract("a check has a name, boolean outcome and text detail")


@dataclass(frozen=True)
class Transition(Wire):
    before: tuple[Region, ...]
    after: tuple[Region, ...]
    plan: ApplyPlan


@dataclass(frozen=True)
class Receipt(Wire):
    manifest: Manifest
    checks: tuple[Check, ...]
    accepted: bool
    transition: Transition | None
    ledger: Ledger
    claims: Claims
    publication_id: str | None

    def __post_init__(self):
        object.__setattr__(self, "checks", tuple(self.checks))
        if self.accepted != (self.transition is not None):
            raise InvalidContract("only accepted receipts contain a transition")
        if self.accepted and not all(c.passed for c in self.checks):
            raise InvalidContract("accepted receipts require passing checks")
        if self.publication_id != self.ledger.publication_id:
            raise InvalidContract("receipt publication must identify its ledger")


class Gate:
    @staticmethod
    def requirements(contract: Contract, manifest: Manifest) -> tuple[CheckRequirement, ...]:
        shape = manifest.check(contract)
        names = ["row_count", "schema_hash"]
        if manifest.window is not None:
            names.append("rows_in_window")
        if contract.key:
            names.append("key_integrity")
        if shape.coverage is Coverage.KEYS or manifest.keys or manifest.tombstones:
            names.append("named_keys")
        if shape.complete and contract.max_shrink_fraction is not None:
            names.append("shrink_tolerance")
        return tuple(CheckRequirement(n) for n in names)

    @staticmethod
    def preflight(ledger: Ledger, manifest: Manifest) -> tuple[Check, ...]:
        try:
            manifest.check(ledger.contract)
        except (InvalidContract, TypeError, ValueError) as exc:
            return (Check("manifest", False, str(exc)),)
        try:
            ledger.apply(manifest)
        except (InvalidContract, RejectedDelivery, TypeError, ValueError) as exc:
            return (Check("manifest", True), Check("transition", False, str(exc)))
        return (Check("manifest", True), Check("transition", True))

    @classmethod
    def evaluate(
        cls,
        ledger: Ledger,
        manifest: Manifest,
        outcomes: tuple[Check, ...] = (),
        *,
        now: datetime | None = None,
    ) -> Receipt:
        checks = cls.preflight(ledger, manifest)
        if all(c.passed for c in checks):
            expected = {r.name for r in cls.requirements(ledger.contract, manifest)}
            names = [c.name for c in outcomes]
            checks += outcomes
            checks += (
                Check(
                    "check_set",
                    len(names) == len(set(names)) and set(names) == expected,
                    "one outcome per required row check",
                ),
            )
        accepted = all(c.passed for c in checks)
        after = ledger.apply(manifest) if accepted else ledger
        transition = (
            Transition(ledger.regions, after.regions, ApplyPlan.build(ledger.contract, manifest))
            if accepted
            else None
        )
        return Receipt(
            manifest,
            checks,
            accepted,
            transition,
            after,
            after.claims(now or manifest.produced_at),
            after.publication_id,
        )
