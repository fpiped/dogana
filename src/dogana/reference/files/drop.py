"""Read sidecar manifests or derive them from a declared filename pattern."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from dogana import Contract, InvalidContract, Manifest, Receipt, Store
from dogana.wire import utc


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    return utc(result if result.tzinfo is not None else result.replace(tzinfo=UTC))


@dataclass(frozen=True)
class FilePattern:
    expression: str
    shape: str | None = None

    def __post_init__(self):
        regex = re.compile(self.expression)
        if "produced_at" not in regex.groupindex:
            raise InvalidContract("a filename pattern needs a produced_at group")
        if "shape" not in regex.groupindex and not self.shape:
            raise InvalidContract("a feed needs a fixed shape or a filename shape group")
        if self.shape is not None and (not isinstance(self.shape, str) or not self.shape):
            raise InvalidContract("a fixed shape must be a nonempty name")
        if ("low" in regex.groupindex) != ("high" in regex.groupindex):
            raise InvalidContract("a filename pattern needs both window bounds groups")

    def manifest(self, filename: str | Path, contract: Contract) -> Manifest:
        path = Path(filename)
        match = re.fullmatch(self.expression, path.name)
        if match is None:
            raise InvalidContract("filename does not match its declared pattern")
        groups = match.groupdict()
        shape_name = groups.get("shape") or self.shape
        if not shape_name:
            raise InvalidContract("filename supplies no shape and the feed has no fixed shape")
        if self.shape is not None and groups.get("shape") is not None and self.shape != groups["shape"]:
            raise InvalidContract("filename shape differs from the declared feed shape")
        shape = contract.shape(shape_name)
        produced_at = _instant(groups["produced_at"])
        window = None
        column = next((c for c in contract.schema if c.name == contract.window_column), None)
        if "low" in groups:
            if groups.get("low") is None or groups.get("high") is None:
                raise InvalidContract("filename needs both window bounds")
            parse = {"int": int, "str": str, "date": date.fromisoformat, "datetime": _instant}.get(
                column.type if column else "str"
            )
            if parse is None:
                raise InvalidContract("window column must be an ordered scalar")
            window = (parse(groups["low"]), parse(groups["high"]))
        elif shape.coverage == "window":
            if shape.window_size is None:
                raise InvalidContract("a window feed needs bounds or window_size")
            if column is None or column.type not in ("date", "datetime"):
                raise InvalidContract("derived windows need a date or datetime column")
            low = produced_at - shape.window_size
            window = (
                ((low + timedelta(days=1)).date(), produced_at.date())
                if column.type == "date"
                else (low, produced_at)
            )
        since = groups.get("since")
        if since is not None and shape.checkpoint == "timestamp":
            since = _instant(since)
        return Manifest(
            contract.table,
            shape_name,
            produced_at,
            window=window,
            since=since,
            generation=groups.get("generation"),
            rows=int(groups["rows"]) if groups.get("rows") is not None else _count_rows(path),
            schema_hash=groups.get("schema_hash") or contract.schema_hash,
        )


def _count_rows(path: Path) -> int:
    if path.suffix.lower() == ".csv":
        with path.open(newline="") as stream:
            return sum(1 for _ in csv.DictReader(stream))
    if path.suffix.lower() == ".parquet":
        import duckdb

        with duckdb.connect() as connection:
            return connection.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()[0]
    raise InvalidContract("row counting supports CSV and Parquet files")


class FileDrop:
    def __init__(self, store: Store, contract: Contract, pattern: FilePattern | None = None):
        self.store, self.contract, self.pattern = store, contract, pattern

    def ingest(self, path: str | Path) -> Receipt:
        path = Path(path)
        sidecar = path.with_suffix(path.suffix + ".json")
        if sidecar.exists():
            manifest = Manifest.from_json(sidecar.read_text())
        elif self.pattern is not None:
            manifest = self.pattern.manifest(path, self.contract)
        else:
            raise InvalidContract("delivery needs a sidecar manifest or a declared filename pattern")
        return self.store.deliver(self.contract, path, manifest)
