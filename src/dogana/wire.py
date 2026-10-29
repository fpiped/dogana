"""Strict versioned JSON and stable identities for protocol values."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import MISSING, fields, is_dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from functools import cache
from hashlib import sha256
from types import MappingProxyType, UnionType
from typing import ClassVar, Protocol, Self, TypeAlias, Union, cast, get_args, get_origin, get_type_hints

from .errors import InvalidContract as InvalidContract
from .errors import SerializationError as SerializationError

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


class Wire:
    version: ClassVar[int] = 4

    @classmethod
    def _wire_schema(cls) -> type[Wire]:
        return cls

    def __post_init__(self) -> None:
        schema = self._wire_schema()
        if not is_dataclass(schema):
            raise InvalidContract("wire values must be dataclasses")
        hints = _hints(type(self))
        for field in fields(schema):
            value = getattr(self, field.name)
            annotation = hints[field.name]
            if not _matches(value, annotation, lists=True):
                expected = str(annotation).replace("typing.", "").replace("dogana.", "")
                raise InvalidContract(
                    f"invalid value for {type(self).__name__}.{field.name}={value!r}: expected {expected}"
                )
            object.__setattr__(self, field.name, _normalize(value, annotation))

    def _document(self, *, identity: bool) -> dict[str, JSONValue]:
        kind = type(self).__name__
        schema = self._wire_schema()
        if not is_dataclass(schema) or _registry().get(kind) is not type(self):
            raise SerializationError("unknown wire kind")
        result: dict[str, JSONValue] = {"kind": kind}
        if not identity:
            result["version"] = self.version
        for field in fields(schema):
            value = getattr(self, field.name)
            if identity:
                if kind == "Receipt" and field.name == "outcome" and value == "replayed":
                    value = "accepted"
                default = field.default
                if field.default_factory is not MISSING:
                    default = field.default_factory()
                if default is not MISSING and value == default:
                    continue
            encoded = _encode(value, identity=identity)
            result[field.name] = encoded
        return result

    def to_dict(self) -> dict[str, JSONValue]:
        return self._document(identity=False)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            indent=indent,
            allow_nan=False,
        )

    def identity_dict(self) -> dict[str, JSONValue]:
        return self._document(identity=True)

    def identity_json(self) -> str:
        return canonical_json(self.identity_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        try:
            result = decode(value)
            if not isinstance(result, cls):
                raise SerializationError(f"expected {cls.__name__}")
            return result
        except SerializationError:
            raise
        except (TypeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
            raise SerializationError(str(exc)) from exc

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            if type(value) is not str:
                raise SerializationError("wire JSON must be a string")
            raw = json.loads(value, object_pairs_hook=_object_pairs, parse_constant=_invalid_constant)
            return cls.from_dict(raw)
        except SerializationError:
            raise
        except (TypeError, ValueError, RecursionError) as exc:
            raise SerializationError(str(exc)) from exc

    @property
    def identity(self) -> str:
        return sha256(self.identity_json().encode("utf-8")).hexdigest()


def utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidContract("timestamps must be timezone-aware datetimes")
    try:
        return value.astimezone(UTC)
    except OverflowError as exc:
        raise InvalidContract("timestamp is outside the UTC datetime range") from exc


def canonical_json(value: JSONValue) -> str:
    """RFC 8785 JSON, rejecting integers that would lose precision in binary64."""
    try:
        if value is None:
            return "null"
        if type(value) is bool:
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return _number(value)
        if isinstance(value, str):
            value.encode("utf-8")
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, list):
            return "[" + ",".join(canonical_json(item) for item in value) + "]"
        if isinstance(value, dict):
            if any(type(key) is not str for key in value):
                raise SerializationError("mapping keys must be strings")
            keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
            properties = (canonical_json(key) + ":" + canonical_json(value[key]) for key in keys)
            return "{" + ",".join(properties) + "}"
        raise SerializationError(f"unsupported JSON value: {type(value).__name__}")
    except UnicodeError as exc:
        raise SerializationError("strings must contain only Unicode scalar values") from exc
    except RecursionError as exc:
        raise SerializationError("JSON containers are recursive or too deeply nested") from exc


def _number(value: int | float) -> str:
    try:
        number = float(value)
    except OverflowError as exc:
        raise SerializationError("number is outside the binary64 range") from exc
    if not math.isfinite(number):
        raise SerializationError("numbers must be finite")
    if number != value:
        raise SerializationError("integer cannot be represented exactly as binary64")
    if not number:
        return "0"
    sign = "-" if number < 0 else ""
    mantissa, _, exponent = repr(abs(number)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    leading = len(digits) - len(digits.lstrip("0"))
    point = len(whole) + int(exponent or "0") - leading
    digits = digits.lstrip("0").rstrip("0")
    if 0 < point <= 21:
        if point >= len(digits):
            return sign + digits + "0" * (point - len(digits))
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * -point + digits
    coefficient = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
    return sign + coefficient + "e" + ("+" if point > 0 else "") + str(point - 1)


def _encode(value: object, *, identity: bool) -> JSONValue:
    if isinstance(value, Wire):
        return value._document(identity=identity)
    if isinstance(value, Enum):
        return _encode(value.value, identity=identity)
    if isinstance(value, datetime):
        return {"$datetime": utc(value).isoformat()}
    if type(value) is date:
        return {"$date": value.isoformat()}
    if isinstance(value, timedelta):
        return {"$seconds": _encode(_seconds(value), identity=identity)}
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise SerializationError("mapping keys must be strings")
        return {key: _encode(item, identity=identity) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode(item, identity=identity) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SerializationError("numbers must be finite")
        return int(value) if identity and value.is_integer() else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise SerializationError(f"unsupported value type: {type(value).__name__}")


class _Constructor(Protocol):
    def __call__(self, **kwargs: object) -> Wire: ...


def decode(value: object) -> object:
    if isinstance(value, list):
        return tuple(decode(item) for item in value)
    if not isinstance(value, Mapping):
        if value is None or type(value) in (str, int, bool):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        raise SerializationError("unsupported JSON value")
    if any(type(key) is not str for key in value):
        raise SerializationError("mapping keys must be strings")
    if set(value) == {"$datetime"} and isinstance(value["$datetime"], str):
        if _DATETIME.fullmatch(value["$datetime"]) is None:
            raise SerializationError(
                "a datetime tag is RFC 3339 with an offset and at most six fractional digits"
            )
        return utc(datetime.fromisoformat(value["$datetime"]))
    if set(value) == {"$date"} and isinstance(value["$date"], str):
        if _DATE.fullmatch(value["$date"]) is None:
            raise SerializationError("a date tag is YYYY-MM-DD")
        return date.fromisoformat(value["$date"])
    if set(value) == {"$seconds"} and type(value["$seconds"]) in (int, float):
        return _duration(value["$seconds"])
    if isinstance(value.get("kind"), str) or type(value.get("version")) in (int, float, bool):
        version = _integral(value.get("version"), int)
        if type(version) is not int or version != Wire.version:
            raise SerializationError(
                f"unsupported wire version {value.get('version')!r}: this dogana reads version {Wire.version}"
            )
        kind = value.get("kind")
        cls = _registry().get(kind) if isinstance(kind, str) else None
        if cls is None:
            raise SerializationError("unknown wire kind")
        schema = cls._wire_schema()
        if not is_dataclass(schema):
            raise SerializationError("wire values must be dataclasses")
        names = {field.name for field in fields(schema)}
        missing = names - set(value)
        extra = set(value) - names - {"kind", "version"}
        if missing:
            raise SerializationError(f"missing fields for {kind}: {', '.join(sorted(missing))}")
        if extra:
            raise SerializationError(f"unknown fields for {kind}: {', '.join(sorted(extra))}")
        hints = _hints(schema)
        decoded = {name: _integral(_decode_field(value[name], hints[name]), hints[name]) for name in names}
        for name, item in decoded.items():
            if not _matches(item, hints[name]):
                raise SerializationError(f"invalid value for {kind}.{name}={item!r}")
        return cast(_Constructor, cls)(**decoded)
    return {key: decode(item) for key, item in value.items()}


def _integral(value: object, annotation: object) -> object:
    """JSON has one number type: a zero-fraction number is the integer it spells, where no float fits."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        if any(option is float for option in args):
            return value
        options = [option for option in args if option is not type(None)]
        return _integral(value, options[0]) if len(options) == 1 else _integral(value, _scalar(args))
    if origin is tuple and isinstance(value, (tuple, list)):
        hints = (args[0],) * len(value) if len(args) == 2 and args[1] is Ellipsis else args
        if len(hints) != len(value):
            return value
        return tuple(_integral(item, hint) for item, hint in zip(value, hints, strict=True))
    if annotation is not float and type(value) is float and value.is_integer() and abs(value) <= 2**53:
        return int(value)
    return value


