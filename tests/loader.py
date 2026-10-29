"""A receiver in memory: it checks rows against the manifest, decides with the gate and commits."""

from dataclasses import replace

from dogana import Check, Gate, Ledger, Operation, Outcome
from dogana.contract import LOGICAL_TYPES, cursor_interval
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


def split_rows(contract, rows):
    """Live rows without the deleted column, the keys of rows marked deleted, and unmarked rows."""
    marker = contract.deleted_column
    if marker is None or marker not in rows.columns:
        return rows, [], 0
    columns = {name: kind for name, kind in rows.columns.items() if name != marker}
    live = RowSource(
        (
            {name: value for name, value in row.items() if name != marker}
            for row in rows
            if row.get(marker) is False
        ),
        columns,
    )
    deleted = [tuple(row.get(k) for k in contract.key) for row in rows if row.get(marker) is True]
    return live, deleted, sum(1 for row in rows if type(row.get(marker)) is not bool)


def check_rows(contract, manifest, rows, previous):
    """The receiver's own checks of the rows against the manifest: counts, schema, keys, window and cursor."""
    delivered = rows
    rows, deleted, unmarked = split_rows(contract, rows)
    columns = rows.columns
    delivery = manifest.check(contract)
    part = delivery.part
    names = ["row_count"]
    if contract.schema_hash is not None:
        names.append("schema_hash")
    if part.window is not None:
        names.append("rows_in_window")
    if contract.window_column is not None:
        names.append("window_present")
    if cursor_interval(contract, delivery) is not None:
        names.append("rows_in_interval")
    if contract.key:
        names.append("key_integrity")
    schema_ok = contract.schema_hash is None or (
        schema_hash(columns) == contract.schema_hash
        and part.schema_hash == contract.schema_hash
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
    if not schema_ok:
        return tuple(
            Check(name, part.rows == len(rows), "count")
            if name == "row_count"
            else Check(name, False, "schema differs" if name == "schema_hash" else "not evaluated")
            for name in names
        )

    def key(row):
        return tuple(row.get(k) for k in contract.key)

    def typed(row, name):
        return name in row and type(row[name]) is LOGICAL_TYPES[contract.types[name]].python_type

    live = [key(r) for r in rows]
    window, col = part.window, contract.window_column

    def inside(row):
        return window is None or typed(row, col) and window[0] <= row[col] <= window[1]

    published = {key(r): r for r in previous} if contract.key else {}
    cursor = contract.cursor_column
    outcomes = {
        "row_count": part.rows == len(rows) and part.tombstones == len(deleted) and not unmarked,
        "schema_hash": schema_ok,
        "key_integrity": all(typed(row, name) for row in rows for name in contract.key)
        and all(None not in k for k in deleted)
        and len(set(live + deleted)) == len(live) + len(deleted),
        "rows_in_window": all(inside(r) for r in rows)
        and all(k not in published or inside(published[k]) for k in live + deleted),
        "window_present": col is None or all(typed(row, col) for row in rows),
    }
    if "rows_in_interval" in names:
        interval = cursor_interval(contract, delivery)
        marker = contract.deleted_column
        outcomes["rows_in_interval"] = all(
            typed(row, cursor) and interval[0] < row[cursor] <= interval[1] for row in rows
        ) and all(
            row.get(cursor) is None or interval[0] < row[cursor] <= interval[1]
            for row in delivered
            if marker is not None and row.get(marker) is True
        )
    return tuple(Check(name, outcomes[name]) for name in names)


def apply_rows(previous, incoming, plan, deleted=()):
    """The published rows after the plan's actions, as a receiver executes them."""
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
            rows = [r for r in rows if key(r) not in set(deleted)]
        else:
            rows.extend(dict(r) for r in incoming)
    return rows


class Loader:
    """A receiver with its tables in memory; a delivery changes rows and ledger together, or neither."""

    def __init__(self):
        self.rows = {}
        self.ledgers = {}
        self.receipts = []

    def ledger(self, contract):
        return self.ledgers.get(contract.table, Ledger(contract)).with_contract(contract)

    def read(self, table):
        return [dict(row) for row in self.rows.get(table, [])]

    def receipts_of(self, table):
        return tuple(r for r in self.receipts if r.contract.table == table)

    def deliver(self, contract, rows, manifest, now):
        """Decide a table of a read as a receiver does: replay by the read, check the rows, decide, commit."""
        for earlier in self.receipts:
            if earlier.accepted and (earlier.read_identity, earlier.contract.table) == (
                manifest.identity,
                contract.table,
            ):
                return replace(earlier, outcome=Outcome.REPLAYED)
        ledger = self.ledger(contract)
        source = RowSource(rows, getattr(rows, "columns", None))
        outcomes = ()
        if all(check.passed for check in Gate.preflight(ledger, manifest, now=now)):
            outcomes = check_rows(contract, manifest, source, self.rows.get(contract.table, []))
        receipt = Gate.evaluate(ledger, manifest, outcomes, now=now)
        if receipt.accepted:
            live, deleted, _ = split_rows(contract, source)
            plan = receipt.transition.plan
            self.rows[contract.table] = apply_rows(self.rows.get(contract.table, []), live, plan, deleted)
            self.ledgers[contract.table] = ledger.follow(receipt)
        self.receipts.append(receipt)
        return receipt
