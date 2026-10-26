"""Publication decisions and executable row-operation values."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from .contract import (
    AppendInterval,
    Contract,
    Coverage,
    FullDelivery,
    Manifest,
    Scalar,
    SequenceAppend,
    TimestampChanges,
    Window,
    WindowCoverage,
    WindowDelivery,
    bounds,
    cursor_interval,
    identifier,
)
from .errors import InvalidContract, RejectedDelivery
from .ledger import Claims, Ledger, Region
from .wire import Wire, utc


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

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "window", bounds(self.window))
        if (self.operation is Operation.REPLACE_WINDOW) != (self.window is not None):
            raise InvalidContract("only replace_window actions supply a window")
        if self.keys and self.operation not in (Operation.UPSERT, Operation.TOMBSTONE):
            raise InvalidContract("only key actions supply keys")
        if any(not key for key in self.keys) or len(set(self.keys)) != len(self.keys):
            raise InvalidContract("action keys must be nonempty and distinct")


@dataclass(frozen=True)
class ApplyPlan(Wire):
    table: str
    key: tuple[str, ...]
    window_column: str | None
    actions: tuple[Action, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        identifier(self.table)
        for name in self.key:
            identifier(name)
        if len(set(self.key)) != len(self.key):
            raise InvalidContract("duplicate plan key column")
        if self.window_column is not None:
            identifier(self.window_column)
        for action in self.actions:
            if action.window is not None and self.window_column is None:
                raise InvalidContract("window actions need a window column")
            if action.operation in (Operation.UPSERT, Operation.TOMBSTONE) and not self.key:
                raise InvalidContract("key actions need a key")
            if any(len(key) != len(self.key) for key in action.keys):
                raise InvalidContract("action keys must have the plan key width")

    @classmethod
    def build(cls, contract: Contract, manifest: Manifest) -> ApplyPlan:
        delivery = manifest.check(contract)
        actions: tuple[Action, ...]
        match delivery:
            case AppendInterval() | SequenceAppend():
                actions = (Action(Operation.APPEND),)
            case FullDelivery():
                actions = (Action(Operation.REPLACE_ALL),)
            case WindowDelivery(window=window):
                actions = (Action(Operation.REPLACE_WINDOW, window),)
            case _:
                actions = (Action(Operation.UPSERT, keys=manifest.keys),)
                if manifest.tombstones:
                    actions += (Action(Operation.TOMBSTONE, keys=manifest.tombstones),)
        return cls(contract.table, contract.key, contract.window_column, actions)


@dataclass(frozen=True)
class CheckRequirement(Wire):
    name: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.name:
            raise InvalidContract("a required check needs a name")


@dataclass(frozen=True)
class Check(Wire):
    name: str
    passed: bool
    detail: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.name:
            raise InvalidContract("a check needs a name")


@dataclass(frozen=True)
class Transition(Wire):
    before: tuple[Region, ...]
    after: tuple[Region, ...]
    plan: ApplyPlan

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.before or not self.after:
            raise InvalidContract("a transition needs preceding and resulting regions")


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REPLAYED = "replayed"


@dataclass(frozen=True)
class Receipt(Wire):
    manifest: Manifest
    checks: tuple[Check, ...]
    outcome: Outcome
    transition: Transition | None
    ledger: Ledger
    claims: Claims
    publication_id: str | None
    received_at: datetime
    source: str | None = None
    base_publication_id: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.accepted != (self.transition is not None):
            raise InvalidContract("only accepted receipts contain a transition")
        if self.accepted and not all(c.passed for c in self.checks):
            raise InvalidContract("accepted receipts require passing checks")
        if self.publication_id != self.ledger.publication_id:
            raise InvalidContract("receipt publication must identify its ledger")
        if self.transition is not None and (
            Ledger(self.ledger.contract, self.transition.after).regions != self.ledger.regions
        ):
            raise InvalidContract("receipt transition must establish its ledger regions")
        if self.accepted and self.manifest.produced_at > self.received_at:
            raise InvalidContract("accepted receipts cannot attest a future source boundary")
        if not self.accepted and self.base_publication_id != self.publication_id:
            raise InvalidContract("rejected receipts retain their base publication")

    @property
    def accepted(self) -> bool:
        return self.outcome is not Outcome.REJECTED

    @property
    def replayed(self) -> bool:
        return self.outcome is Outcome.REPLAYED


class Gate:
    @staticmethod
    def requirements(contract: Contract, manifest: Manifest) -> tuple[CheckRequirement, ...]:
        delivery = manifest.check(contract)
        shape = delivery.shape
        names = ["row_count"]
        if contract.schema_hash is not None:
            names.append("schema_hash")
        if isinstance(delivery, WindowDelivery) or (
            isinstance(delivery, TimestampChanges) and isinstance(delivery.coverage, WindowCoverage)
        ):
            names.append("rows_in_window")
        if contract.window_column is not None:
            names.append("window_stability")
        if contract.domain is not None:
            names.append("rows_in_domain")
        if cursor_interval(contract, delivery) is not None:
            names.append("rows_in_interval")
        if contract.key:
            names.append("key_integrity")
        if shape.coverage is Coverage.KEYS or manifest.keys or manifest.tombstones:
            names.append("named_keys")
        if shape.complete and contract.max_shrink_fraction is not None:
            names.append("shrink_tolerance")
        if manifest.content_hash is not None:
            names.append("content_hash")
        return tuple(CheckRequirement(n) for n in names)

    @staticmethod
    def preflight(ledger: Ledger, manifest: Manifest, *, now: datetime) -> tuple[Check, ...]:
        now = utc(now)
        try:
            manifest.check(ledger.contract)
        except InvalidContract as exc:
            return (Check("manifest", False, str(exc)),)
        if manifest.produced_at > now:
            return (
                Check("manifest", True),
                Check("receiver_time", False, "source boundary is after receiver time"),
            )
        try:
            ledger.apply(manifest)
        except (InvalidContract, RejectedDelivery) as exc:
            return (Check("manifest", True), Check("transition", False, str(exc)))
        return (Check("manifest", True), Check("receiver_time", True), Check("transition", True))

    @staticmethod
    def reject(
        ledger: Ledger,
        manifest: Manifest,
        checks: tuple[Check, ...],
        *,
        now: datetime,
        source: str | None = None,
    ) -> Receipt:
        now = utc(now)
        if not checks or all(check.passed for check in checks):
            raise InvalidContract("a rejection needs a failed check")
        return Receipt(
            manifest,
            checks,
            Outcome.REJECTED,
            None,
            ledger,
            ledger.claims(now),
            ledger.publication_id,
            now,
            source=source,
            base_publication_id=ledger.publication_id,
        )

    @classmethod
    def evaluate(
        cls,
        ledger: Ledger,
        manifest: Manifest,
        outcomes: tuple[Check, ...] = (),
        *,
        now: datetime,
        source: str | None = None,
    ) -> Receipt:
        now = utc(now)
        if not isinstance(outcomes, (tuple, list)) or any(not isinstance(c, Check) for c in outcomes):
            raise InvalidContract("row outcomes must be Check values")
        checks = cls.preflight(ledger, manifest, now=now)
        if all(c.passed for c in checks):
            expected = {r.name for r in cls.requirements(ledger.contract, manifest)}
            names = [c.name for c in outcomes]
            checks += tuple(
                replace(outcome, passed=False, detail="manifest and contract schema hashes differ")
                if outcome.name == "schema_hash"
                and outcome.passed
                and manifest.schema_hash != ledger.contract.schema_hash
                else outcome
                for outcome in outcomes
            )
            checks += (
                Check(
                    "check_set",
                    len(names) == len(set(names)) and set(names) == expected,
                    "one outcome per required row check",
                ),
            )
        if not all(c.passed for c in checks):
            return cls.reject(ledger, manifest, checks, now=now, source=source)
        after = ledger.apply(manifest)
        plan = ApplyPlan.build(ledger.contract, manifest)
        transition = Transition(ledger.regions, after.regions, plan)
        return Receipt(
            manifest,
            checks,
            Outcome.ACCEPTED,
            transition,
            after,
            after.claims(now),
            after.publication_id,
            now,
            source=source,
            base_publication_id=ledger.publication_id,
        )
