"""Publication decisions and executable row-operation values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .contract import (
    AppendInterval,
    Contract,
    FullDelivery,
    Manifest,
    Window,
    WindowDelivery,
    bounds,
    coverage_window,
    identifier,
)
from .errors import InvalidContract, RejectedDelivery
from .ledger import Ledger, Region
from .wire import Wire, utc


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
                if delivery.part.tombstones:
                    actions += (Action(Operation.TOMBSTONE),)
        return cls(contract.table, contract.key, contract.window_column, actions)


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
    """One decision on one table: what the read declared about it, the checks, what changed, and where
    the decision sits in the chain.

    `manifest` holds the table's part of the read alone; `read_identity` names the whole read. The ledger
    is the fold of accepted receipts; a receipt names the ledger it leaves by identity, so its size follows
    the delivery's coverage, never the table's history.
    """

    manifest: Manifest
    contract: Contract
    checks: tuple[Check, ...]
    outcome: Outcome
    transition: Transition | None
    ledger_identity: str
    publication_id: str | None
    received_at: datetime
    read_identity: str
    source: str | None = None
    base_publication_id: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.manifest.tables != (self.contract.table,):
            raise InvalidContract("a receipt decides one table: its manifest holds that table's part alone")
        if self.accepted != (self.transition is not None):
            raise InvalidContract("only accepted receipts contain a transition")
        if self.accepted and not all(c.passed for c in self.checks):
            raise InvalidContract("accepted receipts require passing checks")
        if self.accepted and self.manifest.produced_at > self.received_at:
            raise InvalidContract("accepted receipts cannot attest a future source boundary")
        if not self.accepted and self.base_publication_id != self.publication_id:
            raise InvalidContract("rejected receipts retain their base publication")
        if not self.ledger_identity or not self.read_identity or self.publication_id == "":
            raise InvalidContract("a receipt's identities are never empty")

    @property
    def accepted(self) -> bool:
        return self.outcome is not Outcome.REJECTED

    @property
    def replayed(self) -> bool:
        return self.outcome is Outcome.REPLAYED


class Gate:
    """Decides deliveries from declarations and the ledger; the receiver adds its own checks on the rows."""

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
    def together(alone: tuple[Receipt, ...]) -> Check:
        """The joint check of a group: one read, every table accepted alone.

        The passed check names the read, so a receipt carrying it was accepted with exactly that read.
        """
        reads = sorted((receipt.contract.table, receipt.read_identity) for receipt in alone)
        if len({read for _, read in reads}) != 1:
            named = ", ".join(f"{table} {read}" for table, read in reads)
            return Check("together", False, f"receipts of different reads: {named}")
        refused = sorted(receipt.contract.table for receipt in alone if not receipt.accepted)
        if refused:
            return Check("together", False, f"refused with {', '.join(refused)}")
        tables = ", ".join(table for table, _ in reads)
        boundary = alone[0].manifest.produced_at.isoformat()
        return Check("together", True, f"{tables} as of {boundary}, read {reads[0][1]}")

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
        own = manifest.only(ledger.contract.table)
        if not checks or all(check.passed for check in checks):
            raise InvalidContract("a rejection needs a failed check")
        return Receipt(
            own,
            ledger.contract,
            checks,
            Outcome.REJECTED,
            None,
            ledger.identity,
            ledger.publication_id,
            now,
            manifest.identity,
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
        """Decide the ledger's table from a read: the part the read declares about it.

        `outcomes` are the checks the receiver ran on the rows against the manifest, recorded as its own;
        `joint` holds the checks of the group the table travels with. A failed check of either rejects.
        """
        now = utc(now)
        own = manifest.only(ledger.contract.table)
        if not isinstance(outcomes, (tuple, list)) or any(not isinstance(c, Check) for c in outcomes):
            raise InvalidContract("row outcomes must be Check values")
        if any(not isinstance(c, Check) for c in joint):
            raise InvalidContract("joint outcomes must be Check values")
        checks = cls.preflight(ledger, own, now=now)
        if all(c.passed for c in checks):
            checks += tuple(outcomes)
        checks += tuple(joint)
        if not all(c.passed for c in checks):
            return cls.reject(ledger, manifest, checks, now=now, source=source)
        after = ledger.apply(own)
        coverage = coverage_window(own.check(ledger.contract))
        transition = Transition(ApplyPlan.build(ledger.contract, own), after.covered(coverage))
        return Receipt(
            own,
            ledger.contract,
            checks,
            Outcome.ACCEPTED,
            transition,
            after.identity,
            after.publication_id,
            now,
            manifest.identity,
            source=source,
            base_publication_id=ledger.publication_id,
        )
