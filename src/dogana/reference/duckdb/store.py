"""DuckDB candidates, atomic publications and JSON evidence persistence."""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from uuid import uuid4

import duckdb

from dogana import (
    ApplyPlan,
    Check,
    CheckRequirement,
    Contract,
    Coverage,
    Gate,
    InconsistentPublication,
    InvalidContract,
    Ledger,
    Manifest,
    Operation,
    Receipt,
)
from dogana.contract import identifier

_TYPES = {
    "int": "BIGINT",
    "float": "DOUBLE",
    "str": "VARCHAR",
    "bool": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP WITH TIME ZONE",
}


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _logical(value: str) -> str:
    if "INT" in value and "INTERVAL" not in value:
        return "int"
    if value in ("DOUBLE", "FLOAT", "REAL") or value.startswith("DECIMAL"):
        return "float"
    if value.startswith("TIMESTAMP"):
        return "datetime"
    return {"VARCHAR": "str", "BOOLEAN": "bool", "DATE": "date"}.get(value, "unsupported")


class DuckDBStore:
    def __init__(self, path: str | Path):
        self.connection = duckdb.connect(str(path))
        self.connection.execute("SET TimeZone='UTC'")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS __dogana_ledgers
            (table_name VARCHAR PRIMARY KEY, document VARCHAR NOT NULL)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS __dogana_receipts
            (receipt_id VARCHAR PRIMARY KEY, table_name VARCHAR, manifest_id VARCHAR,
             accepted BOOLEAN, publication_id VARCHAR, document VARCHAR NOT NULL)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS __dogana_publications
            (table_name VARCHAR PRIMARY KEY, publication_id VARCHAR NOT NULL)""")
        self._candidates: dict[str, tuple[Manifest, bool]] = {}
        self._checked: dict[str, tuple[Contract, tuple[Check, ...]]] = {}
        self._transaction: str | None = None
        self._lock = RLock()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        for candidate in tuple(self._candidates):
            self.abort(candidate)
        self.connection.close()

    def _exists(self, table: str) -> bool:
        return (
            self.connection.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
            ).fetchone()[0]
            > 0
        )

    def _check_table_spelling(self, table: str) -> None:
        identifier(table)
        row = self.connection.execute(
            "SELECT table_name FROM __dogana_ledgers WHERE lower(table_name)=lower(?)", [table]
        ).fetchone()
        if row and row[0] != table:
            raise InvalidContract("DuckDB table identities must retain their registered letter case")

    def _stored_ledger(self, contract: Contract) -> Ledger:
        self._check_table_spelling(contract.table)
        row = self.connection.execute(
            "SELECT document FROM __dogana_ledgers WHERE table_name=?", [contract.table]
        ).fetchone()
        ledger = Ledger.from_json(row[0]) if row else Ledger(contract)
        if ledger.contract != contract:
            raise InvalidContract("a published table's contract cannot change within its history")
        return ledger

    def _stamp(self, table: str) -> str | None:
        row = self.connection.execute(
            "SELECT publication_id FROM __dogana_publications WHERE table_name=?", [table]
        ).fetchone()
        return row[0] if row else None

    def _ledger_ahead(self, ledger: Ledger, stamp: str | None) -> bool:
        if ledger.publication_id is None:
            return False
        if stamp is None:
            return True
        return any(
            r.accepted and r.publication_id == stamp and r.manifest.identity in ledger.accepted
            for r in self.receipts(ledger.contract.table)
        )

    def load_ledger(self, contract: Contract) -> Ledger:
        ledger = self._stored_ledger(contract)
        stamp = self._stamp(contract.table)
        if stamp != ledger.publication_id:
            side = "ledger" if self._ledger_ahead(ledger, stamp) else "stamp"
            raise InconsistentPublication(f"{side} ahead of {'stamp' if side == 'ledger' else 'ledger'}")
        return ledger

    def _base(self, contract: Contract, manifest: Manifest) -> Ledger:
        try:
            return self.load_ledger(contract)
        except InconsistentPublication:
            ledger = self._stored_ledger(contract)
            shape = manifest.check(contract)
            if not (
                self._ledger_ahead(ledger, self._stamp(contract.table))
                and shape.complete
                and shape.coverage is Coverage.ALL
                and manifest.identity not in ledger.accepted
            ):
                raise
            return ledger

    def repair(self, contract: Contract) -> Ledger:
        with self._lock:
            ledger = Ledger.from_receipts(contract, self.receipts(contract.table))
            if ledger.publication_id != self._stamp(contract.table):
                raise InconsistentPublication("ledger ahead of stamp; a new complete delivery is required")
            self.connection.execute(
                "INSERT OR REPLACE INTO __dogana_ledgers VALUES (?, ?)",
                [contract.table, ledger.to_json()],
            )
            return ledger

    def receipts(self, table: str | None = None) -> tuple[Receipt, ...]:
        sql = "SELECT document FROM __dogana_receipts"
        params = []
        if table is not None:
            sql += " WHERE table_name=?"
            params.append(table)
        return tuple(
            Receipt.from_json(r[0])
            for r in self.connection.execute(sql + " ORDER BY rowid", params).fetchall()
        )

    def save_receipt(self, receipt: Receipt) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO __dogana_receipts VALUES (?, ?, ?, ?, ?, ?)""",
            [
                receipt.identity,
                receipt.ledger.contract.table,
                receipt.manifest.identity,
                receipt.accepted,
                receipt.publication_id,
                receipt.to_json(),
            ],
        )

    def hold(self, source: object, manifest: Manifest) -> str:
        if self._transaction is not None:
            raise RuntimeError("publish or abort the existing candidate first")
        path = Path(source)
        suffix = path.suffix.lower()
        reader = {".csv": "read_csv_auto", ".parquet": "read_parquet"}.get(suffix)
        if reader is None:
            raise ValueError("only CSV and Parquet files are supported")
        candidate = "__dogana_candidate_" + uuid4().hex
        options = ", header=true, sample_size=-1" if suffix == ".csv" else ""
        self.connection.execute(
            f"CREATE TEMP TABLE {_quote(candidate)} AS SELECT * FROM {reader}(?{options})", [str(path)]
        )
        self._candidates[candidate] = (manifest, suffix == ".csv")
        return candidate

    def check(
        self,
        candidate: str,
        contract: Contract,
        manifest: Manifest,
        requirements: tuple[CheckRequirement, ...],
    ) -> tuple[Check, ...]:
        if self._candidates[candidate][0] != manifest:
            raise ValueError("candidate belongs to a different manifest")
        if requirements != Gate.requirements(contract, manifest):
            raise ValueError("row checks must come from the gate")
        db, source = self.connection, _quote(candidate)
        count = db.execute(f"SELECT count(*) FROM {source}").fetchone()[0]
        description = db.execute(f"DESCRIBE {source}").fetchall()
        names = tuple(r[0] for r in description)
        if not count and self._candidates[candidate][1] and names == tuple(c.name for c in contract.schema):
            casts = ", ".join(
                f"CAST({_quote(c.name)} AS {_TYPES[c.type]}) AS {_quote(c.name)}" for c in contract.schema
            )
            db.execute(f"CREATE OR REPLACE TEMP TABLE {source} AS SELECT {casts} FROM {source}")
            description = db.execute(f"DESCRIBE {source}").fetchall()
        actual = tuple((r[0], _logical(r[1])) for r in description)
        expected = tuple((c.name, c.type) for c in contract.schema)
        schema_ok = (
            bool(contract.schema) and actual == expected and manifest.schema_hash == contract.schema_hash
        )
        if schema_ok:
            casts = ", ".join(
                f"CAST({_quote(c.name)} AS {_TYPES[c.type]}) AS {_quote(c.name)}" for c in contract.schema
            )
            db.execute(f"CREATE OR REPLACE TEMP TABLE {source} AS SELECT {casts} FROM {source}")
        checks = {
            "row_count": Check(
                "row_count",
                type(manifest.rows) is int and manifest.rows == count,
                f"declared={manifest.rows}, actual={count}",
            ),
            "schema_hash": Check("schema_hash", schema_ok, f"expected={expected}, actual={actual}"),
        }
        key_columns = ", ".join(_quote(k) for k in contract.key)
        if contract.key:
            bad = True
            if schema_ok:
                nulls = " OR ".join(f"{_quote(k)} IS NULL" for k in contract.key)
                bad = db.execute(f"SELECT count(*) FROM {source} WHERE {nulls}").fetchone()[0] > 0
                bad |= (
                    db.execute(
                        f"SELECT count(*) FROM (SELECT {key_columns} FROM {source} "
                        f"GROUP BY {key_columns} HAVING count(*) > 1)"
                    ).fetchone()[0]
                    > 0
                )
            checks["key_integrity"] = Check("key_integrity", not bad, "keys must be non-null and unique")
        if manifest.window is not None:
            valid = False
            if schema_ok:
                col = _quote(contract.window_column)
                invalid = db.execute(
                    f"SELECT count(*) FROM {source} WHERE {col} IS NULL OR {col} < ? OR {col} > ?",
                    list(manifest.window),
                ).fetchone()[0]
                valid = invalid == 0
            checks["rows_in_window"] = Check(
                "rows_in_window", valid, "all rows must be inside the closed window"
            )
        if contract.window_column is not None:
            stable = False
            if schema_ok:
                col = _quote(contract.window_column)
                stable = db.execute(f"SELECT count(*) FROM {source} WHERE {col} IS NULL").fetchone()[0] == 0
                shape = contract.shape(manifest.shape)
                if self._exists(contract.table) and not (shape.complete and shape.coverage is Coverage.ALL):
                    match = " AND ".join(f"p.{_quote(k)}=c.{_quote(k)}" for k in contract.key)
                    moved = db.execute(
                        f"SELECT count(*) FROM {_quote(contract.table)} p JOIN {source} c ON {match} "
                        f"WHERE p.{col} IS DISTINCT FROM c.{col}"
                    ).fetchone()[0]
                    stable &= moved == 0
            checks["window_stability"] = Check(
                "window_stability",
                stable,
                "window values must be non-null and stable for existing keys outside a full replacement",
            )
        if any(r.name == "named_keys" for r in requirements):
            matches = False
            if schema_ok:
                live = set(db.execute(f"SELECT {key_columns} FROM {source}").fetchall())
                enumerate_live = contract.shape(manifest.shape).coverage is Coverage.KEYS or bool(
                    manifest.keys
                )
                matches = (not enumerate_live or live == set(manifest.keys)) and not live.intersection(
                    manifest.tombstones
                )
                if manifest.window is not None and manifest.tombstones:
                    low, high = manifest.window
                    col = _quote(contract.window_column)
                    for key in manifest.tombstones:
                        where = " AND ".join(f"{_quote(k)}=?" for k in contract.key)
                        if self._exists(contract.table):
                            outside = db.execute(
                                f"SELECT count(*) FROM {_quote(contract.table)} WHERE {where} "
                                f"AND ({col} IS NULL OR {col} < ? OR {col} > ?)",
                                [*key, low, high],
                            ).fetchone()[0]
                            matches &= outside == 0
            checks["named_keys"] = Check(
                "named_keys", matches, "live and deleted keys must agree with the manifest"
            )
        if any(r.name == "shrink_tolerance" for r in requirements):
            previous = 0
            if self._exists(contract.table):
                sql = f"SELECT count(*) FROM {_quote(contract.table)}"
                params = []
                if manifest.window is not None:
                    sql += f" WHERE {_quote(contract.window_column)} BETWEEN ? AND ?"
                    params.extend(manifest.window)
                previous = db.execute(sql, params).fetchone()[0]
            checks["shrink_tolerance"] = Check(
                "shrink_tolerance",
                previous == 0 or (previous - count) / previous <= contract.max_shrink_fraction,
                f"previous={previous}, candidate={count}",
            )
        result = tuple(checks[r.name] for r in requirements)
        self._checked[candidate] = contract, result
        return result

    def execute(self, candidate: str, plan: ApplyPlan) -> None:
        contract, checks = self._checked[candidate]
        if not all(c.passed for c in checks) or plan != ApplyPlan.build(
            contract, self._candidates[candidate][0]
        ):
            raise ValueError("candidate must pass the gate's checks before its plan executes")
        if self._transaction is not None:
            raise RuntimeError("a publication transaction is already open")
        db, target, source = self.connection, _quote(plan.table), _quote(candidate)
        db.execute("BEGIN TRANSACTION")
        self._transaction = candidate
        db.execute(f"CREATE TABLE IF NOT EXISTS {target} AS SELECT * FROM {source} WHERE false")
        for action in plan.actions:
            if action.operation is Operation.REPLACE_ALL:
                db.execute(f"DELETE FROM {target}")
            elif action.operation is Operation.REPLACE_WINDOW:
                db.execute(
                    f"DELETE FROM {target} WHERE {_quote(plan.window_column)} BETWEEN ? AND ?",
                    list(action.window),
                )
            if action.operation in (Operation.UPSERT, Operation.REPLACE_WINDOW):
                match = " AND ".join(f"t.{_quote(k)} = c.{_quote(k)}" for k in plan.key)
                db.execute(f"DELETE FROM {target} AS t USING {source} AS c WHERE {match}")
            if action.operation is Operation.TOMBSTONE:
                where = " AND ".join(f"{_quote(k)}=?" for k in plan.key)
                for key in action.keys:
                    db.execute(f"DELETE FROM {target} WHERE {where}", list(key))
            else:
                db.execute(f"INSERT INTO {target} SELECT * FROM {source}")

    def publish(self, candidate: str, receipt: Receipt) -> None:
        if self._transaction != candidate or not receipt.accepted:
            raise ValueError("publication requires an accepted receipt and an executing candidate")
        contract, checks = self._checked[candidate]
        current = self._base(contract, self._candidates[candidate][0])
        expected = Gate.evaluate(current, self._candidates[candidate][0], checks)
        if receipt != expected:
            raise ValueError("receipt does not authorize this candidate against the current publication")
        self.save_receipt(receipt)
        self.connection.execute(
            "INSERT OR REPLACE INTO __dogana_publications VALUES (?, ?)",
            [contract.table, receipt.publication_id],
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO __dogana_ledgers VALUES (?, ?)",
            [contract.table, receipt.ledger.to_json()],
        )
        snapshot = _quote("__dogana_version_" + receipt.publication_id)
        self.connection.execute(f"CREATE TABLE {snapshot} AS SELECT * FROM {_quote(contract.table)}")
        self.connection.execute("COMMIT")
        self._transaction = None
        self._release(candidate)

    def _release(self, candidate: str) -> None:
        self.connection.execute(f"DROP TABLE IF EXISTS {_quote(candidate)}")
        self._candidates.pop(candidate, None)
        self._checked.pop(candidate, None)

    def abort(self, candidate: str) -> None:
        if self._transaction == candidate:
            self.connection.execute("ROLLBACK")
            self._transaction = None
        self._release(candidate)

    def deliver(self, contract: Contract, source: object, manifest: Manifest) -> Receipt:
        with self._lock:
            ledger = self._base(contract, manifest)
            old = self.connection.execute(
                "SELECT document FROM __dogana_receipts WHERE manifest_id=? AND accepted", [manifest.identity]
            ).fetchone()
            if old:
                return Receipt.from_json(old[0])
            if not all(c.passed for c in Gate.preflight(ledger, manifest)):
                receipt = Gate.evaluate(ledger, manifest)
                self.save_receipt(receipt)
                return receipt
            candidate = None
            checks = ()
            try:
                candidate = self.hold(source, manifest)
                checks = self.check(candidate, contract, manifest, Gate.requirements(contract, manifest))
                receipt = Gate.evaluate(ledger, manifest, checks)
                if receipt.accepted:
                    self.execute(candidate, receipt.transition.plan)
                    self.publish(candidate, receipt)
                else:
                    self.abort(candidate)
                    self.save_receipt(receipt)
                return receipt
            except (duckdb.Error, OSError, ValueError, TypeError) as exc:
                if candidate is not None:
                    self.abort(candidate)
                receipt = Gate.evaluate(ledger, manifest, checks + (Check("execution", False, str(exc)),))
                self.save_receipt(receipt)
                return receipt

    def read(self, table: str, publication_id: str | None = None) -> list[dict]:
        self._check_table_spelling(table)
        target = table
        if publication_id is not None:
            exists = self.connection.execute(
                """SELECT count(*) FROM __dogana_receipts
                WHERE table_name=? AND publication_id=? AND accepted""",
                [table, publication_id],
            ).fetchone()[0]
            if not exists:
                raise KeyError("unknown table publication")
            target = "__dogana_version_" + publication_id
        cursor = self.connection.execute(f"SELECT * FROM {_quote(target)} ORDER BY ALL")
        names = [c[0] for c in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
