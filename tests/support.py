from datetime import UTC, datetime, timedelta

from dogana import Checkpoint, Column, Completeness, Contract, Coverage, Deletions, Shape

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
    coverage=Coverage.KEYS,
    completeness=Completeness.CHANGED_ONLY,
    deletions=Deletions.TOMBSTONE,
    checkpoint=Checkpoint.GENERATION,
    cost=1,
)
SCHEMA = (Column("id", "int"), Column("day", "date"), Column("value", "str"))


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


def contract(table="moves", *, shapes=None, schema=SCHEMA, **kwargs):
    shapes = shapes or {"full": FULL, "daily": WINDOW, "since": SINCE, "recent": RECENT, "feed": FEED}
    windowed = any(s.coverage is Coverage.WINDOW for s in shapes.values())
    return Contract(
        table, ("id",), shapes, window_column="day" if windowed else None, schema=schema, **kwargs
    )
