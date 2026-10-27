"""One delivery sequence over atomic storage primitives."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from os import PathLike, fsdecode
from typing import ParamSpec, TypeVar, cast

from .contract import Contract, FullDelivery, Manifest
from .errors import InconsistentPublication, InvalidContract, LedgerAhead, RejectedDelivery, StoreError
from .gate import Check, Gate, Outcome, Receipt
from .group import Group
from .interfaces import Store, TogetherStore
from .ledger import Ledger
from .wire import utc

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _call(operation: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    try:
        return operation(*args, **kwargs)
    except (InvalidContract, RejectedDelivery, StoreError):
        raise
    except Exception as exc:
        raise StoreError(str(exc)) from exc


def _base(store: Store, contract: Contract, manifest: Manifest) -> Ledger:
    try:
        return _call(store.load_ledger, contract)
    except LedgerAhead:
        if not isinstance(manifest.check(contract), FullDelivery):
            raise
        if _call(store.accepted_receipt, manifest.identity) is not None:
            raise
        return Ledger.from_receipts(contract, _call(store.receipts, contract.table))


def _description(source: object) -> str | None:
    return fsdecode(source) if isinstance(source, (str, bytes, PathLike)) else None


def _replay(
    store: Store, contract: Contract, source: object, previous: Receipt, ledger: Ledger, now: datetime
) -> Receipt:
    """A stored acceptance answers for a declared digest only once the store has hashed these bytes."""
    manifest, description = previous.manifest, _description(source)
    candidate = _call(store.hold, source, manifest, contract)
    try:
        checks = _call(store.check, candidate, contract, manifest, Gate.requirements(contract, manifest))
    finally:
        _call(store.abort, candidate)
    if all(check.passed for check in checks if check.name == "content_hash"):
        return replace(previous, outcome=Outcome.REPLAYED)
    refused = Gate.reject(ledger, manifest, checks, now=now, source=description)
    _call(store.publish, None, refused)
    return refused


def deliver(store: Store, contract: Contract, source: object, manifest: Manifest, now: datetime) -> Receipt:
    """Check and publish once, retaining refusals and propagating storage failures."""
    now = utc(now)
    description = _description(source)
    ledger = _base(store, contract, manifest)
    previous = _call(store.accepted_receipt, manifest.identity)
    if previous is not None and manifest.content_hash is not None:
        return _replay(store, contract, source, previous, ledger, now)
    if previous is not None:
        return replace(previous, outcome=Outcome.REPLAYED)
    if not all(check.passed for check in Gate.preflight(ledger, manifest, now=now)):
        refused = Gate.evaluate(ledger, manifest, now=now, source=description)
        _call(store.publish, None, refused)
        return refused

    candidate: str | None = None
    checks: tuple[Check, ...] = ()
    try:
        candidate = _call(store.hold, source, manifest, contract)
        checks = _call(store.check, candidate, contract, manifest, Gate.requirements(contract, manifest))
        receipt = Gate.evaluate(ledger, manifest, checks, now=now, source=description)
        _call(store.publish, candidate, receipt)
        return receipt
    except BaseException as error:
        cleanup_error: Exception | None = None
        if candidate is not None:
            try:
                _call(store.abort, candidate)
            except Exception as exc:
                cleanup_error = exc
        if not isinstance(error, Exception):
            raise
        committed = _call(store.accepted_receipt, manifest.identity)
        if committed is not None:
            _call(store.load_ledger, contract)
            return committed
        if cleanup_error is not None:
            raise StoreError("candidate cleanup failed") from cleanup_error
        if not isinstance(error, (InvalidContract, RejectedDelivery)):
            raise
        current = _base(store, contract, manifest)
        refused = Gate.reject(
            current, manifest, (*checks, Check("execution", False, str(error))), now=now, source=description
        )
        _call(store.publish, None, refused)
        return refused


def _together(store: Store, group: Group) -> TogetherStore:
    names = ("publish_together", "check_references") if group.references else ("publish_together",)
    if not all(callable(getattr(store, name, None)) for name in names):
        raise StoreError("the store cannot publish tables together")
    return cast(TogetherStore, store)


def deliver_together(
    store: Store, group: Group, deliveries: Mapping[str, tuple[object, Manifest]], now: datetime
) -> dict[str, Receipt]:
    """Check one delivery per table of a group, then publish all of them in one step or refuse them all."""
    now = utc(now)
    together = _together(store, group)
    if set(deliveries) != set(group.tables):
        raise InvalidContract("a delivery together carries one source per table of its group")
    members = [(group.contract(table), *deliveries[table]) for table in group.tables]
    described = {contract.table: _description(source) for contract, source, _ in members}
    ledgers = {contract.table: _base(store, contract, manifest) for contract, _, manifest in members}
    previous = _accepted(store, members)
    if any(receipt is not None for receipt in previous.values()):
        return _replay_together(store, members, previous, ledgers, now)

    candidates: dict[str, str] = {}
    checks: dict[str, tuple[Check, ...]] = {}
    try:
        alone: list[Receipt] = []
        for contract, source, manifest in members:
            table = contract.table
            if all(check.passed for check in Gate.preflight(ledgers[table], manifest, now=now)):
                candidates[table] = _call(store.hold, source, manifest, contract)
                requirements = Gate.requirements(contract, manifest)
                checks[table] = _call(store.check, candidates[table], contract, manifest, requirements)
            outcomes = checks.get(table, ())
            alone.append(Gate.evaluate(ledgers[table], manifest, outcomes, now=now, source=described[table]))
        joint: tuple[Check, ...] = (Gate.together(tuple(alone)),)
        if joint[0].passed and group.references:
            joint += (_call(together.check_references, group, candidates),)
        receipts = {
            contract.table: Gate.evaluate(
                ledgers[contract.table],
                manifest,
                checks.get(contract.table, ()),
                now=now,
                source=described[contract.table],
                joint=joint,
            )
            for contract, _, manifest in members
        }
        if all(receipt.accepted for receipt in receipts.values()):
            _call(together.publish_together, group, tuple((candidates[t], receipts[t]) for t in group.tables))
            return receipts
    except BaseException as error:
        cleanup_error: Exception | None = None
        for candidate in candidates.values():
            try:
                _call(store.abort, candidate)
            except Exception as exc:
                cleanup_error = exc
        if not isinstance(error, Exception):
            raise
        committed = _accepted(store, members)
        if all(receipt is not None for receipt in committed.values()):
            for contract, _, _ in members:
                _call(store.load_ledger, contract)
            return cast(dict[str, Receipt], committed)
        if any(receipt is not None for receipt in committed.values()):
            raise InconsistentPublication("a group publication committed only some of its tables") from error
        if cleanup_error is not None:
            raise StoreError("candidate cleanup failed") from cleanup_error
        if not isinstance(error, (InvalidContract, RejectedDelivery)):
            raise
        refused: dict[str, Receipt] = {}
        for contract, _, manifest in members:
            table = contract.table
            refused[table] = Gate.reject(
                _base(store, contract, manifest),
                manifest,
                (*checks.get(table, ()), Check("execution", False, str(error))),
                now=now,
                source=described[table],
            )
            _call(store.publish, None, refused[table])
        return refused
    try:
        for table, receipt in receipts.items():
            _call(store.publish, candidates.pop(table, None), receipt)
    finally:
        for candidate in candidates.values():
            _call(store.abort, candidate)
    return receipts


def _accepted(store: Store, members: list[tuple[Contract, object, Manifest]]) -> dict[str, Receipt | None]:
    return {c.table: _call(store.accepted_receipt, m.identity) for c, _, m in members}


def _replay_together(
    store: Store,
    members: list[tuple[Contract, object, Manifest]],
    previous: Mapping[str, Receipt | None],
    ledgers: Mapping[str, Ledger],
    now: datetime,
) -> dict[str, Receipt]:
    """A group replays whole; a table accepted before on its own refuses the rest of the group."""
    accepted = ", ".join(table for table, receipt in previous.items() if receipt is not None)
    receipts: dict[str, Receipt] = {}
    for contract, source, manifest in members:
        table, earlier = contract.table, previous[contract.table]
        if earlier is not None and manifest.content_hash is not None:
            receipts[table] = _replay(store, contract, source, earlier, ledgers[table], now)
        elif earlier is not None:
            receipts[table] = replace(earlier, outcome=Outcome.REPLAYED)
        else:
            receipts[table] = Gate.reject(
                ledgers[table],
                manifest,
                (Check("together", False, f"a group replays whole, and {accepted} was accepted before"),),
                now=now,
                source=_description(source),
            )
            _call(store.publish, None, receipts[table])
    return receipts
