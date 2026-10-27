"""Read sidecar manifests or derive them from a declared filename pattern."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from dogana import Contract, InvalidContract, Manifest, Receipt, SerializationError, Store, deliver
from dogana.contract import Window
from dogana.reference._sources import (
    _column_hash,
    _columns,
    _count_rows,
    _file_digest,
    _FileSource,
    _published,
)
from dogana.reference._text import _checkpoint, _instant, _position, _scalar

_DECLARED = ", or a declared timezone"


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
        produced_at = _instant(groups["produced_at"], self.timezone, remedy=_DECLARED)
        window: Window | None = None
        column_type = contract.types.get(contract.window_column) if contract.window_column else None
        if "low" in groups:
            if groups.get("low") is None or groups.get("high") is None:
                raise InvalidContract("filename needs both window bounds")
            window = (
                _scalar(groups["low"], column_type or "str", self.timezone, remedy=_DECLARED),
                _scalar(groups["high"], column_type or "str", self.timezone, remedy=_DECLARED),
            )
        elif shape.coverage == "window":
            if shape.window_size is None:
                raise InvalidContract("a window feed needs bounds or window_size")
            if column_type not in ("date", "datetime"):
                raise InvalidContract("derived windows need a date or datetime column")
            window = shape.window_ending_at(produced_at, column_type)

        since = groups.get("since")
        generation = groups.get("generation")
        declared = groups.get("rows")
        live, tombstones = (
            (int(declared), int(groups.get("tombstones") or 0))
            if declared is not None
            else _count_rows(path, contract.deleted_column)
        )
        return Manifest(
            table=contract.table,
            shape=shape_name,
            produced_at=produced_at,
            window=window,
            since=_checkpoint(since, shape, self.timezone, remedy=_DECLARED) if since is not None else None,
            generation=_position(generation, contract) if generation is not None else None,
            rows=live,
            tombstones=tombstones,
            schema_hash=groups.get("schema_hash") or contract.schema_hash,
        )


class FileDrop:
    """Ingest paths within root, defaulting to each submitted file's directory."""

    def __init__(
        self,
        store: Store,
        contract: Contract,
        pattern: FilePattern | None = None,
        *,
        columns: Mapping[str, str],
        root: str | Path | None = None,
    ) -> None:
        self.store, self.contract, self.pattern = store, contract, pattern
        self.columns = _columns(columns)
        if any(
            self.columns.get(name) != kind
            for name, kind in contract.types.items()
            if name != contract.deleted_column or name in self.columns
        ):
            raise InvalidContract("file columns must match the contract's bound column types")
        if (
            contract.schema_hash is not None
            and _column_hash(_published(self.columns, contract)) != contract.schema_hash
        ):
            raise InvalidContract("file column hash differs from the contract schema_hash")
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
            # The receiver hashes what it derives, so a changed file under the same name is not a replay.
            derived = self.pattern.manifest(path, self.contract)
            manifest = replace(derived, content_hash=_file_digest(resolved))
        else:
            raise InvalidContract("delivery needs a sidecar manifest or a declared filename pattern")
        return deliver(
            self.store,
            self.contract,
            _FileSource(path, self.columns),
            manifest,
            now=now if now is not None else datetime.now(UTC),
        )
