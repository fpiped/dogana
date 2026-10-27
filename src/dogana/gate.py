"""Publication decisions and executable row-operation values."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING

from .contract import (
    AppendInterval,
    Contract,
    FullDelivery,
    Manifest,
    TimestampChanges,
    Window,
    WindowCoverage,
    WindowDelivery,
    bounds,
    coverage_window,
    cursor_interval,
    identifier,
)
from .errors import InvalidContract, RejectedDelivery
from .ledger import Ledger, Region
from .wire import Wire, utc

if TYPE_CHECKING:
    from .group import Reference


class Operation(StrEnum):
    REPLACE_ALL = "replace_all"
    REPLACE_WINDOW = "replace_window"
    UPSERT = "upsert"
    TOMBSTONE = "tombstone"
    APPEND = "append"


@dataclass(frozen=True)
class Action(Wire):
    """One row operation; upsert and tombstone take their keys from the candidate's live and deleted rows."""

    operation: Operation
    window: Window | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "window", bounds(self.window))
        if (self.operation is Operation.REPLACE_WINDOW) != (self.window is not None):
            raise InvalidContract("only replace_window actions supply a window")


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

    @classmethod
    def build(cls, contract: Contract, manifest: Manifest) -> ApplyPlan:
        delivery = manifest.check(contract)
        actions: tuple[Action, ...]
        match delivery:
            case AppendInterval():
                actions = (Action(Operation.APPEND),)
            case FullDelivery():
                actions = (Action(Operation.REPLACE_ALL),)
            case WindowDelivery(window=window):
                actions = (Action(Operation.REPLACE_WINDOW, window),)
            case _:
                actions = (Action(Operation.UPSERT),)
                if manifest.tombstones:
                    actions += (Action(Operation.TOMBSTONE),)
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
    """What an accepted delivery did: its plan, and the regions over its coverage afterwards."""

    plan: ApplyPlan
    regions: tuple[Region, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.regions:
            raise InvalidContract("a transition needs the regions it establishes")


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REPLAYED = "replayed"


@dataclass(frozen=True)
class Receipt(Wire):
    """One decision: what was delivered and checked, what it changed, and where it sits in the chain.

    The ledger is the fold of accepted receipts; a receipt names the ledger it leaves by identity, so
    its size follows the delivery's coverage, never the table's history.
    """

    manifest: Manifest
    contract: Contract
    checks: tuple[Check, ...]
    outcome: Outcome
    transition: Transition | None
    ledger_identity: str
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
        if cursor_interval(contract, delivery) is not None:
            names.append("rows_in_interval")
        if contract.key:
            names.append("key_integrity")
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
    def together(alone: tuple[Receipt, ...], references: tuple[Reference, ...] = ()) -> Check:
        """The joint check of a group: one read boundary, and every table accepted on its own."""
        reads = sorted((receipt.contract.table, receipt.manifest.produced_at) for receipt in alone)
        refused = sorted(receipt.contract.table for receipt in alone if not receipt.accepted)
        if len({boundary for _, boundary in reads}) != 1:
            boundaries = ", ".join(f"{table} {boundary.isoformat()}" for table, boundary in reads)
            return Check("together", False, f"read boundaries differ: {boundaries}")
        if refused:
            return Check("together", False, f"refused with {', '.join(refused)}")
        return Gate.delivered(tuple((r.contract.table, r.manifest) for r in alone), references)

    @staticmethod
    def delivered(reads: tuple[tuple[str, Manifest], ...], references: tuple[Reference, ...] = ()) -> Check:
        """The passed joint check of these manifests decided together under these references.

        Its digest names the delivery, so a receipt carrying it was accepted with exactly these files.
        """
        tables = ", ".join(sorted(table for table, _ in reads))
        boundary = min(manifest.produced_at for _, manifest in reads)
        named = {
            "manifests": sorted([table, manifest.identity] for table, manifest in reads),
            "references": sorted(str(reference) for reference in references),
        }
        digest = sha256(json.dumps(named, separators=(",", ":")).encode()).hexdigest()
        return Check("together", True, f"{tables} as of {boundary.isoformat()}, delivery {digest}")

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
            ledger.contract,
            checks,
            Outcome.REJECTED,
            None,
            ledger.identity,
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
        joint: tuple[Check, ...] = (),
    ) -> Receipt:
        """Decide one delivery; `joint` holds the checks of the group it travels with."""
        now = utc(now)
        if not isinstance(outcomes, (tuple, list)) or any(not isinstance(c, Check) for c in outcomes):
            raise InvalidContract("row outcomes must be Check values")
        if any(not isinstance(c, Check) for c in joint):
            raise InvalidContract("joint outcomes must be Check values")
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
        checks += tuple(joint)
        if not all(c.passed for c in checks):
            return cls.reject(ledger, manifest, checks, now=now, source=source)
        after = ledger.apply(manifest)
        coverage = coverage_window(manifest.check(ledger.contract))
        transition = Transition(ApplyPlan.build(ledger.contract, manifest), after.covered(coverage))
        return Receipt(
            manifest,
            ledger.contract,
            checks,
            Outcome.ACCEPTED,
            transition,
            after.identity,
            after.publication_id,
            now,
            source=source,
            base_publication_id=ledger.publication_id,
        )
