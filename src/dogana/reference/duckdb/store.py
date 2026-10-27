"""DuckDB candidates, atomic publications and retained row versions."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import cached_property, wraps
from hashlib import sha256
from os import PathLike, stat
from pathlib import Path
from tempfile import TemporaryDirectory
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
    Gate,
    Group,
    Ledger,
    Manifest,
    Operation,
    Receipt,
    Reference,
)
from dogana.contract import (
    LOGICAL_TYPES,
    FullDelivery,
    ParsedDelivery,
    Window,
    coverage_window,
    cursor_interval,
    identifier,
)
from dogana.errors import (
    InconsistentPublication,
    InvalidContract,
    LedgerAhead,
    SerializationError,
    StoreError,
)
from dogana.reference._sources import (
    Rows,
    _column_hash,
    _columns,
    _decimal,
    _file_digest,
    _FileSource,
    _published,
    _snapshot,
)

_TYPES = {
    "int": "BIGINT",
    "float": "DOUBLE",
    "str": "VARCHAR",
    "bool": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP WITH TIME ZONE",
}
# The one CSV grammar dogana reads (RFC 4180, UTF-8), pinned so DuckDB never guesses a dialect.
_CSV = (
    "delim=',', quote='\"', escape='\"', comment='', strict_mode=true, sample_size=-1, "
    "allow_quoted_nulls=false"
)
_P = ParamSpec("_P")
_R = TypeVar("_R")
_FILES_GUARD = Lock()


class _File:
    """One open database file: the name DuckDB opened it under, and the lock its stores share."""

    __slots__ = ("__weakref__", "lock", "path")

    def __init__(self, path: str) -> None:
        self.path, self.lock = path, RLock()


# By DuckDB's name for the file and by its inode, so another name for one file is refused.
_FILES: WeakValueDictionary[object, _File] = WeakValueDictionary()


def _duck(kind: str) -> str:
    """The DuckDB type of a column type: `decimal(18,2)` stays exact as DECIMAL(18,2)."""
    return _TYPES.get(kind) or "DECIMAL" + kind.removeprefix("decimal")


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _key_match(keys: tuple[str, ...], left: str, right: str) -> str:
    return " AND ".join(f"{left}.{_quote(k)}={right}.{_quote(k)}" for k in keys)


def _names(child: str, key: str, child_type: str, key_type: str) -> str:
    """SQL for a child value naming a key: a float names an int only when integral, compared as an integer."""
    if (_logical(child_type), _logical(key_type)) == ("float", "int"):
        return f"{key}=TRY_CAST({child} AS BIGINT) AND {child}=trunc({child})"
    return f"{key}={child}"


def _domain_end(value: object, *, high: bool) -> bool:
    if isinstance(value, datetime):
        return value == (datetime.max if high else datetime.min).replace(tzinfo=UTC)
    if isinstance(value, date):
        return value == (date.max if high else date.min)
    if isinstance(value, int):
        return value >= 2**63 - 1 if high else value <= -(2**63)
    return not high and value == ""


def _inside(column: str, window: Window) -> tuple[str, list[object]]:
    """A closed window as SQL. A bound at the end of its domain leaves that side open, as the ledger does,
    since DuckDB holds dates and timestamps past the ones Python can name."""
    low, high = window
    sides = [(f"{column} >= ?", low)] if not _domain_end(low, high=False) else []
    sides += [(f"{column} <= ?", high)] if not _domain_end(high, high=True) else []
    return " AND ".join(side for side, _ in sides) or "TRUE", [bound for _, bound in sides]


def _brief(error: Exception) -> str:
    """The first line of a reader's error, short: it may quote the producer's data."""
    return str(error).splitlines()[0][:200] if str(error) else type(error).__name__


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
    if value.startswith("DECIMAL"):
        return "decimal" + value.removeprefix("DECIMAL").replace(" ", "")
    if value in ("DOUBLE", "FLOAT", "REAL"):
        return "float"
    if value == "TIMESTAMP WITH TIME ZONE":
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
    columns: Mapping[str, str] | None = None
    header: tuple[str, ...] | None = None
    digest: str | None = None
    prepared: tuple[Mapping[str, str], str | None, int, int] | None = None


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
    columns: Mapping[str, str]
    schema_error: str | None
    digest: str | None
    tombstones: int
    unmarked: int

    @property
    def manifest(self) -> Manifest:
        return self.delivery.manifest

    @cached_property
    def window(self) -> Window | None:
        return coverage_window(self.delivery)

    @property
    def staging(self) -> str:
        return _quote(self.candidate)

    @property
    def deleted(self) -> str:
        return _quote(_deleted(self.candidate))


def _deleted(candidate: str) -> str:
    """The staged keys of a candidate's tombstones, beside its live rows."""
    return candidate + "_deleted"


