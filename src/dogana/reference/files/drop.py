"""Read sidecar manifests or derive them from a declared filename pattern."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path

from dogana import Contract, InvalidContract, Manifest, Receipt, SerializationError, Store, deliver
from dogana.contract import Scalar, Window
from dogana.wire import utc


def _instant(value: str, timezone: tzinfo | None) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is not None:
        return utc(result)
    if timezone is None:
        raise InvalidContract("naive filename timestamps need a declared timezone")
    local = result.replace(tzinfo=timezone)
    instant = utc(local)
    if instant.astimezone(timezone).replace(tzinfo=None) != result:
        raise InvalidContract("nonexistent filename time needs an explicit UTC offset")
    if local.utcoffset() != local.replace(fold=1).utcoffset():
        raise InvalidContract("ambiguous filename time needs an explicit UTC offset")
    return instant


def _window_bound(value: str, column_type: str, timezone: tzinfo | None) -> Scalar:
    if column_type == "datetime":
        return _instant(value, timezone)
    if column_type == "date":
        return date.fromisoformat(value)
    if column_type == "int":
        return int(value)
    if column_type == "str":
        return value
    raise InvalidContract("window column must be an ordered scalar")


def _literal_path(path: Path) -> None:
    if any(character in str(path) for character in "*?[]{}"):
        raise InvalidContract("delivery paths must be literal paths without glob characters")


def _resolved_path(path: Path) -> Path:
    _literal_path(path)
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise InvalidContract("delivery path cannot be resolved") from exc
    _literal_path(resolved)
    return resolved


def _within_root(path: Path, root: Path) -> Path:
    resolved = _resolved_path(path)
    if not resolved.is_relative_to(root):
        raise InvalidContract("delivery path escapes the drop root")
    return resolved


@dataclass(frozen=True)
class FilePattern:
    """A filename declaration; timezone applies only to naive datetime groups."""

    expression: str
    shape: str | None = None
    timezone: tzinfo | None = None

    def __post_init__(self) -> None:
        regex = re.compile(self.expression)
        if "produced_at" not in regex.groupindex:
            raise InvalidContract("a filename pattern needs a produced_at group")
        if "shape" not in regex.groupindex and not self.shape:
            raise InvalidContract("a feed needs a fixed shape or a filename shape group")
        if self.shape is not None and (not isinstance(self.shape, str) or not self.shape):
            raise InvalidContract("a fixed shape must be a nonempty name")
        if ("low" in regex.groupindex) != ("high" in regex.groupindex):
            raise InvalidContract("a filename pattern needs both window bounds groups")
        if self.timezone is not None and not isinstance(self.timezone, tzinfo):
            raise InvalidContract("a filename timezone must be a tzinfo value")

    def manifest(self, filename: str | Path, contract: Contract) -> Manifest:
        path = Path(filename)
        _resolved_path(path)
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
        produced_at = _instant(groups["produced_at"], self.timezone)
        window: Window | None = None
        column = contract.columns.get(contract.window_column) if contract.window_column else None
        if "low" in groups:
            if groups.get("low") is None or groups.get("high") is None:
                raise InvalidContract("filename needs both window bounds")
            column_type = column.type if column else "str"
            window = (
                _window_bound(groups["low"], column_type, self.timezone),
                _window_bound(groups["high"], column_type, self.timezone),
            )
        elif shape.coverage == "window":
            if shape.window_size is None:
                raise InvalidContract("a window feed needs bounds or window_size")
            if column is None or column.type not in ("date", "datetime"):
                raise InvalidContract("derived windows need a date or datetime column")
            window = shape.window_ending_at(produced_at, column.type)

        since: str | datetime | None = groups.get("since")
        if isinstance(since, str) and shape.checkpoint == "timestamp":
            since = _instant(since, self.timezone)
        return Manifest(
            table=contract.table,
            shape=shape_name,
            produced_at=produced_at,
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
            row = connection.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()
            if row is None or not isinstance(row[0], int):
                raise InvalidContract("Parquet row count is unavailable")
            return row[0]
    raise InvalidContract("row counting supports CSV and Parquet files")


class FileDrop:
    """Ingest paths within root, defaulting to each submitted file's directory."""

    def __init__(
        self,
        store: Store,
        contract: Contract,
        pattern: FilePattern | None = None,
        *,
        root: str | Path | None = None,
    ) -> None:
        self.store, self.contract, self.pattern = store, contract, pattern
        self.root = _resolved_path(Path(root)) if root is not None else None

    def ingest(self, path: str | Path, *, now: datetime | None = None) -> Receipt:
        path = Path(path)
        root = self.root if self.root is not None else _resolved_path(path.parent)
        resolved = _within_root(path, root)
        sidecar = path.with_suffix(path.suffix + ".json")
        resolved_sidecar = _within_root(sidecar, root)
        if not resolved.is_file():
            raise InvalidContract("delivery needs a regular file")
        if resolved_sidecar.exists():
            if not resolved_sidecar.is_file():
                raise InvalidContract("delivery sidecar must be a regular file")
            try:
                document = sidecar.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise SerializationError("sidecar manifest must be UTF-8") from exc
            manifest = Manifest.from_json(document)
        elif self.pattern is not None:
            manifest = self.pattern.manifest(path, self.contract)
        else:
            raise InvalidContract("delivery needs a sidecar manifest or a declared filename pattern")
        return deliver(
            self.store, self.contract, path, manifest, now=now if now is not None else datetime.now(UTC)
        )
