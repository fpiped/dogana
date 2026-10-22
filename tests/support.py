from datetime import UTC, datetime, timedelta

from dogana import Checkpoint, Column, Completeness, Contract, Coverage, Deletions, Shape

FULL = Shape(Coverage.ALL, Completeness.COMPLETE, Deletions.ABSENCE, Checkpoint.NONE, cost=100)
WINDOW = Shape(
    Coverage.WINDOW,
    Completeness.COMPLETE,
    Deletions.ABSENCE,
    Checkpoint.NONE,
    cost=10,
    window_size=timedelta(days=30),
)
SINCE = Shape(Coverage.ALL, Completeness.CHANGED_ONLY, Deletions.NONE, Checkpoint.TIMESTAMP, cost=2)
RECENT = Shape(Coverage.WINDOW, Completeness.CHANGED_ONLY, Deletions.NONE, Checkpoint.TIMESTAMP, cost=2)
FEED = Shape(Coverage.KEYS, Completeness.CHANGED_ONLY, Deletions.TOMBSTONE, Checkpoint.GENERATION, cost=1)
SCHEMA = (Column("id", "int"), Column("day", "date"), Column("value", "str"))


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


def contract(table="moves", *, shapes=None, schema=SCHEMA, **kwargs):
    shapes = shapes or {"full": FULL, "daily": WINDOW, "since": SINCE, "recent": RECENT, "feed": FEED}
    windowed = any(s.coverage is Coverage.WINDOW for s in shapes.values())
    return Contract(table, ("id",), shapes, "day" if windowed else None, schema, **kwargs)
