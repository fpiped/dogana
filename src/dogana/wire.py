"""Versioned, canonical JSON for protocol values."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from functools import cache
from hashlib import sha256
from types import UnionType
from typing import ClassVar, Self, Union, get_args, get_origin, get_type_hints


class InvalidContract(ValueError):
    """A declaration contradicts the protocol."""


class SerializationError(InvalidContract):
    """A wire value has an unsupported version, kind or representation."""


_TYPES: dict[str, type[Wire]] = {}


class Wire:
    version: ClassVar[int] = 1

    def __init_subclass__(cls) -> None:
        _TYPES[cls.__name__] = cls

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "kind": type(self).__name__,
            **{f.name: encode(getattr(self, f.name)) for f in fields(self)},
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), indent=indent, allow_nan=False
        )

    @classmethod
    def from_dict(cls, value: dict) -> Self:
        try:
            result = decode(value)
            if not isinstance(result, cls):
                raise SerializationError(f"expected {cls.__name__}")
            return result
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as exc:
            raise SerializationError(str(exc)) from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            return cls.from_dict(json.loads(value))
        except (TypeError, ValueError, KeyError) as exc:
            raise SerializationError(str(exc)) from exc

    @property
    def identity(self) -> str:
        return sha256(self.to_json().encode()).hexdigest()


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidContract("timestamps must be timezone-aware datetimes")
    try:
        return value.astimezone(UTC)
    except OverflowError as exc:
        raise InvalidContract("timestamp is outside the UTC datetime range") from exc


def encode(value):
    if isinstance(value, Wire) and is_dataclass(value):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return {"$datetime": utc(value).isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, timedelta):
        return {"$seconds": value.total_seconds()}
    if isinstance(value, Mapping):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [encode(v) for v in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise SerializationError(f"unsupported value type: {type(value).__name__}")


def decode(value):
    if isinstance(value, list):
        return tuple(decode(v) for v in value)
    if not isinstance(value, dict):
        return value
    if set(value) == {"$datetime"} and isinstance(value["$datetime"], str):
        return utc(datetime.fromisoformat(value["$datetime"]))
    if set(value) == {"$date"} and isinstance(value["$date"], str):
        return date.fromisoformat(value["$date"])
    if set(value) == {"$seconds"} and type(value["$seconds"]) in (int, float):
        return timedelta(seconds=value["$seconds"])
    if isinstance(value.get("kind"), str) or type(value.get("version")) in (int, bool):
        if type(value.get("version")) is not int or value["version"] != 1:
            raise SerializationError("unsupported wire version")
        cls = _TYPES.get(value.get("kind"))
        if cls is None:
            raise SerializationError("unknown wire kind")
        try:
            names = {f.name for f in fields(cls)}
            if set(value) != names | {"kind", "version"}:
                raise SerializationError(f"fields differ from {cls.__name__}")
            decoded = {k: decode(v) for k, v in value.items() if k not in ("kind", "version")}
            hints = _hints(cls)
            for name, item in decoded.items():
                if name not in hints or not _matches(item, hints[name]):
                    raise SerializationError(f"invalid type for {cls.__name__}.{name}")
            result = cls(**decoded)
            for f in fields(result):
                if not _matches(getattr(result, f.name), hints[f.name]):
                    raise SerializationError(f"invalid type for {cls.__name__}.{f.name}")
            return result
        except (TypeError, ValueError, KeyError) as exc:
            raise SerializationError(str(exc)) from exc
    return {k: decode(v) for k, v in value.items()}


@cache
def _hints(cls):
    return get_type_hints(cls)


def _matches(value, annotation) -> bool:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        return any(_matches(value, option) for option in args)
    if origin is tuple:
        if not isinstance(value, tuple):
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_matches(v, args[0]) for v in value)
        return len(value) == len(args) and all(_matches(v, t) for v, t in zip(value, args, strict=True))
    if origin is Mapping:
        return isinstance(value, Mapping) and all(
            _matches(k, args[0]) and _matches(v, args[1]) for k, v in value.items()
        )
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        try:
            annotation(value)
            return True
        except (TypeError, ValueError):
            return False
    if annotation in (str, int, bool):
        return type(value) is annotation
    if annotation is float:
        return type(value) in (int, float) and float("-inf") < value < float("inf")
    return isinstance(value, annotation)
