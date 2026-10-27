"""Explicit transport schemas, separate from delivery contracts."""

import csv
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import file_digest, sha256
from json import dumps
from os import PathLike, fspath
from pathlib import Path
from types import MappingProxyType

from dogana.contract import LOGICAL_TYPES, Contract, identifier
from dogana.errors import InvalidContract


def _columns(columns: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(columns, Mapping) or not columns:
        raise InvalidContract("source columns must be a nonempty mapping")
    result = dict(columns)
    for name, kind in result.items():
        identifier(name)
        if not isinstance(kind, str) or kind not in LOGICAL_TYPES:
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


def _file_digest(path: str | PathLike[str]) -> str:
    with open(path, "rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


# The spellings DuckDB casts to BOOLEAN, so a sender's count and the store's agree.
_BOOLEANS = {"true": True, "t": True, "yes": True, "y": True, "1": True}
_BOOLEANS |= {"false": False, "f": False, "no": False, "n": False, "0": False}


def _count_rows(path: Path, deleted_column: str | None = None) -> tuple[int, int]:
    """Live rows and tombstones: a row is a tombstone when its deleted column is true."""
    if path.suffix.lower() == ".csv":
        live = tombstones = 0
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            marked = deleted_column is not None and deleted_column in (reader.fieldnames or ())
            for row in reader:
                deleted = _BOOLEANS.get((row[deleted_column] or "").lower()) if marked else False
                if deleted is None:
                    raise InvalidContract(f"{deleted_column} must be true or false in every row")
                live, tombstones = live + (not deleted), tombstones + deleted
        return live, tombstones
    if path.suffix.lower() == ".parquet":
        import duckdb

        with duckdb.connect() as connection:
            described = connection.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
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
