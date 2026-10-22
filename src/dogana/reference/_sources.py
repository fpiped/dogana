"""Explicit transport schemas, separate from delivery contracts."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from json import dumps
from os import PathLike, fspath
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