def _scalar(options: tuple[object, ...]) -> object:
    """The one annotation of a scalar union, int when it admits an integer, else the first option."""
    return int if any(option is int for option in options) else options[0]


def _decode_field(value: object, annotation: object) -> object:
    if get_origin(annotation) is Mapping and isinstance(value, Mapping):
        item_type = get_args(annotation)[1]
        return {key: _decode_field(item, item_type) for key, item in value.items()}
    return decode(value)


@cache
def _registry() -> Mapping[str, type[Wire]]:
    from .contract import Contract, Manifest, Shape
    from .gate import Action, ApplyPlan, Check, CheckRequirement, Receipt, Transition
    from .ledger import Claims, Ledger, Region, RegionClaim
    from .planner import ExtractionRequest, Requirement

    kinds = (
        Contract,
        Manifest,
        Shape,
        Action,
        ApplyPlan,
        Check,
        CheckRequirement,
        Receipt,
        Transition,
        Claims,
        Ledger,
        Region,
        RegionClaim,
        ExtractionRequest,
        Requirement,
    )
    return MappingProxyType({kind.__name__: kind for kind in kinds})


@cache
def _hints(cls: type[Wire]) -> dict[str, object]:
    return get_type_hints(cls._wire_schema())


def _matches(value: object, annotation: object, *, lists: bool = False) -> bool:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        return any(_matches(value, option, lists=lists) for option in args)
    if origin is tuple:
        if not isinstance(value, tuple) and not (lists and isinstance(value, list)):
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_matches(item, args[0], lists=lists) for item in value)
        return len(value) == len(args) and all(
            _matches(item, hint, lists=lists) for item, hint in zip(value, args, strict=True)
        )
    if origin is Mapping:
        return isinstance(value, Mapping) and all(
            _matches(key, args[0], lists=lists) and _matches(item, args[1], lists=lists)
            for key, item in value.items()
        )
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if type(value) is not str and type(value) is not annotation:
            return False
        try:
            annotation(value)
            return True
        except (TypeError, ValueError):
            return False
    if annotation is float:
        return (
            isinstance(value, (int, float))
            and type(value) in (int, float)
            and float("-inf") < value < float("inf")
        )
    return isinstance(annotation, type) and type(value) is annotation


