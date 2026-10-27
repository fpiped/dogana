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
        held = {} if candidate is None else {contract.table: candidate}
        members = [(contract, source, manifest)]
        return _recover(store, members, held, {contract.table: checks}, error, now)[contract.table]


def _recover(
    store: Store,
    members: list[tuple[Contract, object, Manifest]],
    candidates: Mapping[str, str],
    checks: Mapping[str, tuple[Check, ...]],
    error: BaseException,
    now: datetime,
    group: Group | None = None,
    attempted: Mapping[str, Receipt] | None = None,
) -> dict[str, Receipt]:
    """After a failure: return what committed, raise what no protocol rule decides, refuse the rest."""
    cleanup_error = _abort_all(store, candidates)
    if not isinstance(error, Exception):
        raise error
    committed = _accepted(store, members)
    if group is not None and any(receipt is not None for receipt in committed.values()):
        return _settle(store, group, members, committed, attempted, error, now)
    if all(receipt is not None for receipt in committed.values()):
        for contract, _, _ in members:
            _call(store.load_ledger, contract)
        return cast(dict[str, Receipt], committed)
    if cleanup_error is not None:
        raise StoreError("candidate cleanup failed") from cleanup_error
    if not isinstance(error, (InvalidContract, RejectedDelivery)):
        raise error
    refused: dict[str, Receipt] = {}
    for contract, source, manifest in members:
        refused[contract.table] = Gate.reject(
            _base(store, contract, manifest),
            manifest,
            (*checks.get(contract.table, ()), Check("execution", False, str(error))),
            now=now,
            source=_description(source),
        )
        _call(store.publish, None, refused[contract.table])
    return refused


def _abort_all(store: Store, candidates: Mapping[str, str]) -> Exception | None:
    """Try every abort, returning the last failure."""
    failure: Exception | None = None
    for candidate in candidates.values():
        try:
            _call(store.abort, candidate)
        except Exception as exc:
            failure = exc
    return failure


def _settle(
    store: Store,
    group: Group,
    members: list[tuple[Contract, object, Manifest]],
    committed: Mapping[str, Receipt | None],
    attempted: Mapping[str, Receipt] | None,
    error: Exception,
    now: datetime,
) -> dict[str, Receipt]:
    """A failed group delivery whose tables were accepted anyway is decided as a replay, whoever committed."""
    expected = Gate.delivered(tuple((c.table, m) for c, _, m in members), group.references)
    accepted = {table: receipt for table, receipt in committed.items() if receipt is not None}
    if len(accepted) < len(members) and any(expected in receipt.checks for receipt in accepted.values()):
        raise InconsistentPublication("a group publication committed only some of its tables") from error
    if len(accepted) == len(members) and all(
        receipt == (attempted or {}).get(t) for t, receipt in accepted.items()
    ):
        for contract, _, _ in members:
            _call(store.load_ledger, contract)
        return accepted
    ledgers = {contract.table: _base(store, contract, manifest) for contract, _, manifest in members}
    return _replay_together(store, group, members, committed, ledgers, now)


def _together(store: Store) -> TogetherStore:
    if not all(callable(getattr(store, name, None)) for name in ("check_references", "publish_together")):
        raise StoreError("the store cannot publish tables together")
    return cast(TogetherStore, store)


def deliver_together(
    store: Store, group: Group, deliveries: Mapping[str, tuple[object, Manifest]], now: datetime
) -> dict[str, Receipt]:
    """Check one delivery per table of a group, then publish all of them in one step or refuse them all."""
    now = utc(now)
    together = _together(store)
    if set(deliveries) != set(group.tables):
        raise InvalidContract("a delivery together carries one source per table of its group")
    members = [(group.contract(table), *deliveries[table]) for table in group.tables]
    described = {contract.table: _description(source) for contract, source, _ in members}
    ledgers = {contract.table: _base(store, contract, manifest) for contract, _, manifest in members}
    previous = _accepted(store, members)
    if any(receipt is not None for receipt in previous.values()):
        return _replay_together(store, group, members, previous, ledgers, now)

    candidates: dict[str, str] = {}
    checks: dict[str, tuple[Check, ...]] = {}
    receipts: dict[str, Receipt] = {}
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
        joint: tuple[Check, ...] = (Gate.together(tuple(alone), group.references),)
        if joint[0].passed and group.references:
            joint += (_call(together.check_references, group, candidates),)
        for contract, _, manifest in members:
            table = contract.table
            receipts[table] = Gate.evaluate(
                ledgers[table], manifest, checks.get(table, ()), now=now, source=described[table], joint=joint
            )
        if all(receipt.accepted for receipt in receipts.values()):
            _call(together.publish_together, group, tuple((candidates[t], receipts[t]) for t in group.tables))
            return receipts
    except BaseException as error:
        return _recover(store, members, candidates, checks, error, now, group, receipts)
    try:
        for receipt in receipts.values():
            _call(store.publish, None, receipt)
    finally:
        failure = _abort_all(store, candidates)
    if failure is not None:
        raise StoreError("candidate cleanup failed") from failure
    return receipts


def _accepted(store: Store, members: list[tuple[Contract, object, Manifest]]) -> dict[str, Receipt | None]:
    return {c.table: _call(store.accepted_receipt, m.identity) for c, _, m in members}


def _replay_together(
    store: Store,
    group: Group,
    members: list[tuple[Contract, object, Manifest]],
    previous: Mapping[str, Receipt | None],
    ledgers: Mapping[str, Ledger],
    now: datetime,
) -> dict[str, Receipt]:
    """A group replays only one delivery of these files under these references; otherwise all are refused."""
    expected = Gate.delivered(tuple((c.table, m) for c, _, m in members), group.references)
    earlier = {table: receipt for table, receipt in previous.items() if receipt is not None}
    accepted = ", ".join(sorted(earlier))
    detail = f"a group replays whole, and {accepted} was not accepted together with this group"
    if len(earlier) == len(members) and all(expected in receipt.checks for receipt in earlier.values()):
        replays = {
            contract.table: _replay(
                store, contract, source, earlier[contract.table], ledgers[contract.table], now
            )
            if manifest.content_hash is not None
            else replace(earlier[contract.table], outcome=Outcome.REPLAYED)
            for contract, source, manifest in members
        }
        refused: dict[str, Receipt] = {table: r for table, r in replays.items() if not r.accepted}
        if not refused:
            return replays
        detail = f"a group replays whole, and {', '.join(sorted(refused))} differs from what was accepted"
    else:
        refused = {}
    for contract, source, manifest in members:
        if contract.table in refused:
            continue
        refused[contract.table] = Gate.reject(
            ledgers[contract.table],
            manifest,
            (Check("together", False, detail),),
            now=now,
            source=_description(source),
        )
        _call(store.publish, None, refused[contract.table])
    return {contract.table: refused[contract.table] for contract, _, _ in members}
