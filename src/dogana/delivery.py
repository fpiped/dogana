"""One delivery sequence over atomic storage primitives."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from os import PathLike, fsdecode
from typing import ParamSpec, TypeVar

from .contract import Contract, FullDelivery, Manifest
from .errors import InvalidContract, LedgerAhead, RejectedDelivery, StoreError
from .gate import Check, Gate, Outcome, Receipt
from .interfaces import Store
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


def deliver(store: Store, contract: Contract, source: object, manifest: Manifest, now: datetime) -> Receipt:
    """Check and publish once, retaining refusals and propagating storage failures."""
    now = utc(now)
    description = fsdecode(source) if isinstance(source, (str, bytes, PathLike)) else None
    ledger = _base(store, contract, manifest)
    previous = _call(store.accepted_receipt, manifest.identity)
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
