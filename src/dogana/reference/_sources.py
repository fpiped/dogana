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


def _open(path: str | PathLike[str], root: Path | None = None) -> BufferedReader:
    """Open a delivered file once, refusing anything but a regular file inside the root, if one is given.

    The path is resolved before it is opened and the last step is opened without following a link, so a
    link swapped in after the check is refused instead of read.
    """
    resolved = Path(path).resolve(strict=True)
    if root is not None and not resolved.is_relative_to(root):
        raise InvalidContract("delivered files must stay inside the drop root")
    try:
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise InvalidContract(f"the delivered file cannot be opened: {exc.strerror}") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise InvalidContract("delivered files must be regular files")
    return os.fdopen(descriptor, "rb")


def _snapshot(path: str | PathLike[str], copy: Path, root: Path | None = None) -> str:
    """Copy a delivered file once while hashing it, so every later read sees exactly the bytes hashed."""
    digest = sha256()
    with _open(path, root) as stream, copy.open("xb") as target:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
            target.write(chunk)
    return digest.hexdigest()


# The one CSV dogana reads (RFC 4180: comma, double quote, UTF-8, a header row), so a sender's count
# and the store's agree; Python's default dialect is the same grammar.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# The spellings DuckDB casts to BOOLEAN, so a sender's count and the store's agree.
_BOOLEANS = {"true": True, "t": True, "yes": True, "y": True, "1": True}
_BOOLEANS |= {"false": False, "f": False, "no": False, "n": False, "0": False}


def _count_rows(path: Path, deleted_column: str | None = None) -> tuple[int, int]:
    """Live rows and tombstones: a row is a tombstone when its deleted column is true."""
    if path.suffix.lower() == ".csv":
        live = tombstones = 0
        try:
            with path.open(newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream, strict=True)
                marked = deleted_column is not None and deleted_column in (reader.fieldnames or ())
                for row in reader:
                    deleted = _BOOLEANS.get((row[deleted_column] or "").lower()) if marked else False
                    if deleted is None:
                        raise InvalidContract(f"{deleted_column} must be true or false in every row")
                    live, tombstones = live + (not deleted), tombstones + deleted
        except (UnicodeDecodeError, csv.Error) as exc:
            raise InvalidContract(f"{path.name} is not UTF-8 CSV: {exc}") from exc
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
class _RowSource:
    rows: Iterable[Mapping[str, object]]
    columns: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", _columns(self.columns))
