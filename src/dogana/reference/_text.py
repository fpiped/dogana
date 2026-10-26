"""Typed values parsed from text, shared by filename patterns and the command line."""

from datetime import date, datetime, tzinfo

from dogana import Checkpoint, Contract, InvalidContract, Shape
from dogana.contract import Scalar
from dogana.wire import utc


def _instant(value: str, timezone: tzinfo | None = None, *, remedy: str = "") -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidContract(f"{value!r} is not an ISO 8601 time") from exc
    if result.tzinfo is not None:
        return utc(result)
    if timezone is None:
        raise InvalidContract(f"{value!r} needs a UTC offset such as Z or +02:00{remedy}")
    local = result.replace(tzinfo=timezone)
    instant = utc(local)
    if instant.astimezone(timezone).replace(tzinfo=None) != result:
        raise InvalidContract("nonexistent local time needs an explicit UTC offset")
    if local.utcoffset() != local.replace(fold=1).utcoffset():
        raise InvalidContract("ambiguous local time needs an explicit UTC offset")
    return instant


def _integer(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise InvalidContract(f"{value!r} is not an integer") from exc


def _scalar(value: str, logical_type: str, timezone: tzinfo | None = None, *, remedy: str = "") -> Scalar:
    if logical_type == "datetime":
        return _instant(value, timezone, remedy=remedy)
    if logical_type == "date":
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise InvalidContract(f"{value!r} is not an ISO 8601 date") from exc
    if logical_type == "int":
        return _integer(value)
    if logical_type == "str":
        return value
    raise InvalidContract(f"{logical_type!r} values cannot be parsed from text")


def _checkpoint(
    value: str, shape: Shape, timezone: tzinfo | None = None, *, remedy: str = ""
) -> str | datetime | int:
    if shape.checkpoint is Checkpoint.TIMESTAMP:
        return _instant(value, timezone, remedy=remedy)
    return _integer(value) if shape.checkpoint is Checkpoint.SEQUENCE else value


def _position(value: str, contract: Contract) -> str | int:
    sequenced = any(shape.checkpoint is Checkpoint.SEQUENCE for shape in contract.shapes.values())
    return _integer(value) if sequenced else value
