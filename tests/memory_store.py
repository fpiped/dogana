"""Atomic transaction and table-format commit models."""

from datetime import date, datetime
from threading import RLock
from uuid import uuid4

from dogana import Check, Gate, InconsistentPublication, InvalidContract, Ledger, Operation, StoreError


def check_rows(contract, manifest, rows, previous):
    names = tuple(c.name for c in contract.schema)
    types = {"int": int, "str": str, "float": float, "bool": bool, "date": date, "datetime": datetime}
    schema_ok = not names or (
        manifest.schema_hash == contract.schema_hash
        and all(
            tuple(row) == names
            and all(row[c.name] is None or type(row[c.name]) is types[c.type] for c in contract.schema)
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
        return tuple(row[k] for k in contract.key)

    live = [key(r) for r in rows]
    shape = contract.shape(manifest.shape)
    window, col = manifest.window, contract.window_column

    def inside(row):
        return window is None or row[col] is not None and window[0] <= row[col] <= window[1]

    published = {key(r): r for r in previous}
    covered = [r for r in previous if inside(r)]
    shrink = contract.max_shrink_fraction
    cursor = contract.cursor_column
    outcomes = {
        "row_count": manifest.rows == len(rows),
        "schema_hash": schema_ok,
        "key_integrity": len(set(live)) == len(live) and all(None not in k for k in live),
        "rows_in_window": all(inside(r) for r in rows),
        "window_stability": col is None or all(r[col] is not None for r in rows),
        "named_keys": (
            (shape.coverage != "keys" and not manifest.keys or set(live) == set(manifest.keys))
            and not set(live).intersection(manifest.tombstones)
            and all(k not in published or inside(published[k]) for k in manifest.tombstones)
        ),
        "shrink_tolerance": not covered
        or shrink is None
        or (len(covered) - len(rows)) / len(covered) <= shrink,
    }
    if any(r.name == "rows_in_interval" for r in requirements):
        outcomes["rows_in_interval"] = all(
            r[cursor] is not None and manifest.since < r[cursor] <= manifest.produced_at for r in rows
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
            ledger = self._ledger(contract)
            stamp = self.publication_stamp(contract.table)
            receipts = tuple(r for r in self.receipts(contract.table) if r.accepted)
            if stamp is None and ledger.publication_id is None and receipts:
                raise InconsistentPublication("ledger and stamp missing for retained accepted receipts")
            if stamp != ledger.publication_id:
                ahead = ledger.publication_id is not None and (
                    stamp is None or any(r.publication_id == stamp for r in receipts[:-1])
                )
                raise InconsistentPublication("ledger ahead of stamp" if ahead else "stamp ahead of ledger")
            return ledger

    def repair(self, contract):
        with self.lock:
            ledger = Ledger.from_receipts(contract, self.receipts(contract.table))
            if ledger.publication_id != self.publication_stamp(contract.table):
                raise InconsistentPublication("ledger ahead of stamp; complete delivery required")
            self.state[1][contract.table] = ledger
            return ledger

    def hold(self, source, manifest, contract):
        rows = [dict(row) for row in source]
        token = uuid4().hex
        self.candidates[token] = contract, manifest, rows
        return token

    def check(self, candidate, contract, manifest, requirements):
        with self.lock:
            if not contract.schema:
                raise InvalidContract("publication requires a declared schema")
            held_contract, held_manifest, rows = self.candidates[candidate]
            if (held_contract, held_manifest) != (contract, manifest):
                raise InvalidContract("candidate belongs to another delivery")
            if requirements != Gate.requirements(contract, manifest):
                raise InvalidContract("checks must come from the gate")
            base = self._ledger(contract)
            previous = self.state[0].get(contract.table, ([], None))[0]
            checks = check_rows(contract, manifest, rows, previous)
            self.checked[candidate] = base.publication_id, checks
            return checks

    def _plan(self, candidate, plan):
        contract, manifest, incoming = self.candidates[candidate]
        base, checks = self.checked[candidate]
        if base != self._ledger(contract).publication_id:
            raise StoreError("base publication moved")
        from dogana import ApplyPlan

        if not all(c.passed for c in checks) or plan != ApplyPlan.build(contract, manifest):
            raise InvalidContract("candidate requires passing checks and the prescribed plan")
        return apply_rows(self.state[0].get(contract.table, ([], None))[0], incoming, plan)

    def execute(self, candidate, plan):
        with self.lock:
            self.executed[candidate] = self._plan(candidate, plan)
            self._after_rows()

    def _after_rows(self):
        pass

    def publish(self, candidate, receipt):
        with self.lock:
            contract, manifest, _ = self.candidates[candidate]
            current = self._ledger(contract)
            if current.publication_id != receipt.base_publication_id:
                raise StoreError("base publication moved")
            expected = Gate.evaluate(
                current, manifest, self.checked[candidate][1], now=receipt.received_at, source=receipt.source
            )
            if receipt != expected or not receipt.accepted:
                raise InvalidContract("receipt does not authorize the candidate")
            rows = self.executed[candidate]
            tables, ledgers, receipts = (dict(part) for part in self.state)
            tables[contract.table] = rows, receipt.publication_id
            ledgers[contract.table] = receipt.ledger
            receipts[uuid4().hex] = receipt
            self.publications[manifest.identity] = receipt.publication_id
            self.snapshots[receipt.publication_id] = rows
            self.state = tables, ledgers, receipts
            self.abort(candidate)

    def abort(self, candidate):
        self.candidates.pop(candidate, None)
        self.checked.pop(candidate, None)
        self.executed.pop(candidate, None)

    def save_receipt(self, receipt):
        with self.lock:
            if receipt.accepted:
                raise StoreError("accepted receipts belong to atomic publication")
            self.state[2][uuid4().hex] = receipt

    def read(self, table, publication_id=None):
        with self.lock:
            rows = self.state[0][table][0] if publication_id is None else self.snapshots[publication_id]
            return [dict(row) for row in rows]


class CommitMemoryStore(MemoryStore):
    def execute(self, candidate, plan):
        self._plan(candidate, plan)
        self.executed[candidate] = plan

    def publish(self, candidate, receipt):
        with self.lock:
            self.executed[candidate] = self._plan(candidate, self.executed[candidate])
            self._after_rows()
            super().publish(candidate, receipt)
