"""Atomic transaction and table-format commit models."""

from threading import RLock
from uuid import uuid4

from dogana import (
    Check,
    Gate,
    InconsistentPublication,
    InvalidContract,
    Ledger,
    Operation,
    Receipt,
    StoreError,
)
from dogana.contract import LOGICAL_TYPES, FullDelivery, cursor_interval
from dogana.errors import LedgerAhead, SerializationError
from tests.support import schema_hash


class RowSource(list):
    def __init__(self, rows, columns=None):
        super().__init__(dict(row) for row in rows)
        self.columns = dict(columns) if columns is not None else self._infer_columns()

    def _infer_columns(self):
        if not self:
            return {}
        columns = {}
        for name in self[0]:
            types = {type(row[name]) for row in self if name in row and row[name] is not None}
            columns[name] = next(
                (name for name, kind in LOGICAL_TYPES.items() if types == {kind.python_type}),
                "unsupported",
            )
        return columns


def check_rows(contract, manifest, rows, previous):
    columns = rows.columns
    schema_ok = contract.schema_hash is None or (
        schema_hash(columns) == contract.schema_hash
        and manifest.schema_hash == contract.schema_hash
        and all(
            tuple(row) == tuple(columns)
            and all(
                kind in LOGICAL_TYPES
                and (row[name] is None or type(row[name]) is LOGICAL_TYPES[kind].python_type)
                for name, kind in columns.items()
            )
            for row in rows
        )
    )
    requirements = Gate.requirements(contract, manifest)
    if not schema_ok:
        return tuple(
            Check(r.name, manifest.rows == len(rows), "count")
            if r.name == "row_count"
            else Check(r.name, False, "schema differs" if r.name == "schema_hash" else "not evaluated")
            for r in requirements
        )

    def key(row):
        return tuple(row.get(k) for k in contract.key)

    def typed(row, name):
        return name in row and type(row[name]) is LOGICAL_TYPES[contract.types[name]].python_type

    live = [key(r) for r in rows]
    shape = contract.shape(manifest.shape)
    window, col = manifest.window, contract.window_column

    def inside(row):
        return window is None or typed(row, col) and window[0] <= row[col] <= window[1]

    published = {key(r): r for r in previous}
    covered = [r for r in previous if inside(r)]
    shrink = contract.max_shrink_fraction
    cursor = contract.cursor_column
    outcomes = {
        "row_count": manifest.rows == len(rows),
        "schema_hash": schema_ok,
        "key_integrity": all(typed(row, name) for row in rows for name in contract.key)
        and len(set(live)) == len(live),
        "rows_in_window": all(inside(r) for r in rows),
        "window_stability": col is None or all(typed(row, col) for row in rows),
        "named_keys": (
            (shape.coverage != "keys" and not manifest.keys or set(live) == set(manifest.keys))
            and not set(live).intersection(manifest.tombstones)
            and all(k not in published or inside(published[k]) for k in manifest.tombstones)
        ),
        "shrink_tolerance": not covered
        or shrink is None
        or (len(covered) - len(rows)) / len(covered) <= shrink,
        "content_hash": False,  # rows in memory have no delivered bytes
    }
    if any(r.name == "rows_in_interval" for r in requirements):
        interval = cursor_interval(contract, manifest.check(contract))
        assert interval is not None
        outcomes["rows_in_interval"] = all(
            typed(row, cursor) and interval[0] < row[cursor] <= interval[1] for row in rows
        )
    return tuple(Check(r.name, outcomes[r.name]) for r in requirements)


def apply_rows(previous, incoming, plan):
    rows = list(previous)

    def key(row):
        return tuple(row[k] for k in plan.key)

    for action in plan.actions:
        if action.operation is Operation.REPLACE_ALL:
            rows = []
        if action.operation is Operation.REPLACE_WINDOW:
            rows = [r for r in rows if not action.window[0] <= r[plan.window_column] <= action.window[1]]
        if plan.key and action.operation in (Operation.UPSERT, Operation.REPLACE_WINDOW):
            live = {key(r) for r in incoming}
            rows = [r for r in rows if key(r) not in live]
        if action.operation is Operation.TOMBSTONE:
            rows = [r for r in rows if key(r) not in action.keys]
        else:
            rows.extend(dict(r) for r in incoming)
    return rows


