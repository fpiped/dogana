import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from dogana import Checkpoint, Completeness, Contract, Coverage, Deletions, Shape

FULL = Shape(
    coverage=Coverage.ALL,
    completeness=Completeness.COMPLETE,
    deletions=Deletions.ABSENCE,
    checkpoint=Checkpoint.NONE,
    cost=100,
)
WINDOW = Shape(
    coverage=Coverage.WINDOW,
    completeness=Completeness.COMPLETE,
    deletions=Deletions.ABSENCE,
    checkpoint=Checkpoint.NONE,
    cost=10,
    window_size=timedelta(days=30),
)
SINCE = Shape(
    coverage=Coverage.ALL,
    completeness=Completeness.CHANGED_ONLY,
    deletions=Deletions.NONE,
    checkpoint=Checkpoint.TIMESTAMP,
    cost=2,
)
RECENT = Shape(
    coverage=Coverage.WINDOW,
    completeness=Completeness.CHANGED_ONLY,
    deletions=Deletions.NONE,
    checkpoint=Checkpoint.TIMESTAMP,
    cost=2,
)
FEED = Shape(
    coverage=Coverage.ALL,
    completeness=Completeness.CHANGED_ONLY,
    deletions=Deletions.TOMBSTONE,
    checkpoint=Checkpoint.GENERATION,
    cost=1,
)
COLUMNS = {"id": "int", "day": "date", "value": "str"}


def schema_hash(columns):
    return sha256(
        json.dumps(list(columns.items()), ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


def contract(table="moves", *, shapes=None, columns=COLUMNS, types=None, **kwargs):
    shapes = shapes or {"full": FULL, "daily": WINDOW, "since": SINCE, "recent": RECENT, "feed": FEED}
    windowed = any(s.coverage is Coverage.WINDOW for s in shapes.values())
    if any(s.deletions is Deletions.TOMBSTONE for s in shapes.values()):
        kwargs.setdefault("deleted_column", "deleted")
    bound = {"id"} | ({"day"} if windowed else set())
    for column in ("cursor_column", "deleted_column"):
        if kwargs.get(column) is not None:
            bound.add(kwargs[column])
    if types is None:
        types = {
            name: "bool" if name == kwargs.get("deleted_column") else (columns or COLUMNS)[name]
            for name in bound
        }
    kwargs.setdefault("schema_hash", schema_hash(columns) if columns is not None else None)
    return Contract(table, ("id",), shapes, window_column="day" if windowed else None, types=types, **kwargs)


def accounting_contract(table="moves", *, window_type="int", shapes=None, **kwargs):
    from dataclasses import replace

    shapes = shapes or {"full": FULL, "daily": WINDOW, "since": SINCE, "recent": RECENT, "feed": FEED}
    if window_type in ("int", "str"):
        shapes = {name: replace(shape, window_size=None) for name, shape in shapes.items()}
    bound = {"id": "int"}
    if any(shape.coverage is Coverage.WINDOW for shape in shapes.values()):
        bound["day"] = window_type
    if any(shape.deletions is Deletions.TOMBSTONE for shape in shapes.values()):
        bound["deleted"] = "bool"
    kwargs.setdefault("columns", None)
    kwargs.setdefault("types", bound)
    return contract(table, shapes=shapes, **kwargs)