class DuckDBStore:
    def __init__(self, path: str | Path, *, adopt: bool = False) -> None:
        self._adopt = adopt
        self._candidates: dict[str, _Candidate] = {}
        self._checked: dict[str, _Checked] = {}
        self._references: dict[tuple[tuple[Reference, ...], frozenset[str]], Check] = {}
        self._transaction: tuple[str, ...] | None = None
        # Delivered files are read from private copies here, never from where the sender can change them.
        self._scratch = TemporaryDirectory(prefix="dogana-")
        try:
            self.connection = duckdb.connect(str(path))
            self._file = self._register(str(path) == ":memory:")
        except (duckdb.Error, OSError) as exc:
            raise StoreError(str(exc)) from exc
        self._lock = self._file.lock
        with self._lock:
            try:
                self._initialize()
            except (duckdb.Error, OSError) as exc:
                raise StoreError(str(exc)) from exc

    def _register(self, memory: bool) -> _File:
        """Share a lock with every store on the database DuckDB opened, refusing a second name for its file.

        DuckDB keeps one database per name, so a hard link or another mount path to the same file would
        open a second database on it, with its own lock and no compare and set between the two.
        """
        if memory:
            return _File(uuid4().hex)
        opened = str(
            self._one("SELECT path FROM duckdb_databases() WHERE database_name = current_database()")[0]
        )
        identity = stat(opened)
        inode = (identity.st_dev, identity.st_ino)
        with _FILES_GUARD:
            known = _FILES.get(inode)
            if known is not None and known.path != opened:
                self.connection.close()
                raise StoreError(f"this database file is already open as {known.path}")
            file = _FILES.get(opened) or _File(opened)
            _FILES[opened] = _FILES[inode] = file
            return file

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
            self._scratch.cleanup()
            # A closed connection discards an unresolved transaction, so nothing is left to lock for.
            self._end_transaction()

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
        self._require_committed_read()
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
            or receipt.contract.table != table
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
        self._require_committed_read()
        return self._receipts(table)

    def _receipts(self, table: str | None = None) -> tuple[Receipt, ...]:
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
        self._require_committed_read()
        row = self.connection.execute(
            "SELECT receipt_id, document FROM __dogana_receipts WHERE manifest_id=? AND accepted", [identity]
        ).fetchone()
        committed = self.connection.execute(
            "SELECT receipt_id FROM __dogana_commits WHERE manifest_id=?", [identity]
        ).fetchone()
        if committed and (row is None or row[0] != committed[0]):
            raise InconsistentPublication("accepted receipt missing for a retained publication")
        if row is None:
            return None
        receipt = self._receipt(row[1])
        if receipt.manifest.identity != identity or receipt.identity != row[0]:
            raise InconsistentPublication("a stored receipt answers for another delivery")
        return receipt

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
                receipt.contract.table,
                receipt.manifest.identity,
                receipt.accepted,
                receipt.publication_id,
                document,
            ],
        )

    @_synchronized
    def digest(self, source: object) -> str | None:
        """The SHA-256 of the bytes a delivered file holds now; None for rows, which have no bytes."""
        if not isinstance(source, (str, PathLike)):
            return None
        return _file_digest(source, source.root if isinstance(source, _FileSource) else None)

    @_synchronized
    def hold(self, source: object, manifest: Manifest, contract: Contract | None = None) -> str:
        if self._transaction is not None:
            raise StoreError("publish or abort the existing candidate first")
        candidate = "__dogana_candidate_" + uuid4().hex
        columns = source.columns if isinstance(source, (_FileSource, Rows)) else None
        self._candidates[candidate] = _Candidate(manifest, contract, columns=columns)
        try:
            if isinstance(source, (str, PathLike)):
                suffix = Path(source).suffix.lower()
                if suffix not in (".csv", ".parquet"):
                    raise InvalidContract("only CSV and Parquet paths are supported")
                copy = self._copy(candidate)
                root = source.root if isinstance(source, _FileSource) else None
                digest = _snapshot(source, copy, root)
                header: tuple[str, ...] | None = None
                try:
                    if suffix == ".csv":
                        first = self.connection.execute(
                            f"SELECT * FROM read_csv(?, header=false, all_varchar=true, {_CSV}) LIMIT 1",
                            [str(copy)],
                        ).fetchone()
                        header = tuple(value if value is not None else "" for value in first or ())
                        self.connection.execute(
                            f"CREATE TEMP TABLE {_quote(candidate)} AS SELECT * "
                            f"FROM read_csv(?, header=true, all_varchar=?, {_CSV})",
                            [str(copy), columns is not None],
                        )
                    else:
                        self.connection.execute(
                            f"CREATE TEMP TABLE {_quote(candidate)} AS SELECT * FROM read_parquet(?)",
                            [str(copy)],
                        )
                except (duckdb.InvalidInputException, duckdb.ConversionException) as exc:
                    raise InvalidContract(f"the delivered file does not read: {_brief(exc)}") from exc
                self._candidates[candidate] = _Candidate(
                    manifest,
                    contract,
                    csv=header is not None and columns is not None,
                    columns=columns,
                    header=header,
                    digest=digest,
                )
            elif type(source).__module__.startswith("pyarrow"):
                relation = candidate + "_input"
                try:
                    self.connection.register(relation, source)
                    self.connection.execute(
                        f"CREATE TEMP TABLE {_quote(candidate)} AS SELECT * FROM {_quote(relation)}"
                    )
                finally:
                    self.connection.unregister(relation)
            elif isinstance(source, Rows):
                self._hold_rows(candidate, source.rows, source.columns)
            elif isinstance(source, Iterable) and not isinstance(source, (bytes, Mapping)):
                self._hold_rows(candidate, source)
            else:
                raise InvalidContract("source must be a path, iterable of row mappings or Arrow table")
            return candidate
        except BaseException:
            self._release(candidate)
            raise

    def _hold_rows(
        self, candidate: str, source: Iterable[object], columns: Mapping[str, str] | None = None
    ) -> None:
        inferred: dict[str, str | None] = dict(columns) if columns is not None else {}
        staging = _quote(candidate)
        batch: list[list[object]] = []

        def create() -> None:
            declarations = ", ".join(
                f"{_quote(name)} {_duck(kind or 'str')}" for name, kind in inferred.items()
            )
            self.connection.execute(f"CREATE TEMP TABLE {staging} ({declarations})")

        def flush() -> None:
            if batch:
                placeholders = ", ".join("?" for _ in inferred)
                self.connection.executemany(f"INSERT INTO {staging} VALUES ({placeholders})", batch)
                batch.clear()

        if columns is not None:
            create()
        for row in source:
            if not isinstance(row, Mapping):
                raise InvalidContract("source rows must be mappings")
            if not inferred:
                _columns({name: "str" for name in row})
                inferred = dict.fromkeys(row)
                create()
            if set(row) != set(inferred):
                raise InvalidContract("each source row must have the same columns")
            for name, value in row.items():
                if value is None:
                    continue
                kind = next((k for k, t in LOGICAL_TYPES.items() if type(value) is t.python_type), None)
                if isinstance(value, Decimal) and _decimal(inferred[name] or "") is not None:
                    kind = inferred[name]
                if kind is None or isinstance(value, datetime) and value.utcoffset() is None:
                    raise InvalidContract(f"source column {name} has an unsupported value")
                previous = inferred[name]
                if previous == "float" and kind == "int":
                    kind = "float"
                if previous != kind:
                    if (
                        columns is not None
                        or previous not in (None, "int")
                        or (previous == "int" and kind != "float")
                    ):
                        raise InvalidContract(f"source column {name} has inconsistent types")
                    flush()
                    self.connection.execute(f"ALTER TABLE {staging} ALTER {_quote(name)} TYPE {_duck(kind)}")
                    inferred[name] = kind
            batch.append([row[name] for name in inferred])
            if len(batch) == 1000:
                flush()
        if not inferred or any(kind is None for kind in inferred.values()):
            raise InvalidContract("empty or all-null row columns require an explicitly typed source")
        flush()

    def _typed_csv(self, candidate: str, columns: Mapping[str, str]) -> bool:
        source = _quote(candidate)
        predicates = [
            f"({_quote(name)} IS NOT NULL AND TRY_CAST({_quote(name)} AS {_duck(kind)}) IS NULL)"
            for name, kind in columns.items()
        ]
        predicates.extend(
            f"({_quote(name)} IS NOT NULL AND NOT regexp_full_match(trim({_quote(name)}), '[+-]?[0-9]+'))"
            for name, kind in columns.items()
            if kind == "int"
        )
        # A decimal is read exactly: more fraction digits than its scale would be rounded away.
        predicates.extend(
            f"({_quote(name)} IS NOT NULL AND NOT regexp_full_match(trim({_quote(name)}), "
            f"'[+-]?[0-9]+([.][0-9]{{0,{scale[1]}}})?'))"
            for name, kind in columns.items()
            if (scale := _decimal(kind)) is not None
        )
        # A time without its offset names no instant; reading it as UTC would guess.
        predicates.extend(
            f"({_quote(name)} IS NOT NULL "
            f"AND NOT regexp_matches(trim({_quote(name)}), '([zZ]|[+-][0-9:]+)$'))"
            for name, kind in columns.items()
            if kind == "datetime"
        )
        invalid = " OR ".join(predicates)
        if self._one(f"SELECT count(*) FROM {source} WHERE {invalid}")[0]:
            return False
        self._recast(candidate, columns)
        return True

    def _recast(self, candidate: str, columns: Mapping[str, str]) -> None:
        staging = _quote(candidate)
        casts = ", ".join(
            f"CAST({_quote(name)} AS {_duck(kind)}) AS {_quote(name)}" for name, kind in columns.items()
        )
        self.connection.execute(f"CREATE OR REPLACE TEMP TABLE {staging} AS SELECT {casts} FROM {staging}")

    def _assert_destination_schema(self, contract: Contract, candidate: str) -> None:
        row = self.connection.execute(
            "SELECT publication_id, history_table, columns FROM __dogana_commits "
            "WHERE table_name=? ORDER BY sequence DESC LIMIT 1",
            [contract.table],
        ).fetchone()
        if not self._exists(contract.table):
            if row is not None:
                raise StoreError("published table is missing from its recorded schema")
            return
        actual = self._description(contract.table)
        if row is not None:
            if self._exists(row[1]):
                expected = self._description(row[1])[:-2]
            elif self._exists("__dogana_version_" + row[0]):
                expected = self._description("__dogana_version_" + row[0])
            else:
                raise InconsistentPublication("recorded publication schema is missing")
            matches = actual == expected and tuple(name for name, _ in expected) == tuple(row[2])
        else:
            columns = {name: _logical(kind) for name, kind in actual}
            matches = (
                _column_hash(columns) == contract.schema_hash
                if contract.schema_hash is not None
                else tuple(columns.items())
                == tuple((name, _logical(kind)) for name, kind in self._description(candidate))
            )
        if not matches:
            raise StoreError("published column order or types differ from the recorded schema")

    def _prepare_schema(self, candidate: str, contract: Contract) -> tuple[Mapping[str, str], str | None]:
        held = self._candidates[candidate]
        columns = {name: _logical(kind) for name, kind in self._description(candidate)}
        if held.header is not None and held.header != tuple(columns):
            return columns, "CSV header differs from the parsed column names"
        if held.columns is not None and tuple(columns) != tuple(held.columns):
            return columns, "source column order differs from the supplied columns"
        if held.csv:
            assert held.columns is not None
            if not self._typed_csv(candidate, held.columns):
                return columns, "CSV values cannot be converted to the supplied column types"
            self._candidates[candidate] = replace(held, csv=False)
            columns = dict(held.columns)
        if held.columns is not None and columns != held.columns:
            return columns, "source types differ from the supplied column types"
        try:
            validated = _columns(columns)
        except InvalidContract as exc:
            return columns, str(exc)
        if any(
            columns.get(name) != kind
            for name, kind in contract.types.items()
            if name != contract.deleted_column or name in columns
        ):
            return columns, "source columns differ from the contract's bound column types"
        try:
            self._recast(candidate, validated)
        except duckdb.ConversionException as exc:
            return columns, f"values cannot be converted to their column types: {_brief(exc)}"
        return validated, None

    def _split(self, candidate: str, contract: Contract, columns: Mapping[str, str]) -> tuple[int, int]:
        """Move rows marked deleted out of the live rows, keeping their keys; count unmarked rows."""
        marker = contract.deleted_column
        keys = ", ".join(_quote(k) for k in contract.key)
        staging, deleted = _quote(candidate), _quote(_deleted(candidate))
        if marker is None or columns.get(marker) != "bool":
            self.connection.execute(
                f"CREATE TEMP TABLE {deleted} AS SELECT {keys} FROM {staging} WHERE false"
            )
            return 0, 0
        else:
            column = _quote(marker)
            unmarked = int(self._one(f"SELECT count(*) FROM {staging} WHERE {column} IS NULL")[0])
            self.connection.execute(
                f"CREATE TEMP TABLE {deleted} AS SELECT {keys} FROM {staging} WHERE {column}"
            )
            self.connection.execute(
                f"CREATE OR REPLACE TEMP TABLE {staging} AS "
                f"SELECT * EXCLUDE ({column}) FROM {staging} WHERE NOT {column}"
            )
            return int(self._one(f"SELECT count(*) FROM {deleted}")[0]), unmarked

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
        held = self._candidates.get(candidate)
        if held is None or held.manifest != manifest or held.contract not in (None, contract):
            raise StoreError("candidate belongs to a different manifest")
        # Preparation rewrites the staged rows for this contract, so the candidate stays bound to it.
        self._candidates[candidate] = replace(held, contract=contract)
        if requirements != Gate.requirements(contract, manifest):
            raise StoreError("row checks must come from the gate")
        base, stamp = self._base(contract, manifest), self._stamp(contract.table)
        prepared = self._candidates[candidate].prepared
        if prepared is None:
            # Preparing a candidate rewrites its staging table, so a second check reuses the first result.
            columns, schema_error = self._prepare_schema(candidate, contract)
            tombstones, unmarked = (
                self._split(candidate, contract, columns) if contract.key and schema_error is None else (0, 0)
            )
            prepared = (columns, schema_error, tombstones, unmarked)
            self._candidates[candidate] = replace(self._candidates[candidate], prepared=prepared)
        columns, schema_error, tombstones, unmarked = prepared
        if schema_error is not None and contract.schema_hash is None:
            raise InvalidContract(schema_error)
        self._assert_destination_schema(contract, candidate)
        audit = _Audit(
            candidate,
            contract,
            manifest.check(contract),
            int(self._one(f"SELECT count(*) FROM {_quote(candidate)}")[0]),
            _published(columns, contract),
            schema_error,
            self._candidates[candidate].digest,
            tombstones,
            unmarked,
        )
        handlers = {
            "row_count": self._check_row_count,
            "schema_hash": self._check_schema_hash,
            "rows_in_window": self._check_rows_in_window,
            "window_stability": self._check_window_stability,
            "rows_in_interval": self._check_rows_in_interval,
            "key_integrity": self._check_key_integrity,
            "shrink_tolerance": self._check_shrink_tolerance,
            "content_hash": self._check_content_hash,
        }
        checks: dict[str, Check] = {}
        for requirement in requirements:
            name = requirement.name
            dependencies: tuple[str, ...] = (
                ("schema_hash",)
                if contract.schema_hash is not None and name not in ("row_count", "schema_hash")
                else ()
            )
            if name == "shrink_tolerance":
                dependencies += ("row_count",)
            checks[name] = (
                Check(name, False, "not evaluated")
                if any(not checks[dependency].passed for dependency in dependencies)
                else handlers[name](audit)
            )
        result = tuple(checks.values())
        self._checked[candidate] = _Checked(contract, result, base, stamp)
        self._forget_references(candidate)
        return result

    def _check_row_count(self, audit: _Audit) -> Check:
        manifest = audit.manifest
        return Check(
            "row_count",
            manifest.rows == audit.count and manifest.tombstones == audit.tombstones and not audit.unmarked,
            f"declared rows={manifest.rows} tombstones={manifest.tombstones}, "
            f"actual rows={audit.count} tombstones={audit.tombstones}"
            + (f", {audit.unmarked} rows neither live nor deleted" if audit.unmarked else ""),
        )

    def _check_schema_hash(self, audit: _Audit) -> Check:
        actual = _column_hash(audit.columns)
        expected = audit.contract.schema_hash
        passed = audit.schema_error is None and actual == audit.manifest.schema_hash == expected
        return Check(
            "schema_hash",
            passed,
            f"actual_hash={actual}, declared_hash={audit.manifest.schema_hash}, expected_hash={expected}"
            + (f"; {audit.schema_error}" if audit.schema_error else ""),
        )

    def _check_content_hash(self, audit: _Audit) -> Check:
        if audit.digest is None:
            return Check("content_hash", False, "the source has no delivered bytes to hash")
        return Check(
            "content_hash",
            audit.digest == audit.manifest.content_hash,
            f"declared={audit.manifest.content_hash}, actual={audit.digest}",
        )

    def _check_key_integrity(self, audit: _Audit) -> Check:
        keys = audit.contract.key
        columns = ", ".join(_quote(key) for key in keys)
        nulls = " OR ".join(f"{_quote(key)} IS NULL" for key in keys)
        rows = f"(SELECT {columns} FROM {audit.staging} UNION ALL SELECT {columns} FROM {audit.deleted})"
        invalid = self._one(f"SELECT count(*) FROM {rows} WHERE {nulls}")[0]
        duplicates = self._one(
            f"SELECT count(*) FROM (SELECT {columns} FROM {rows} GROUP BY {columns} HAVING count(*) > 1)"
        )[0]
        return Check(
            "key_integrity", invalid == duplicates == 0, "keys must be non-null and unique, live or deleted"
        )

    def _check_rows_in_window(self, audit: _Audit) -> Check:
        assert audit.contract.window_column is not None and audit.window is not None
        column = _quote(audit.contract.window_column)
        inside, bounds = _inside(column, audit.window)
        invalid = self._one(
            f"SELECT count(*) FROM {audit.staging} WHERE {column} IS NULL OR NOT ({inside})", bounds
        )[0]
        if audit.contract.key and self._exists(audit.contract.table):
            # A key keeps its window value: a copy published outside the window would stay claimed there.
            keys = ", ".join(_quote(key) for key in audit.contract.key)
            match = _key_match(audit.contract.key, "p", "d")
            published, bounds = _inside("p." + column, audit.window)
            invalid += self._one(
                f"SELECT count(*) FROM {_quote(audit.contract.table)} p "
                f"JOIN (SELECT {keys} FROM {audit.staging} UNION ALL SELECT {keys} FROM {audit.deleted}) d "
                f"ON {match} WHERE p.{column} IS NULL OR NOT ({published})",
                bounds,
            )[0]
        return Check(
            "rows_in_window",
            invalid == 0,
            "delivered rows and the published copies of their keys must be inside it",
        )

    def _check_window_stability(self, audit: _Audit) -> Check:
        assert audit.contract.window_column is not None
        column = _quote(audit.contract.window_column)
        invalid = self._one(f"SELECT count(*) FROM {audit.staging} WHERE {column} IS NULL")[0]
        return Check("window_stability", invalid == 0, "window values must be non-null")

    def _check_rows_in_interval(self, audit: _Audit) -> Check:
        interval = cursor_interval(audit.contract, audit.delivery)
        assert audit.contract.cursor_column is not None and interval is not None
        column = _quote(audit.contract.cursor_column)
        invalid = self._one(
            f"SELECT count(*) FROM {audit.staging} WHERE {column} IS NULL OR {column} <= ? OR {column} > ?",
            list(interval),
        )[0]
        low, high = interval
        return Check("rows_in_interval", invalid == 0, f"cursor values must be inside ({low}, {high}]")

    def _check_shrink_tolerance(self, audit: _Audit) -> Check:
        contract = audit.contract
        previous = 0
        if self._exists(contract.table):
            sql = f"SELECT count(*) FROM {_quote(contract.table)}"
            params: list[object] = []
            if audit.window is not None:
                assert contract.window_column is not None
                inside, params = _inside(_quote(contract.window_column), audit.window)
                sql += f" WHERE {inside}"
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
            current = Ledger.from_receipts(checked.contract, self._receipts(checked.contract.table))
        if current != checked.base:
            raise StoreError("ledger changed after the candidate was checked")

    def _align_target(self, candidate: str, plan: ApplyPlan) -> None:
        target, source = _quote(plan.table), _quote(candidate)
        if not self._exists(plan.table):
            self.connection.execute(f"CREATE TABLE {target} AS SELECT * FROM {source} WHERE false")
        elif self._description(plan.table) != self._description(candidate):
            if not self._one(f"SELECT count(*) FROM {target}")[0]:
                self.connection.execute(
                    f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {source} WHERE false"
                )
            else:
                raise InvalidContract("schema changes require a full replacement while published rows remain")

    def _delete_rows(self, candidate: str, plan: ApplyPlan) -> None:
        db, target, source = self.connection, _quote(plan.table), _quote(candidate)
        for action in plan.actions:
            if action.operation is Operation.REPLACE_ALL:
                db.execute(f"DELETE FROM {target}")
            elif action.operation is Operation.REPLACE_WINDOW:
                assert action.window is not None and plan.window_column is not None
                inside, bounds = _inside(_quote(plan.window_column), action.window)
                db.execute(f"DELETE FROM {target} WHERE {inside}", bounds)
            if plan.key and action.operation in (Operation.UPSERT, Operation.REPLACE_WINDOW):
                match = _key_match(plan.key, "t", "c")
                db.execute(f"DELETE FROM {target} AS t USING {source} AS c WHERE {match}")
            if action.operation is Operation.TOMBSTONE:
                match = _key_match(plan.key, "t", "d")
                db.execute(
                    f"DELETE FROM {target} AS t USING {_quote(_deleted(candidate))} AS d WHERE {match}"
                )

    def _write_rows(self, candidate: str, plan: ApplyPlan) -> None:
        if self._exists(plan.table):
            self._delete_rows(candidate, plan)
        self._align_target(candidate, plan)
        if any(action.operation is not Operation.TOMBSTONE for action in plan.actions):
            self.connection.execute(f"INSERT INTO {_quote(plan.table)} SELECT * FROM {_quote(candidate)}")

    def _authorize(self, candidate: str, receipt: Receipt, joint: tuple[Check, ...]) -> ApplyPlan:
        checked = self._checked[candidate]
        manifest = self._candidates[candidate].manifest
        if receipt.base_publication_id != checked.base.publication_id:
            raise StoreError("receipt names a different base publication")
        expected = Gate.evaluate(
            checked.base,
            manifest,
            checked.checks,
            now=receipt.received_at,
            source=receipt.source,
            joint=joint,
        )
        if receipt != expected:
            raise StoreError("receipt does not authorize this candidate against the current publication")
        assert receipt.transition is not None
        plan = receipt.transition.plan
        if not all(c.passed for c in checked.checks) or plan != ApplyPlan.build(checked.contract, manifest):
            raise StoreError("candidate must pass the gate's checks before its plan executes")
        return plan

    def _publication_point(self) -> None:
        """Fault boundary after provisional row changes and before commit."""

    @_synchronized
    def publish(self, candidate: str | None, receipt: Receipt) -> None:
        if receipt.replayed:
            raise StoreError("replay responses cannot create publications")
        if not receipt.accepted:
            self._publish_rejection(candidate, receipt)
            return
        if candidate is None:
            raise StoreError("publication requires a checked candidate")
        self._publish_accepted(((candidate, receipt),), ())

    @_synchronized
    def publish_together(self, group: Group, entries: tuple[tuple[str, Receipt], ...]) -> None:
        """Publish the accepted deliveries of a group in one transaction; any refusal releases them."""
        key = (group.references, frozenset(candidate for candidate, _ in entries))
        try:
            if not entries or any(not receipt.accepted or receipt.replayed for _, receipt in entries):
                raise StoreError("a group publishes accepted receipts only")
            if any(candidate not in self._checked for candidate, _ in entries):
                raise StoreError("publication requires a checked candidate")
            tables = sorted(self._checked[candidate].contract.table for candidate, _ in entries)
            if tables != sorted(group.tables):
                raise StoreError("a group publishes one candidate per table of the group")
            if group.references and key not in self._references:
                raise StoreError("references must be checked on these candidates before they publish")
        except BaseException:
            for candidate, _ in entries:
                self.abort(candidate)
            raise
        alone = tuple(
            Gate.evaluate(
                self._checked[candidate].base,
                self._candidates[candidate].manifest,
                self._checked[candidate].checks,
                now=receipt.received_at,
                source=receipt.source,
            )
            for candidate, receipt in entries
        )
        joint: tuple[Check, ...] = (Gate.together(alone, group.references),)
        self._publish_accepted(entries, (*joint, self._references[key]) if group.references else joint)

    def _publish_accepted(self, entries: tuple[tuple[str, Receipt], ...], joint: tuple[Check, ...]) -> None:
        candidates = tuple(candidate for candidate, _ in entries)
        if any(candidate not in self._checked for candidate in candidates):
            raise StoreError("publication requires a checked candidate")
        try:
            if self._transaction is not None:
                raise StoreError("a publication transaction is already open")
            tables = [self._checked[candidate].contract.table for candidate in candidates]
            if len(set(tables)) != len(tables):
                raise StoreError("a publication names each table once")
            plans = [self._authorize(candidate, receipt, joint) for candidate, receipt in entries]
            self._open(candidates)
            for candidate, plan in zip(candidates, plans, strict=True):
                checked = self._checked[candidate]
                self._assert_base(checked)
                self._assert_destination_schema(checked.contract, candidate)
                self._write_rows(candidate, plan)
            self._publication_point()
            for candidate, receipt in entries:
                self._record_publication(candidate, receipt)
            self.connection.execute("COMMIT")
        except BaseException:
            for candidate in candidates:
                self.abort(candidate)
            raise
        else:
            self._end_transaction()
            for candidate in candidates:
                self._release(candidate)

    def _open(self, candidates: tuple[str, ...]) -> None:
        """Hold the lock and a transaction until the candidates commit or abort."""
        self._lock.acquire()
        self._transaction = candidates
        self.connection.execute("BEGIN TRANSACTION")

    def _record_publication(self, candidate: str, receipt: Receipt) -> None:
        table = self._checked[candidate].contract.table
        sequence = int(self._one("SELECT count(*) FROM __dogana_commits WHERE table_name=?", [table])[0]) + 1
        history, columns = self._record_history(candidate, receipt, sequence)
        self._save_receipt(receipt)
        self.connection.execute(
            "INSERT INTO __dogana_commits VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                receipt.publication_id,
                table,
                receipt.manifest.identity,
                receipt.base_publication_id,
                receipt.identity,
                sequence,
                history,
                list(columns),
            ],
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO __dogana_publications VALUES (?, ?)", [table, receipt.publication_id]
        )
        # Authorization made this receipt from the checked base, so applying its manifest gives its ledger.
        self._store_ledger(self._checked[candidate].base.apply(receipt.manifest))

    @_synchronized
    def check_references(self, group: Group, candidates: Mapping[str, str]) -> Check:
        """For each reference an orphan would disprove, find the child rows left without a parent row."""
        if self._transaction is not None:
            raise StoreError("publish or abort the existing candidate first")
        if set(candidates) != set(group.tables):
            raise StoreError("references need one candidate per table of the group")
        for table, candidate in candidates.items():
            checked = self._checked.get(candidate)
            if (
                checked is None
                or checked.contract != group.contract(table)
                or not all(c.passed for c in checked.checks)
            ):
                raise StoreError("references need candidates that passed their checks")
        after = {
            table: self._checked[candidate].base.apply(self._candidates[candidate].manifest)
            for table, candidate in candidates.items()
        }
        outcomes: list[str] = []
        orphaned = False
        self._open(tuple(candidates.values()))
        try:
            for candidate in candidates.values():
                manifest = self._candidates[candidate].manifest
                self._write_rows(candidate, ApplyPlan.build(self._checked[candidate].contract, manifest))
            for reference in group.references:
                child = self._candidates[candidates[reference.table]].manifest
                reason = group.unchecked(reference, after, child)
                held, outcome = (
                    (True, f"not checked, {reason}") if reason else self._orphans(reference, group, child)
                )
                orphaned |= not held
                outcomes.append(f"{reference}: {outcome}")
        finally:
            interruption = self._rollback()
            if interruption is not None and not isinstance(interruption, Exception):
                raise interruption
        result = Check("references", not orphaned, "; ".join(outcomes))
        self._references[group.references, frozenset(candidates.values())] = result
        return result

    def _orphans(self, reference: Reference, group: Group, manifest: Manifest) -> tuple[bool, str]:
        """Whether every child row in the delivered coverage names a parent row, inside the trial."""
        child, parent = group.contract(reference.table), group.contract(reference.parent)
        columns = dict(self._description(child.table))
        keys = dict(self._description(parent.table))
        missing = [name for name in reference.columns if name not in columns]
        if missing:
            return False, f"{child.table} has no column {', '.join(missing)}"
        named = ", ".join(f"c.{_quote(name)}" for name in reference.columns)
        present = " AND ".join(f"c.{_quote(name)} IS NOT NULL" for name in reference.columns)
        scope = f"FROM {_quote(child.table)} c WHERE {present}"
        window = coverage_window(manifest.check(child))
        params: list[object] = []
        if window is not None:
            assert child.window_column is not None
            inside, params = _inside(f"c.{_quote(child.window_column)}", window)
            scope += f" AND {inside}"
        # Numbers compare across int and float; another mismatch is a declaration error when a row names one.
        pairs = list(zip(reference.columns, parent.key, strict=True))
        mismatched = [
            (name, key)
            for name, key in pairs
            if len(kinds := {_logical(columns[name]), _logical(keys[key])}) > 1 and kinds != {"int", "float"}
        ]
        if mismatched and self._one(f"SELECT count(*) {scope}", params)[0]:
            name, key = mismatched[0]
            return False, f"{name} and {parent.table}.{key} have different types"
        match = " AND ".join(
            _names(f"c.{_quote(name)}", f"p.{_quote(key)}", columns[name], keys[key]) for name, key in pairs
        )
        where = f"{scope} AND NOT EXISTS (SELECT 1 FROM {_quote(parent.table)} p WHERE {match})"
        orphans = int(self._one(f"SELECT count(*) {where}", params)[0])
        if not orphans:
            if window is None:
                return True, "every row names a parent row"
            return (
                True,
                f"every row with {child.window_column} from {window[0]} to {window[1]} names a parent row",
            )
        sample = self.connection.execute(
            f"SELECT DISTINCT {named} {where} ORDER BY ALL LIMIT 3", params
        ).fetchall()
        values = ", ".join(str(row[0]) if len(row) == 1 else str(row) for row in sample)
        names = ", ".join(reference.columns)
        return False, f"rows without a parent row in {parent.table}: {orphans}, naming {names} {values}"

    def _publish_rejection(self, candidate: str | None, receipt: Receipt) -> None:
        self._require_committed_read()
        try:
            if candidate is not None:
                held = self._candidates.get(candidate)
                if (
                    held is None
                    or held.manifest != receipt.manifest
                    or held.contract not in (None, receipt.contract)
                ):
                    raise StoreError("receipt belongs to another candidate")
            current = self._base(receipt.contract, receipt.manifest)
            if (
                current.identity != receipt.ledger_identity
                or current.publication_id != receipt.base_publication_id
            ):
                raise StoreError("base publication moved before rejection was recorded")
            self._save_receipt(receipt)
        finally:
            if candidate is not None:
                self.abort(candidate)

    def _record_history(self, candidate: str, receipt: Receipt, sequence: int) -> tuple[str, tuple[str, ...]]:
        contract, db = receipt.contract, self.connection
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
                    inside, bounds = _inside(_quote(contract.window_column), action.window)
                    db.execute(
                        f"UPDATE {history} SET __dogana_until=? WHERE __dogana_until IS NULL AND {inside}",
                        [sequence, *bounds],
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

    def _forget_references(self, candidate: str) -> None:
        self._references = {key: check for key, check in self._references.items() if candidate not in key[1]}

    def _copy(self, candidate: str) -> Path:
        return Path(self._scratch.name) / candidate

    def _release(self, candidate: str) -> None:
        if candidate not in self._candidates:
            return
        self._copy(candidate).unlink(missing_ok=True)
        self._forget_references(candidate)
        self.connection.execute(f"DROP TABLE IF EXISTS {_quote(candidate)}")
        self.connection.execute(f"DROP TABLE IF EXISTS {_quote(_deleted(candidate))}")
        self._candidates.pop(candidate, None)
        self._checked.pop(candidate, None)

    @_synchronized
    def abort(self, candidate: str) -> None:
        interruption = (
            self._rollback() if self._transaction is not None and candidate in self._transaction else None
        )
        self._release(candidate)
        if interruption is not None and not isinstance(interruption, Exception):
            raise interruption

    def _rollback(self) -> BaseException | None:
        """Roll back and unlock, returning any interruption; an unresolved rollback stays locked."""
        interruption: BaseException | None = None
        try:
            self.connection.execute("ROLLBACK")
        except BaseException as exc:
            if not isinstance(exc, duckdb.TransactionException) or "no transaction is active" not in str(exc):
                interruption = exc
                try:
                    self.connection.execute("ROLLBACK")
                except duckdb.TransactionException as retry:
                    if "no transaction is active" not in str(retry):
                        raise
        self._end_transaction()
        return interruption

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

    @_synchronized
    def read_together(self, group: Group) -> tuple[dict[str, list[dict[str, object]]], dict[str, Ledger]]:
        """Read every table of a group with its evidence, at one moment of the store."""
        publications = {contract.table: self.read_publication(contract) for contract in group.contracts}
        return (
            {table: rows for table, (rows, _) in publications.items()},
            {table: ledger for table, (_, ledger) in publications.items()},
        )
