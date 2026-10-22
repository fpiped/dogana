"""Versioned, canonical JSON for protocol values."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from hashlib import sha256
from typing import ClassVar, Self


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
        result = decode(value)
        if not isinstance(result, cls):
            raise SerializationError(f"expected {cls.__name__}")
        return result

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
    return value.astimezone(UTC)


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
    if set(value) == {"$datetime"}:
        return utc(datetime.fromisoformat(value["$datetime"]))
    if set(value) == {"$date"}:
        return date.fromisoformat(value["$date"])
    if set(value) == {"$seconds"}:
        return timedelta(seconds=value["$seconds"])
    if "kind" in value or "version" in value:
        if type(value.get("version")) is not int or value["version"] != 1:
            raise SerializationError("unsupported wire version")
        cls = _TYPES.get(value.get("kind"))
        if cls is None:
            raise SerializationError("unknown wire kind")
        try:
            return cls(**{k: decode(v) for k, v in value.items() if k not in ("kind", "version")})
        except (TypeError, ValueError, KeyError) as exc:
            raise SerializationError(str(exc)) from exc
    return {k: decode(v) for k, v in value.items()}
