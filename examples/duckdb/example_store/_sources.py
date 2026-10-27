"""Explicit transport schemas, separate from delivery contracts."""

import csv
import os
import re
import stat
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import file_digest, sha256
from io import BufferedReader
from json import dumps
from os import PathLike, fspath
from pathlib import Path
from types import MappingProxyType

from dogana.contract import LOGICAL_TYPES, Contract, identifier
from dogana.errors import InvalidContract

_DECIMAL = re.compile(r"decimal\(([0-9]{1,2}),([0-9]{1,2})\)")


def _decimal(kind: str) -> tuple[int, int] | None:
    """The precision and scale of an exact `decimal(p,s)` column; None for any other type."""
    match = _DECIMAL.fullmatch(kind)
    if match is None:
        return None
    precision, scale = int(match[1]), int(match[2])
    return (precision, scale) if 1 <= precision <= 38 and scale <= precision else None


def _columns(columns: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(columns, Mapping) or not columns:
        raise InvalidContract("source columns must be a nonempty mapping")
    result = dict(columns)
    for name, kind in result.items():
        identifier(name)
        if not isinstance(kind, str) or (kind not in LOGICAL_TYPES and _decimal(kind) is None):
            raise InvalidContract(f"source column {name} has an unsupported type")
    if len({name.casefold() for name in result}) != len(result):
        raise InvalidContract("source column names must be distinct ignoring case")
    return MappingProxyType(result)


def _published(columns: Mapping[str, str], contract: Contract) -> dict[str, str]:
    """The columns rows are published with: every delivered column but the deleted marker."""
    return {name: kind for name, kind in columns.items() if name != contract.deleted_column}


def _column_hash(columns: Mapping[str, str]) -> str:
    return sha256(
        dumps(list(columns.items()), ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _file_digest(path: str | PathLike[str], root: Path | None = None) -> str:
    with _open(path, root) as stream:
        return file_digest(stream, "sha256").hexdigest()


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
_FOLDER = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
_BENEATH = bool(_NOFOLLOW) and os.open in os.supports_dir_fd


def _open(path: str | PathLike[str], root: Path | None = None) -> BufferedReader:
    """Open a delivered file once: a regular file, inside the root when one is given.

    Below a root every step is opened relative to the folder above it without following a link, so a
    folder or file swapped for a link after the check is refused instead of read. A platform without
    that (Windows) gets a weaker check: the path must still resolve to the file that was opened.
    """
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InvalidContract(f"{path}: no such file") from exc
    if root is not None and not resolved.is_relative_to(root):
        raise InvalidContract("delivered files must stay inside the drop root")
    try:
        if root is not None and _BENEATH:
            descriptor = _beneath(root, resolved.relative_to(root).parts)
        else:
            descriptor = os.open(resolved, _FLAGS)
            if root is not None and (
                Path(path).resolve() != resolved
                or not os.path.samestat(os.fstat(descriptor), resolved.stat())
            ):
                os.close(descriptor)
                raise InvalidContract("the delivered file changed while it was opened")
    except OSError as exc:
        raise InvalidContract(f"the delivered file cannot be opened: {exc.strerror}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise InvalidContract("delivered files must be regular files")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _beneath(root: Path, parts: tuple[str, ...]) -> int:
    folder = os.open(root, _FOLDER)
    try:
        for part in parts[:-1]:
            inner = os.open(part, _FOLDER, dir_fd=folder)
            os.close(folder)
            folder = inner
        return os.open(parts[-1], _FLAGS, dir_fd=folder)
    finally:
        os.close(folder)


def _snapshot(path: str | PathLike[str], copy: Path, root: Path | None = None) -> str:
    """Copy a delivered file once while hashing it, so every later read sees exactly the bytes hashed."""
    digest = sha256()
    with _open(path, root) as stream, copy.open("xb") as target:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
            target.write(chunk)
    return digest.hexdigest()


# The spellings DuckDB casts to BOOLEAN, so a sender's count and the store's agree.
_BOOLEANS = {"true": True, "t": True, "yes": True, "y": True, "1": True}
_BOOLEANS |= {"false": False, "f": False, "no": False, "n": False, "0": False}


def _count_rows(path: Path, deleted_column: str | None = None) -> tuple[int, int]:
    """Live rows and tombstones: a row is a tombstone when its deleted column is true."""
    if path.suffix.lower() == ".csv":
        # RFC 4180 is Python's default dialect too. A one-column file's blank line is an empty value,
        # as DuckDB reads it; any other short or long record is malformed.
        live = tombstones = 0
        limit = csv.field_size_limit(min(sys.maxsize, 2**31 - 1))
        try:
            with path.open(newline="", encoding="utf-8-sig") as stream:
                reader = csv.reader(stream, strict=True)
                header = next(reader, [])
                position = header.index(deleted_column) if deleted_column in header else None
                for record in reader:
                    record = record or ([""] if len(header) == 1 else record)
                    if len(record) != len(header):
                        raise InvalidContract(
                            f"{path.name}: a record has {len(record)} fields, the header {len(header)}"
                        )
                    deleted = _BOOLEANS.get(record[position].lower()) if position is not None else False
                    if deleted is None:
                        raise InvalidContract(f"{deleted_column} must be true or false in every row")
                    live, tombstones = live + (not deleted), tombstones + deleted
        except (UnicodeDecodeError, csv.Error) as exc:
            raise InvalidContract(f"{path.name} is not UTF-8 CSV: {exc}") from exc
        finally:
            csv.field_size_limit(limit)
        return live, tombstones
    if path.suffix.lower() == ".parquet":
        import duckdb

        with duckdb.connect() as connection:
            try:
                described = connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
                ).fetchall()
            except duckdb.Error as exc:
                raise InvalidContract(f"{path.name} is not a readable Parquet file") from exc
            kinds = {name: kind for name, kind, *_ in described}
            if deleted_column in kinds and kinds[deleted_column] != "BOOLEAN":
                raise InvalidContract(f"{deleted_column} must be a BOOLEAN column")
            marker = (
                '"' + deleted_column.replace('"', '""') + '"'
                if deleted_column is not None and deleted_column in kinds
                else "false"
            )
            counts = connection.execute(
                f"SELECT count(*) FILTER (NOT {marker}), count(*) FILTER ({marker}), "
                f"count(*) FILTER ({marker} IS NULL) FROM read_parquet(?)",
                [str(path)],
            ).fetchone()
            if counts is None:
                raise InvalidContract("Parquet row count is unavailable")
            if counts[2]:
                raise InvalidContract(f"{deleted_column} must be true or false in every row")
            return int(counts[0]), int(counts[1])
    raise InvalidContract("row counting supports CSV and Parquet files")


@dataclass(frozen=True)
class _FileSource(PathLike[str]):
    path: str | PathLike[str]
    columns: Mapping[str, str]
    root: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", _columns(self.columns))

    def __fspath__(self) -> str:
        return fspath(self.path)


@dataclass(frozen=True)
class Rows:
    """Row mappings with their column types, so even an empty or all-null batch has a schema."""

    rows: Iterable[Mapping[str, object]]
    columns: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", _columns(self.columns))