def _normalize(value: object, annotation: object) -> object:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        option = next(option for option in args if _matches(value, option, lists=True))
        return _normalize(value, option)
    if origin is tuple and isinstance(value, (tuple, list)):
        hints = (args[0],) * len(value) if len(args) == 2 and args[1] is Ellipsis else args
        return tuple(_normalize(item, hint) for item, hint in zip(value, hints, strict=True))
    if origin is Mapping and isinstance(value, Mapping):
        return MappingProxyType(
            {_normalize(key, args[0]): _normalize(item, args[1]) for key, item in value.items()}
        )
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if annotation is float and isinstance(value, (int, float)):
        try:
            normalized = float(value)
        except OverflowError as exc:
            raise InvalidContract("number is outside the float range") from exc
        if normalized != value:
            raise InvalidContract("number cannot be represented exactly as a float")
        return normalized
    if type(value) is int:
        _number(value)
    if isinstance(value, timedelta):
        _seconds(value)
    if isinstance(value, datetime):
        return utc(value)
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InvalidContract("strings must contain only Unicode scalar values") from exc
    return value


_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])"
)


def _duration(seconds: int | float) -> timedelta:
    """A wire duration is nonnegative and exact: it decodes to the timedelta that encodes back to it."""
    if not math.isfinite(seconds) or seconds < 0:
        raise SerializationError("a duration is a finite nonnegative number of seconds")
    try:
        value = timedelta(seconds=seconds)
    except OverflowError as exc:
        raise SerializationError("duration is outside the timedelta range") from exc
    if _seconds(value) != seconds:
        raise SerializationError("duration cannot be represented exactly in microseconds")
    return value


def _seconds(value: timedelta) -> int | float:
    seconds = value.days * 86400 + value.seconds
    if not value.microseconds:
        return seconds
    fraction = seconds + value.microseconds / 1_000_000
    try:
        if timedelta(seconds=fraction) == value:
            return fraction
    except OverflowError:
        pass
    raise InvalidContract("duration cannot be represented exactly in wire seconds")


def _object_pairs(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, value in pairs:
        if key in result:
            raise SerializationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise SerializationError(f"invalid JSON number: {value}")