class MemoryStore:
    def __init__(self):
        self.state = ({}, {}, {})
        self.candidates = {}
        self.checked = {}
        self.executed = {}
        self.publications = {}
        self.snapshots = {}
        self.lock = RLock()

    def receipts(self, table=None):
        with self.lock:
            return tuple(
                r for r in self.state[2].values() if table is None or r.ledger.contract.table == table
            )

    def accepted_receipt(self, identity):
        with self.lock:
            found = next((r for r in self.receipts() if r.accepted and r.manifest.identity == identity), None)
            if found is None and identity in self.publications:
                raise InconsistentPublication("accepted receipt is missing")
            return found

    def publication_stamp(self, table):
        with self.lock:
            return self.state[0].get(table, ([], None))[1]

    def _ledger(self, contract):
        return self.state[1].get(contract.table, Ledger(contract)).with_contract(contract)

    def load_ledger(self, contract):
        with self.lock:
            current = self._ledger(contract)
            return Ledger.reconcile(
                contract,
                self.receipts(contract.table),
                current,
                self.publication_stamp(contract.table),
                repair=False,
            )

    def _base(self, contract, manifest):
        try:
            return self.load_ledger(contract)
        except LedgerAhead:
            if not isinstance(manifest.check(contract), FullDelivery):
                raise
            if self.accepted_receipt(manifest.identity) is not None:
                raise
            return Ledger.from_receipts(contract, self.receipts(contract.table))

    def repair(self, contract):
        with self.lock:
            ledger = Ledger.reconcile(
                contract,
                self.receipts(contract.table),
                self._ledger(contract),
                self.publication_stamp(contract.table),
            )
            self.state[1][contract.table] = ledger
            return ledger

    def hold(self, source, manifest, contract):
        rows = RowSource(source, getattr(source, "columns", None))
        token = uuid4().hex
        self.candidates[token] = contract, manifest, rows
        return token

    def check(self, candidate, contract, manifest, requirements):
        with self.lock:
            held = self.candidates.get(candidate)
            if held is None:
                raise StoreError("checks require a candidate held by this store")
            held_contract, held_manifest, rows = held
            if (held_contract, held_manifest) != (contract, manifest):
                raise InvalidContract("candidate belongs to another delivery")
            if requirements != Gate.requirements(contract, manifest):
                raise InvalidContract("checks must come from the gate")
            base = self._base(contract, manifest)
            previous = self.state[0].get(contract.table, ([], None))[0]
            checks = check_rows(contract, manifest, rows, previous)
            self.checked[candidate] = base, self.publication_stamp(contract.table), checks
            return checks

    def _plan(self, candidate, plan):
        contract, manifest, incoming = self.candidates[candidate]
        base, stamp, checks = self.checked[candidate]
        if base != self._base(contract, manifest) or stamp != self.publication_stamp(contract.table):
            raise StoreError("base publication moved")
        from dogana import ApplyPlan

        if not all(c.passed for c in checks) or plan != ApplyPlan.build(contract, manifest):
            raise InvalidContract("candidate requires passing checks and the prescribed plan")
        return apply_rows(self.state[0].get(contract.table, ([], None))[0], incoming, plan)

    def _execute(self, candidate, plan):
        with self.lock:
            self.executed[candidate] = self._plan(candidate, plan)

    def _publication_point(self):
        pass

    def publish(self, candidate, receipt):
        with self.lock:
            try:
                if receipt.replayed:
                    raise StoreError("replay responses cannot create publications")
                if candidate is not None:
                    contract, manifest, _ = self.candidates[candidate]
                    if contract != receipt.ledger.contract or manifest != receipt.manifest:
                        raise StoreError("receipt belongs to another candidate")
                else:
                    contract, manifest = receipt.ledger.contract, receipt.manifest
                current = self._base(contract, manifest)
                if current.publication_id != receipt.base_publication_id:
                    raise StoreError("base publication moved")
                if not receipt.accepted:
                    if current != receipt.ledger:
                        raise StoreError("rejection evidence differs from the current ledger")
                    self._save_receipt(receipt)
                    return
                if candidate is None or candidate not in self.checked:
                    raise StoreError("publication requires a checked candidate")
                expected = Gate.evaluate(
                    current,
                    manifest,
                    self.checked[candidate][2],
                    now=receipt.received_at,
                    source=receipt.source,
                )
                if receipt != expected:
                    raise InvalidContract("receipt does not authorize the candidate")
                self._validate_receipt(receipt)
                self._execute(candidate, receipt.transition.plan)
                rows = self.executed[candidate]
                self._publication_point()
                tables, ledgers, receipts = (dict(part) for part in self.state)
                tables[contract.table] = rows, receipt.publication_id
                ledgers[contract.table] = receipt.ledger
                receipts[uuid4().hex] = receipt
                self.publications[manifest.identity] = receipt.publication_id
                self.snapshots[receipt.publication_id] = rows
                self.state = tables, ledgers, receipts
            finally:
                if candidate is not None:
                    self.abort(candidate)

    def abort(self, candidate):
        self.candidates.pop(candidate, None)
        self.checked.pop(candidate, None)
        self.executed.pop(candidate, None)

    def _validate_receipt(self, receipt):
        if Receipt.from_json(receipt.to_json()) != receipt:
            raise SerializationError("receipt cannot round-trip through its wire representation")

    def _save_receipt(self, receipt):
        with self.lock:
            if receipt.accepted:
                raise StoreError("accepted receipts belong to atomic publication")
            self._validate_receipt(receipt)
            self.state[2][uuid4().hex] = receipt

    def read(self, table, publication_id=None):
        with self.lock:
            rows = self.state[0][table][0] if publication_id is None else self.snapshots[publication_id]
            return [dict(row) for row in rows]


class CommitMemoryStore(MemoryStore):
    """Materialize the planned rows at the atomic publication boundary."""

    def _execute(self, candidate, plan):
        self.executed[candidate] = self._plan(candidate, plan)
