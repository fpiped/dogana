"""Two persistence mechanisms sharing checks and plan execution."""

from datetime import date, datetime

from dogana import Check, Gate, InconsistentPublication, InvalidContract, Ledger, Operation


def check_rows(contract, manifest, rows, previous):
    names = tuple(c.name for c in contract.schema)
    types = {"int": int, "str": str, "float": float, "bool": bool, "date": date, "datetime": datetime}
    schema_ok = (
        bool(names)
        and manifest.schema_hash == contract.schema_hash
        and all(
            tuple(row) == names
            and all(row[c.name] is None or type(row[c.name]) is types[c.type] for c in contract.schema)
            for row in rows
        )
    )
    if not schema_ok:
        return tuple(Check(r.name, False) for r in Gate.requirements(contract, manifest))

    def key(row):
        return tuple(row[k] for k in contract.key)

    live = [key(r) for r in rows]
    shape = contract.shape(manifest.shape)
    window = manifest.window
    col = contract.window_column

    def inside(row):
        return window is None or row[col] is not None and window[0] <= row[col] <= window[1]

    published = {key(r): r for r in previous}
    covered = [r for r in previous if inside(r)]
    shrink = contract.max_shrink_fraction
    outcomes = {
        "row_count": manifest.rows == len(rows),
        "schema_hash": schema_ok,
        "key_integrity": len(set(live)) == len(live) and all(None not in k for k in live),
        "rows_in_window": all(inside(r) for r in rows),
        "window_stability": col is None
        or all(
            r[col] is not None
            and (
                shape.complete
                and shape.coverage == "all"
                or key(r) not in published
                or published[key(r)][col] == r[col]
            )
            for r in rows
        ),
        "named_keys": (
            (shape.coverage != "keys" and not manifest.keys or set(live) == set(manifest.keys))
            and not set(live).intersection(manifest.tombstones)
            and all(k not in published or inside(published[k]) for k in manifest.tombstones)
        ),
        "shrink_tolerance": not covered
        or shrink is None
        or (len(covered) - len(rows)) / len(covered) <= shrink,
    }
    return tuple(Check(r.name, outcomes[r.name]) for r in Gate.requirements(contract, manifest))


def apply_rows(previous, incoming, plan):
    rows = list(previous)

    def key(row):
        return tuple(row[k] for k in plan.key)

    for action in plan.actions:
        if action.operation is Operation.REPLACE_ALL:
            rows = []
        if action.operation is Operation.REPLACE_WINDOW:
            rows = [r for r in rows if not action.window[0] <= r[plan.window_column] <= action.window[1]]
        if action.operation in (Operation.UPSERT, Operation.REPLACE_WINDOW):
            live = {key(r) for r in incoming}
            rows = [r for r in rows if key(r) not in live]
        if action.operation is Operation.TOMBSTONE:
            rows = [r for r in rows if key(r) not in action.keys]
        else:
            rows.extend(dict(r) for r in incoming)
    return rows


class MemoryStore:
    atomic_append = True

    def __init__(self):
        self.state = ({}, {}, {})

    def receipts(self, table=None):
        return tuple(r for r in self.state[2].values() if table is None or r.ledger.contract.table == table)

    def _ledger(self, contract):
        ledger = self.state[1].get(contract.table, Ledger(contract))
        if ledger.contract != contract:
            raise InvalidContract("contract changed")
        return ledger

    def load_ledger(self, contract):
        ledger = self._ledger(contract)
        stamp = self.state[0].get(contract.table, ([], None))[1]
        if stamp != ledger.publication_id:
            ahead = stamp is None or any(
                r.publication_id == stamp and r.manifest.identity in ledger.accepted
                for r in self.receipts(contract.table)
                if r.accepted
            )
            raise InconsistentPublication("ledger ahead of stamp" if ahead else "stamp ahead of ledger")
        return ledger

    def repair(self, contract):
        try:
            self.load_ledger(contract)
        except InconsistentPublication as exc:
            if "ledger ahead" in str(exc):
                raise
        ledger = Ledger.from_receipts(contract, self.receipts(contract.table))
        if ledger.publication_id != self.state[0].get(contract.table, ([], None))[1]:
            raise InconsistentPublication("ledger ahead of stamp; complete delivery required")
        self.state[1][contract.table] = ledger
        return ledger

    def _base(self, contract, manifest):
        try:
            return self.load_ledger(contract)
        except InconsistentPublication as exc:
            shape = contract.shape(manifest.shape)
            if "ledger ahead" not in str(exc) or not shape.complete or shape.coverage != "all":
                raise
            if manifest.identity in self._ledger(contract).accepted:
                raise
            return self._ledger(contract)

    def _after_rows(self):
        pass

    def _publish(self, receipt, rows):
        tables, ledgers, receipts = (dict(part) for part in self.state)
        table = receipt.ledger.contract.table
        tables[table] = rows, receipt.publication_id
        self._after_rows()
        ledgers[table] = receipt.ledger
        receipts[receipt.identity] = receipt
        self.state = tables, ledgers, receipts

    def deliver(self, contract, source, manifest):
        if not self.atomic_append and not contract.key:
            raise InvalidContract("append requires an atomic commit")
        ledger = self._base(contract, manifest)
        for receipt in self.receipts(contract.table):
            if receipt.accepted and receipt.manifest.identity == manifest.identity:
                return receipt
        rows = self.state[0].get(contract.table, ([], None))[0]
        checks = ()
        if all(c.passed for c in Gate.preflight(ledger, manifest)):
            checks = check_rows(contract, manifest, source, rows)
        receipt = Gate.evaluate(ledger, manifest, checks)
        if receipt.accepted:
            try:
                self._publish(receipt, apply_rows(rows, source, receipt.transition.plan))
                return receipt
            except OSError as exc:
                receipt = Gate.evaluate(ledger, manifest, (*checks, Check("execution", False, str(exc))))
        self.state[2][receipt.identity] = receipt
        return receipt


class OrderedMemoryStore(MemoryStore):
    atomic_append = False

    def __init__(self):
        super().__init__()
        self.pending = {}

    def _base(self, contract, manifest):
        if self.pending.get(contract.table) == manifest.identity:
            if any(r.accepted and r.manifest == manifest for r in self.receipts(contract.table)):
                ledger = self.repair(contract)
                self.pending.pop(contract.table)
                return ledger
            return self._ledger(contract)
        return super()._base(contract, manifest)

    def _after_receipt(self):
        pass

    def _publish(self, receipt, rows):
        table = receipt.ledger.contract.table
        self.pending[table] = receipt.manifest.identity
        self.state[0][table] = rows, receipt.publication_id
        self._after_rows()
        self.state[2][receipt.identity] = receipt
        self._after_receipt()
        self.state[1][table] = receipt.ledger
        self.pending.pop(table)
