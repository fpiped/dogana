"""Explicit transport schemas, separate from delivery contracts."""

import csv
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import file_digest, sha256
from json import dumps
from os import PathLike, fspath
from pathlib import Path
from types import MappingProxyType

from dogana.contract import LOGICAL_TYPES, identifier
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


def _column_hash(columns: Mapping[str, str]) -> str:
    return sha256(
        dumps(list(columns.items()), ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _file_digest(path: str | PathLike[str]) -> str:
    with open(path, "rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _count_rows(path: Path) -> int:
    if path.suffix.lower() == ".csv":
        with path.open(newline="") as stream:
            return sum(1 for _ in csv.DictReader(stream))
    if path.suffix.lower() == ".parquet":
        import duckdb

        with duckdb.connect() as connection:
            row = connection.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()
            if row is None or not isinstance(row[0], int):
                raise InvalidContract("Parquet row count is unavailable")
            return row[0]
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
