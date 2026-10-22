"""DuckDB candidates, atomic publications and retained row versions."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property, wraps
from hashlib import sha256
from os import PathLike
from pathlib import Path
from threading import Lock, RLock
from types import TracebackType
from typing import Any, Concatenate, ParamSpec, TypeVar
from uuid import uuid4
from weakref import WeakValueDictionary

import duckdb

from dogana import (
    ApplyPlan,
    Check,
    CheckRequirement,
    Contract,
    Coverage,
    Gate,
    Ledger,
    Manifest,
    Operation,
    Receipt,
)
from dogana.contract import (
    LOGICAL_TYPES,
    FullDelivery,
    ParsedDelivery,
    Scalar,
    TimestampChanges,
    Window,
    WindowCoverage,
    WindowDelivery,
    identifier,
)
from dogana.errors import (
    InconsistentPublication,
    InvalidContract,
    LedgerAhead,
    SerializationError,
    StoreError,
)

_TYPES = {
    "int": "BIGINT",
    "float": "DOUBLE",
    "str": "VARCHAR",
    "bool": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP WITH TIME ZONE",
}
_P = ParamSpec("_P")
_R = TypeVar("_R")
_LOCKS: WeakValueDictionary[str, Any] = WeakValueDictionary()
_LOCKS_GUARD = Lock()


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _key_match(keys: tuple[str, ...], left: str, right: str) -> str:
    return " AND ".join(f"{left}.{_quote(k)}={right}.{_quote(k)}" for k in keys)


def _outside_window(column: str) -> str:
    return f"{column} IS NULL OR {column} < ? OR {column} > ?"


def _logical(value: str) -> str:
    if value.endswith("]") or value.startswith(("STRUCT(", "MAP(", "UNION(", "LIST(")):
        return "unsupported"
    if value in {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
    }:
        return "int"
    if value in ("DOUBLE", "FLOAT", "REAL") or value.startswith("DECIMAL"):
        return "float"
    if value.startswith("TIMESTAMP"):
        return "datetime"
    return {"VARCHAR": "str", "BOOLEAN": "bool", "DATE": "date"}.get(value, "unsupported")


def _synchronized(
    method: Callable[Concatenate[DuckDBStore, _P], _R],
) -> Callable[Concatenate[DuckDBStore, _P], _R]:
    @wraps(method)
    def call(self: DuckDBStore, /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        with self._lock:
            try:
                return method(self, *args, **kwargs)
            except (duckdb.Error, OSError) as exc:
                raise StoreError(str(exc)) from exc

    return call


@dataclass(frozen=True)
class _Candidate:
    manifest: Manifest
    contract: Contract | None
    csv: bool = False


@dataclass(frozen=True)
class _Checked:
    contract: Contract
    checks: tuple[Check, ...]
    base: Ledger
    stamp: str | None


@dataclass(frozen=True)
class _Audit:
    candidate: str
    contract: Contract
    delivery: ParsedDelivery
    count: int

    @property
    def manifest(self) -> Manifest:
        return self.delivery.manifest

    @cached_property
    def window(self) -> Window | None:
        match self.delivery:
            case WindowDelivery(window=window) | TimestampChanges(coverage=WindowCoverage(window)):
                return window
            case _:
                return None

    @property
    def staging(self) -> str:
        return _quote(self.candidate)


class DuckDBStore:
    def __init__(self, path: str | Path, *, adopt: bool = False) -> None:
        key = str(Path(path).resolve()) if str(path) != ":memory:" else uuid4().hex
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, RLock())
        self._adopt = adopt
        self._candidates: dict[str, _Candidate] = {}
        self._checked: dict[str, _Checked] = {}
        self._transaction: str | None = None
        with self._lock:
            try:
                self.connection = duckdb.connect(str(path))
                self._initialize()
            except (duckdb.Error, OSError) as exc:
                raise StoreError(str(exc)) from exc

    def _initialize(self) -> None:
        self.connection.execute("SET TimeZone='UTC'")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS __dogana_ledgers
            (table_name VARCHAR PRIMARY KEY, document VARCHAR NOT NULL)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS __dogana_receipts
            (receipt_id VARCHAR PRIMARY KEY, table_name VARCHAR, manifest_id VARCHAR,
             accepted BOOLEAN, publication_id VARCHAR, document VARCHAR NOT NULL)""")
        self.connection.execute("""CREATE INDEX IF NOT EXISTS __dogana_manifest_lookup
            ON __dogana_receipts (manifest_id)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS __dogana_publications
            (table_name VARCHAR PRIMARY KEY, publication_id VARCHAR NOT NULL)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS __dogana_commits
            (publication_id VARCHAR PRIMARY KEY, table_name VARCHAR NOT NULL,
             manifest_id VARCHAR UNIQUE NOT NULL, base_publication_id VARCHAR,
             receipt_id VARCHAR NOT NULL, sequence BIGINT NOT NULL,
             history_table VARCHAR NOT NULL, columns VARCHAR[] NOT NULL)""")

    def __enter__(self) -> DuckDBStore:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    @_synchronized
    def close(self) -> None:
        try:
            for candidate in tuple(self._candidates):
                self.abort(candidate)
        finally:
            self.connection.close()

    def _one(self, sql: str, params: object = None) -> tuple[Any, ...]:
        row = self.connection.execute(sql, params).fetchone()
        if row is None:
            raise StoreError("storage query returned no result")
        return row

    def _exists(self, table: str) -> bool:
        return bool(
            self._one(
                "SELECT count(*) FROM information_schema.tables WHERE lower(table_name)=lower(?)", [table]
            )[0]
        )

    def _description(self, table: str) -> tuple[tuple[str, str], ...]:
        return tuple(
            (str(row[0]), str(row[1]))
            for row in self.connection.execute(f"DESCRIBE {_quote(table)}").fetchall()
        )

    def _check_table_spelling(self, table: str) -> None:
        identifier(table)
        for registry in (
            "__dogana_ledgers",
            "__dogana_publications",
            "__dogana_receipts",
            "__dogana_commits",
        ):
            row = self.connection.execute(
                f"SELECT table_name FROM {registry} WHERE lower(table_name)=lower(?) LIMIT 1", [table]
            ).fetchone()
            if row:
                if row[0] != table:
                    raise InvalidContract("DuckDB table identities must retain their registered letter case")
                return

    def _stored_ledger(self, contract: Contract, *, created: bool = False) -> Ledger:
        self._check_table_spelling(contract.table)
        row = self.connection.execute(
            "SELECT document FROM __dogana_ledgers WHERE table_name=?", [contract.table]
        ).fetchone()
        if row is None:
            tracked = self._one(
                "SELECT EXISTS(SELECT 1 FROM __dogana_commits WHERE table_name=?) OR "
                "EXISTS(SELECT 1 FROM __dogana_receipts WHERE table_name=? AND accepted) OR "
                "EXISTS(SELECT 1 FROM __dogana_publications WHERE table_name=?)",
                [contract.table] * 3,
            )[0]
            if self._exists(contract.table) and not tracked and not self._adopt and not created:
                raise StoreError("table exists without a publication; explicit adopt=True is required")
            return Ledger(contract)
        try:
            ledger = Ledger.from_json(row[0])
        except InvalidContract as exc:
            raise InconsistentPublication("stored ledger is invalid") from exc
        ledger = ledger.with_contract(contract)
        return ledger

    def _stamp(self, table: str) -> str | None:
        row = self.connection.execute(
            "SELECT publication_id FROM __dogana_publications WHERE table_name=?", [table]
        ).fetchone()
        return str(row[0]) if row else None

    @_synchronized
    def publication_stamp(self, table: str) -> str | None:
        self._check_table_spelling(table)
        return self._stamp(table)

    def _assert_receipts_retained(self, table: str) -> None:
        missing = self.connection.execute(
            "SELECT c.publication_id FROM __dogana_commits c LEFT JOIN __dogana_receipts r "
            "ON c.receipt_id=r.receipt_id AND c.publication_id=r.publication_id AND r.accepted "
            "WHERE c.table_name=? AND r.receipt_id IS NULL LIMIT 1",
            [table],
        ).fetchone()
        if missing:
            raise InconsistentPublication("accepted receipt missing for a retained publication")

    def _accepted_history(self, table: str) -> Iterator[Receipt]:
        documents = self.connection.execute(
            "SELECT document FROM __dogana_receipts WHERE table_name=? AND accepted ORDER BY rowid", [table]
        ).fetchall()
        yield from (self._receipt(row[0]) for row in documents)

    def _retained_head(self, table: str) -> Receipt | None:
        row = self.connection.execute(
            "SELECT r.receipt_id, r.manifest_id, r.publication_id, r.document, c.receipt_id "
            "FROM __dogana_receipts r LEFT JOIN __dogana_commits c "
            "ON c.receipt_id=r.receipt_id AND c.table_name=r.table_name "
            "AND c.manifest_id=r.manifest_id AND c.publication_id=r.publication_id "
            "WHERE r.table_name=? AND r.accepted ORDER BY r.rowid DESC LIMIT 1",
            [table],
        ).fetchone()
        if row is None:
            return None
        receipt = self._receipt(row[3])
        if (
            row[4] is None
            or not receipt.accepted
            or receipt.replayed
            or receipt.identity != row[0]
            or receipt.manifest.identity != row[1]
            or receipt.publication_id != row[2]
            or receipt.ledger.contract.table != table
        ):
            raise InconsistentPublication("accepted head receipt differs from its retained publication")
        return receipt

    def _require_committed_read(self) -> None:
        if self._transaction is not None:
            raise StoreError("publish or abort the candidate before reading a publication")

    @_synchronized
    def load_ledger(self, contract: Contract) -> Ledger:
        self._require_committed_read()
        current = self._stored_ledger(contract)
        self._assert_receipts_retained(contract.table)
        return Ledger.reconcile(
            contract,
            self._accepted_history(contract.table),
            current,
            self._stamp(contract.table),
            repair=False,
            retained_head=self._retained_head(contract.table),
        )

    def _base(self, contract: Contract, manifest: Manifest) -> Ledger:
        try:
            return self.load_ledger(contract)
        except LedgerAhead:
            if not isinstance(manifest.check(contract), FullDelivery):
                raise
            if self.accepted_receipt(manifest.identity) is not None:
                raise
            return Ledger.from_receipts(contract, self.receipts(contract.table))

    @_synchronized
    def repair(self, contract: Contract) -> Ledger:
        self._require_committed_read()
        self._assert_receipts_retained(contract.table)
        ledger = Ledger.reconcile(
            contract,
            self.receipts(contract.table),
            self._stored_ledger(contract),
            self._stamp(contract.table),
        )
        self._store_ledger(ledger)
        return ledger

    def _store_ledger(self, ledger: Ledger) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO __dogana_ledgers VALUES (?, ?)", [ledger.contract.table, ledger.to_json()]
        )

    def _receipt(self, document: str) -> Receipt:
        try:
            return Receipt.from_json(document)
        except InvalidContract as exc:
            raise InconsistentPublication("stored receipt is invalid") from exc

    @_synchronized
    def receipts(self, table: str | None = None) -> tuple[Receipt, ...]:
        sql, params = "SELECT document FROM __dogana_receipts", []
        if table is not None:
            sql += " WHERE table_name=?"
            params.append(table)
        return tuple(
            self._receipt(row[0])
            for row in self.connection.execute(sql + " ORDER BY rowid", params).fetchall()
        )

    @_synchronized
    def accepted_receipt(self, identity: str) -> Receipt | None:
        row = self.connection.execute(
            "SELECT receipt_id, document FROM __dogana_receipts WHERE manifest_id=? AND accepted", [identity]
        ).fetchone()
        committed = self.connection.execute(
            "SELECT receipt_id FROM __dogana_commits WHERE manifest_id=?", [identity]
        ).fetchone()
        if committed and (row is None or row[0] != committed[0]):
            raise InconsistentPublication("accepted receipt missing for a retained publication")
        return self._receipt(row[1]) if row else None

    def _save_receipt(self, receipt: Receipt) -> None:
        if receipt.accepted and self._transaction is None:
            raise StoreError("accepted receipts must commit with their rows")
        document = receipt.to_json()
        if Receipt.from_json(document) != receipt:
            raise SerializationError("receipt cannot round-trip through its wire representation")
        receipt_key = receipt.identity if receipt.accepted else uuid4().hex
        self.connection.execute(
            "INSERT INTO __dogana_receipts VALUES (?, ?, ?, ?, ?, ?)",
            [
                receipt_key,
                receipt.ledger.contract.table,
                receipt.manifest.identity,
                receipt.accepted,
                receipt.publication_id,
                document,
            ],
        )

    @_synchronized
    def hold(self, source: object, manifest: Manifest, contract: Contract | None = None) -> str:
        if self._transaction is not None:
            raise StoreError("publish or abort the existing candidate first")
        candidate = "__dogana_candidate_" + uuid4().hex
        self._candidates[candidate] = _Candidate(manifest, contract)
        try:
            if isinstance(source, (str, PathLike)):
                path = Path(source)
                if any(char in str(path) for char in "*?[]{}"):
                    raise InvalidContract("delivered paths must be literal and contain no glob characters")
                path = path.resolve(strict=True)
                if any(char in str(path) for char in "*?[]{}"):
                    raise InvalidContract("delivered paths must be literal and contain no glob characters")
                if not path.is_file():
                    raise StoreError("the delivered path must be a regular file")
                suffix = path.suffix.lower()
                if suffix == ".csv":
                    self._candidates[candidate] = _Candidate(manifest, contract, csv=True)
                    self.connection.execute(
                        f"CREATE TEMP TABLE {_quote(candidate)} AS SELECT * "
                        "FROM read_csv(?, header=true, all_varchar=true, sample_size=-1)",
                        [[str(path)]],
                    )
                elif suffix == ".parquet":
                    self.connection.execute(
                        f"CREATE TEMP TABLE {_quote(candidate)} AS SELECT * FROM read_parquet(?)",
                        [[str(path)]],
                    )
                else:
                    raise InvalidContract("only CSV and Parquet paths are supported")
            elif type(source).__module__.startswith("pyarrow"):
                relation = candidate + "_input"
                try:
                    self.connection.register(relation, source)
                    self.connection.execute(
                        f"CREATE TEMP TABLE {_quote(candidate)} AS SELECT * FROM {_quote(relation)}"
                    )
                finally:
                    self.connection.unregister(relation)
            elif isinstance(source, Iterable) and not isinstance(source, (bytes, Mapping)):
                self._hold_rows(candidate, source, contract)
            else:
                raise InvalidContract("source must be a path, iterable of row mappings or Arrow table")
            return candidate
        except BaseException:
            self._release(candidate)
            raise

    def _hold_rows(self, candidate: str, source: Iterable[object], contract: Contract | None) -> None:
        if contract is None or not contract.schema:
            raise InvalidContract("row mappings require a declared schema")
        columns = tuple(c.name for c in contract.schema)
        declarations = ", ".join(f"{_quote(c.name)} {_TYPES[c.type]}" for c in contract.schema)
        self.connection.execute(f"CREATE TEMP TABLE {_quote(candidate)} ({declarations})")
        placeholders = ", ".join("?" for _ in columns)
        batch: list[list[object]] = []
        for row in source:
            if not isinstance(row, Mapping) or set(row) != set(columns):
                raise InvalidContract("each source row must match the declared columns")
            for column in contract.schema:
                value = row[column.name]
                if value is not None and (
                    type(value) is not LOGICAL_TYPES[column.type].python_type
                    and not (column.type == "float" and type(value) is int)
                    or isinstance(value, datetime)
                    and value.utcoffset() is None
                ):
                    raise InvalidContract(f"source column {column.name} does not have type {column.type}")
            batch.append([row[c] for c in columns])
            if len(batch) == 1000:
                self.connection.executemany(f"INSERT INTO {_quote(candidate)} VALUES ({placeholders})", batch)
                batch.clear()
        if batch:
            self.connection.executemany(f"INSERT INTO {_quote(candidate)} VALUES ({placeholders})", batch)

    def _typed_csv(self, candidate: str, contract: Contract) -> bool:
        source = _quote(candidate)
        predicates = [
            f"({_quote(c.name)} IS NOT NULL AND TRY_CAST({_quote(c.name)} AS {_TYPES[c.type]}) IS NULL)"
            for c in contract.schema
        ]
        predicates.extend(
            f"({_quote(c.name)} IS NOT NULL AND NOT regexp_full_match(trim({_quote(c.name)}), '[+-]?[0-9]+'))"
            for c in contract.schema
            if c.type == "int"
        )
        invalid = " OR ".join(predicates)
        if self._one(f"SELECT count(*) FROM {source} WHERE {invalid}")[0]:
            return False
        self._recast(candidate, contract)
        return True

    def _recast(self, candidate: str, contract: Contract) -> None:
        staging = _quote(candidate)
        casts = ", ".join(
            f"CAST({_quote(c.name)} AS {_TYPES[c.type]}) AS {_quote(c.name)}" for c in contract.schema
        )
        self.connection.execute(f"CREATE OR REPLACE TEMP TABLE {staging} AS SELECT {casts} FROM {staging}")

    def _key_relation(
        self, keys: tuple[tuple[Scalar, ...], ...], columns: tuple[str, ...]
    ) -> tuple[str, list[object]]:
        names = ", ".join(_quote(k) for k in columns)
        values = ", ".join("unnest(?)" for _ in columns)
        return f"(SELECT {values}) AS deleted({names})", [
            [key[i] for key in keys] for i in range(len(columns))
        ]

    def _assert_destination_schema(self, contract: Contract) -> None:
        if not self._exists(contract.table):
            return
        row = self.connection.execute(
            "SELECT document FROM __dogana_ledgers WHERE table_name=?", [contract.table]
        ).fetchone()
        previous = Ledger.from_json(row[0]).contract if row else contract
        actual = tuple((name, _logical(kind)) for name, kind in self._description(contract.table))
        expected = tuple((column.name, column.type) for column in previous.schema)
        if actual != expected:
            raise StoreError("published column order or types differ from the recorded schema")

    @_synchronized
    def check(
        self,
        candidate: str,
        contract: Contract,
        manifest: Manifest,
        requirements: tuple[CheckRequirement, ...],
    ) -> tuple[Check, ...]:
        if self._transaction is not None:
            raise StoreError("publish or abort the existing candidate first")
        if not contract.schema:
            raise InvalidContract("publishing requires a declared schema")
        held = self._candidates.get(candidate)
        if held is None or held.manifest != manifest or held.contract not in (None, contract):
            raise StoreError("candidate belongs to a different manifest")
        if requirements != Gate.requirements(contract, manifest):
            raise StoreError("row checks must come from the gate")
        base, stamp = self._base(contract, manifest), self._stamp(contract.table)
        self._assert_destination_schema(contract)
        audit = _Audit(
            candidate,
            contract,
            manifest.check(contract),
            int(self._one(f"SELECT count(*) FROM {_quote(candidate)}")[0]),
        )
        handlers = {
            "row_count": self._check_row_count,
            "schema_hash": self._check_schema_hash,
            "rows_in_window": self._check_rows_in_window,
            "window_stability": self._check_window_stability,
            "rows_in_interval": self._check_rows_in_interval,
            "key_integrity": self._check_key_integrity,
            "named_keys": self._check_named_keys,
            "shrink_tolerance": self._check_shrink_tolerance,
        }
        checks: dict[str, Check] = {}
        for requirement in requirements:
            name = requirement.name
            dependencies: tuple[str, ...] = () if name in ("row_count", "schema_hash") else ("schema_hash",)
            if name == "named_keys":
                dependencies += ("key_integrity",)
            if name == "shrink_tolerance":
                dependencies += ("row_count",)
            checks[name] = (
                Check(name, False, "not evaluated")
                if any(not checks[dependency].passed for dependency in dependencies)
                else handlers[name](audit)
            )
        result = tuple(checks.values())
        self._checked[candidate] = _Checked(contract, result, base, stamp)
        return result

    def _check_row_count(self, audit: _Audit) -> Check:
        return Check(
            "row_count",
            audit.manifest.rows == audit.count,
            f"declared={audit.manifest.rows}, actual={audit.count}",
        )

    def _check_schema_hash(self, audit: _Audit) -> Check:
        contract, candidate = audit.contract, audit.candidate
        description = self._description(candidate)
        expected = tuple((column.name, column.type) for column in contract.schema)
        names_match = tuple(name for name, _ in description) == tuple(contract.columns)
        held = self._candidates[candidate]
        converted = True
        if held.csv and names_match:
            converted = self._typed_csv(candidate, contract)
            description = self._description(candidate)
            if converted:
                self._candidates[candidate] = _Candidate(held.manifest, held.contract)
        actual = tuple((name, _logical(kind)) for name, kind in description)
        passed = converted and actual == expected and audit.manifest.schema_hash == contract.schema_hash
        if passed and not held.csv:
            self._recast(candidate, contract)
        return Check(
            "schema_hash",
            passed,
            f"expected={expected}, actual={actual}, "
            f"declared_hash={audit.manifest.schema_hash}, expected_hash={contract.schema_hash}",
        )

    def _check_key_integrity(self, audit: _Audit) -> Check:
        keys = audit.contract.key
        columns = ", ".join(_quote(key) for key in keys)
        nulls = " OR ".join(f"{_quote(key)} IS NULL" for key in keys)
        invalid = self._one(f"SELECT count(*) FROM {audit.staging} WHERE {nulls}")[0]
        duplicates = self._one(
            f"SELECT count(*) FROM (SELECT {columns} FROM {audit.staging} "
            f"GROUP BY {columns} HAVING count(*) > 1)"
        )[0]
        return Check("key_integrity", invalid == duplicates == 0, "keys must be non-null and unique")

    def _check_rows_in_window(self, audit: _Audit) -> Check:
        assert audit.contract.window_column is not None and audit.window is not None
        column = _quote(audit.contract.window_column)
        invalid = self._one(
            f"SELECT count(*) FROM {audit.staging} WHERE {_outside_window(column)}",
            list(audit.window),
        )[0]
        return Check("rows_in_window", invalid == 0, "all rows must be inside the closed window")

    def _check_window_stability(self, audit: _Audit) -> Check:
        assert audit.contract.window_column is not None
        column = _quote(audit.contract.window_column)
        invalid = self._one(f"SELECT count(*) FROM {audit.staging} WHERE {column} IS NULL")[0]
        return Check("window_stability", invalid == 0, "window values must be non-null")

    def _check_rows_in_interval(self, audit: _Audit) -> Check:
        assert audit.contract.cursor_column is not None
        column = _quote(audit.contract.cursor_column)
        invalid = self._one(
            f"SELECT count(*) FROM {audit.staging} WHERE {column} IS NULL OR {column} <= ? OR {column} > ?",
            [audit.manifest.since, audit.manifest.produced_at],
        )[0]
        return Check("rows_in_interval", invalid == 0, "row timestamps must be inside (since, produced_at]")

    def _check_named_keys(self, audit: _Audit) -> Check:
        contract, manifest = audit.contract, audit.manifest
        columns = ", ".join(_quote(key) for key in contract.key)
        live = set(self.connection.execute(f"SELECT {columns} FROM {audit.staging}").fetchall())
        enumerated = audit.delivery.shape.coverage is Coverage.KEYS or bool(manifest.keys)
        matches = (not enumerated or live == set(manifest.keys)) and not live.intersection(
            manifest.tombstones
        )
        window = audit.window
        if window is not None and manifest.tombstones and self._exists(contract.table):
            relation, params = self._key_relation(manifest.tombstones, contract.key)
            match = _key_match(contract.key, "p", "deleted")
            assert contract.window_column is not None
            column = "p." + _quote(contract.window_column)
            outside = self._one(
                f"SELECT count(*) FROM {_quote(contract.table)} p JOIN {relation} ON {match} "
                f"WHERE {_outside_window(column)}",
                [*params, *window],
            )[0]
            matches = matches and outside == 0
        return Check("named_keys", bool(matches), "live and deleted keys must agree with the manifest")

    def _check_shrink_tolerance(self, audit: _Audit) -> Check:
        contract = audit.contract
        previous = 0
        if self._exists(contract.table):
            sql = f"SELECT count(*) FROM {_quote(contract.table)}"
            params: list[object] = []
            if audit.window is not None:
                assert contract.window_column is not None
                sql += f" WHERE {_quote(contract.window_column)} BETWEEN ? AND ?"
                params.extend(audit.window)
            previous = self._one(sql, params)[0]
        limit = contract.max_shrink_fraction
        assert limit is not None
        return Check(
            "shrink_tolerance",
            previous == 0 or (previous - audit.count) / previous <= limit,
            f"previous={previous}, candidate={audit.count}",
        )

    def _assert_base(self, checked: _Checked) -> None:
        if self._stamp(checked.contract.table) != checked.stamp:
            raise StoreError("publication changed after the candidate was checked")
        current = self._stored_ledger(checked.contract, created=self._transaction is not None)
        if current.publication_id is None and checked.base.publication_id is not None:
            current = Ledger.from_receipts(checked.contract, self.receipts(checked.contract.table))
        if current != checked.base:
            raise StoreError("ledger changed after the candidate was checked")

    def _align_target(self, candidate: str, plan: ApplyPlan) -> None:
        target, source = _quote(plan.table), _quote(candidate)
        if not self._exists(plan.table):
            self.connection.execute(f"CREATE TABLE {target} AS SELECT * FROM {source} WHERE false")
        elif self._description(plan.table) != self._description(candidate):
            if any(a.operation is Operation.REPLACE_ALL for a in plan.actions):
                self.connection.execute(
                    f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {source} WHERE false"
                )
            else:
                old = dict(self._description(plan.table))
                projections = ", ".join(
                    f"CAST({_quote(name) if name in old else 'NULL'} AS {kind}) AS {_quote(name)}"
                    for name, kind in self._description(candidate)
                )
                self.connection.execute(
                    f"CREATE OR REPLACE TABLE {target} AS SELECT {projections} FROM {target}"
                )

    def _delete_rows(self, candidate: str, plan: ApplyPlan) -> None:
        db, target, source = self.connection, _quote(plan.table), _quote(candidate)
        for action in plan.actions:
            if action.operation is Operation.REPLACE_ALL:
                db.execute(f"DELETE FROM {target}")
            elif action.operation is Operation.REPLACE_WINDOW:
                assert action.window is not None and plan.window_column is not None
                db.execute(
                    f"DELETE FROM {target} WHERE {_quote(plan.window_column)} BETWEEN ? AND ?",
                    list(action.window),
                )
            if plan.key and action.operation in (Operation.UPSERT, Operation.REPLACE_WINDOW):
                match = _key_match(plan.key, "t", "c")
                db.execute(f"DELETE FROM {target} AS t USING {source} AS c WHERE {match}")
            if action.operation is Operation.TOMBSTONE:
                relation, params = self._key_relation(action.keys, plan.key)
                match = _key_match(plan.key, "t", "deleted")
                db.execute(f"DELETE FROM {target} AS t USING {relation} WHERE {match}", params)

    @_synchronized
    def _execute(self, candidate: str, plan: ApplyPlan) -> None:
        checked = self._checked.get(candidate)
        if (
            checked is None
            or not all(c.passed for c in checked.checks)
            or plan != ApplyPlan.build(checked.contract, self._candidates[candidate].manifest)
        ):
            raise StoreError("candidate must pass the gate's checks before its plan executes")
        if self._transaction is not None:
            raise StoreError("a publication transaction is already open")
        self._lock.acquire()
        self._transaction = candidate
        try:
            self.connection.execute("BEGIN TRANSACTION")
            self._assert_base(checked)
            self._assert_destination_schema(checked.contract)
            db, target, source = self.connection, _quote(plan.table), _quote(candidate)
            if self._exists(plan.table):
                self._delete_rows(candidate, plan)
            self._align_target(candidate, plan)
            if any(action.operation is not Operation.TOMBSTONE for action in plan.actions):
                db.execute(f"INSERT INTO {target} SELECT * FROM {source}")
        except BaseException:
            self.abort(candidate)
            raise

    def _publication_point(self) -> None:
        """Fault boundary after provisional row changes and before commit."""

    @_synchronized
    def publish(self, candidate: str | None, receipt: Receipt) -> None:
        if receipt.replayed:
            raise StoreError("replay responses cannot create publications")
        if not receipt.accepted:
            self._publish_rejection(candidate, receipt)
            return
        if candidate is None or candidate not in self._checked:
            raise StoreError("publication requires a checked candidate")
        try:
            checked = self._checked[candidate]
            if receipt.base_publication_id != checked.base.publication_id:
                raise StoreError("receipt names a different base publication")
            expected = Gate.evaluate(
                checked.base,
                self._candidates[candidate].manifest,
                checked.checks,
                now=receipt.received_at,
                source=receipt.source,
            )
            if receipt != expected:
                raise StoreError("receipt does not authorize this candidate against the current publication")
            assert receipt.transition is not None
            self._execute(candidate, receipt.transition.plan)
            self._publication_point()
            sequence = (
                int(
                    self._one(
                        "SELECT count(*) FROM __dogana_commits WHERE table_name=?", [checked.contract.table]
                    )[0]
                )
                + 1
            )
            history, columns = self._record_history(candidate, receipt, sequence)
            self._save_receipt(receipt)
            self.connection.execute(
                "INSERT INTO __dogana_commits VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    receipt.publication_id,
                    checked.contract.table,
                    receipt.manifest.identity,
                    receipt.base_publication_id,
                    receipt.identity,
                    sequence,
                    history,
                    list(columns),
                ],
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO __dogana_publications VALUES (?, ?)",
                [checked.contract.table, receipt.publication_id],
            )
            self._store_ledger(receipt.ledger)
            self.connection.execute("COMMIT")
        except BaseException:
            self.abort(candidate)
            raise
        else:
            self._end_transaction()
            self._release(candidate)

    def _publish_rejection(self, candidate: str | None, receipt: Receipt) -> None:
        self._require_committed_read()
        try:
            if candidate is not None:
                held = self._candidates.get(candidate)
                if (
                    held is None
                    or held.manifest != receipt.manifest
                    or held.contract not in (None, receipt.ledger.contract)
                ):
                    raise StoreError("receipt belongs to another candidate")
            current = self._base(receipt.ledger.contract, receipt.manifest)
            if current != receipt.ledger or current.publication_id != receipt.base_publication_id:
                raise StoreError("base publication moved before rejection was recorded")
            self._save_receipt(receipt)
        finally:
            if candidate is not None:
                self.abort(candidate)

    def _record_history(self, candidate: str, receipt: Receipt, sequence: int) -> tuple[str, tuple[str, ...]]:
        contract, db = receipt.ledger.contract, self.connection
        target = _quote(contract.table)
        description = self._description(contract.table)
        columns = tuple(name for name, _ in description)
        history_name = "__dogana_history_" + contract.table
        if self._exists(history_name) and self._description(history_name)[:-2] != description:
            history_name = (
                "__dogana_layout_" + sha256(repr((contract.table, description)).encode()).hexdigest()
            )
        previous = self.connection.execute(
            "SELECT history_table FROM __dogana_commits WHERE table_name=? ORDER BY sequence DESC LIMIT 1",
            [contract.table],
        ).fetchone()
        switched = previous is not None and previous[0] != history_name
        if switched and previous is not None and self._exists(previous[0]):
            db.execute(
                f"UPDATE {_quote(previous[0])} SET __dogana_until=? WHERE __dogana_until IS NULL", [sequence]
            )
        history = _quote(history_name)
        db.execute(
            f"CREATE TABLE IF NOT EXISTS {history} AS SELECT *, "
            f"0::BIGINT AS __dogana_from, NULL::BIGINT AS __dogana_until FROM {target} WHERE false"
        )
        if not contract.key:
            assert receipt.transition is not None
            for action in receipt.transition.plan.actions:
                if action.operation is Operation.REPLACE_ALL or switched:
                    db.execute(
                        f"UPDATE {history} SET __dogana_until=? WHERE __dogana_until IS NULL", [sequence]
                    )
                elif action.operation is Operation.REPLACE_WINDOW:
                    assert contract.window_column is not None and action.window is not None
                    db.execute(
                        f"UPDATE {history} SET __dogana_until=? WHERE __dogana_until IS NULL "
                        f"AND {_quote(contract.window_column)} BETWEEN ? AND ?",
                        [sequence, *action.window],
                    )
            inserted = target if switched else _quote(candidate)
            db.execute(f"INSERT INTO {history} SELECT *, ?, NULL FROM {inserted}", [sequence])
        else:
            same = " AND ".join(f"h.{_quote(name)} IS NOT DISTINCT FROM p.{_quote(name)}" for name in columns)
            db.execute(
                f"UPDATE {history} AS h SET __dogana_until=? WHERE __dogana_until IS NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {target} p WHERE {same})",
                [sequence],
            )
            db.execute(
                f"INSERT INTO {history} SELECT p.*, ?, NULL FROM {target} p WHERE NOT EXISTS "
                f"(SELECT 1 FROM {history} h WHERE h.__dogana_until IS NULL AND {same})",
                [sequence],
            )
        return history_name, columns

    def _end_transaction(self) -> None:
        if self._transaction is not None:
            self._transaction = None
            self._lock.release()

    def _release(self, candidate: str) -> None:
        if candidate not in self._candidates:
            return
        self.connection.execute(f"DROP TABLE IF EXISTS {_quote(candidate)}")
        self._candidates.pop(candidate, None)
        self._checked.pop(candidate, None)

    @_synchronized
    def abort(self, candidate: str) -> None:
        interruption: BaseException | None = None
        if self._transaction == candidate:
            try:
                self.connection.execute("ROLLBACK")
            except BaseException as exc:
                if not isinstance(exc, duckdb.TransactionException) or "no transaction is active" not in str(
                    exc
                ):
                    interruption = exc
                    try:
                        self.connection.execute("ROLLBACK")
                    except duckdb.TransactionException as retry:
                        if "no transaction is active" not in str(retry):
                            raise
            self._end_transaction()
        self._release(candidate)
        if interruption is not None and not isinstance(interruption, Exception):
            raise interruption

    @_synchronized
    def deliver(
        self, contract: Contract, source: object, manifest: Manifest, *, now: datetime | None = None
    ) -> Receipt:
        from dogana import deliver

        return deliver(self, contract, source, manifest, now=now if now is not None else datetime.now(UTC))

    @_synchronized
    def read(self, table: str, publication_id: str | None = None) -> list[dict[str, object]]:
        self._require_committed_read()
        self._check_table_spelling(table)
        query, params = f"SELECT * FROM {_quote(table)}", []
        if publication_id is not None:
            row = self.connection.execute(
                "SELECT sequence, history_table, columns FROM __dogana_commits "
                "WHERE table_name=? AND publication_id=?",
                [table, publication_id],
            ).fetchone()
            if row is None:
                raise StoreError("unknown table publication")
            columns = ", ".join(_quote(c) for c in row[2])
            query = (
                f"SELECT {columns} FROM {_quote(row[1])} WHERE __dogana_from <= ? "
                "AND (__dogana_until IS NULL OR __dogana_until > ?)"
            )
            params = [row[0], row[0]]
            legacy = "__dogana_version_" + publication_id
            if self._exists(legacy):
                query, params = f"SELECT {columns} FROM {_quote(legacy)}", []
        cursor = self.connection.execute(query + " ORDER BY ALL", params)
        assert cursor.description is not None
        names = [c[0] for c in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    @_synchronized
    def read_publication(self, contract: Contract) -> tuple[list[dict[str, object]], Ledger]:
        """Read rows and evidence under one committed publication identity."""
        self._require_committed_read()
        ledger = self.load_ledger(contract)
        rows = self.read(contract.table, ledger.publication_id) if self._exists(contract.table) else []
        return rows, ledger
