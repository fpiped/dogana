"""Read sidecar manifests or derive them from a declared filename pattern."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from dogana import Contract, InvalidContract, Manifest, Receipt, Store


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    return result if result.tzinfo is not None else result.replace(tzinfo=UTC)


@dataclass(frozen=True)
class FilePattern:
    expression: str

    def __post_init__(self):
        regex = re.compile(self.expression)
        if not {"shape", "produced_at", "rows"} <= regex.groupindex.keys():
            raise InvalidContract("a filename pattern declares shape, produced_at and rows groups")

    def manifest(self, filename: str, contract: Contract) -> Manifest:
        match = re.fullmatch(self.expression, filename)
        if match is None:
            raise InvalidContract("filename does not match its declared pattern")
        groups = match.groupdict()
        window = None
        if groups.get("low") is not None or groups.get("high") is not None:
            if groups.get("low") is None or groups.get("high") is None:
                raise InvalidContract("filename needs both window bounds")
            column = next((c for c in contract.schema if c.name == contract.window_column), None)
            parse = {"int": int, "str": str, "date": date.fromisoformat, "datetime": _instant}.get(
                column.type if column else "str"
            )
            if parse is None:
                raise InvalidContract("window column must be an ordered scalar")
            window = (parse(groups["low"]), parse(groups["high"]))
        shape = contract.shape(groups["shape"])
        since = groups.get("since")
        if since is not None and shape.checkpoint == "timestamp":
            since = _instant(since)
        return Manifest(
            contract.table,
            groups["shape"],
            _instant(groups["produced_at"]),
            window=window,
            since=since,
            generation=groups.get("generation"),
            rows=int(groups["rows"]),
            schema_hash=groups.get("schema_hash") or contract.schema_hash,
        )


class FileDrop:
    def __init__(self, store: Store, contract: Contract, pattern: FilePattern | None = None):
        self.store, self.contract, self.pattern = store, contract, pattern

    def ingest(self, path: str | Path) -> Receipt:
        path = Path(path)
        sidecar = path.with_suffix(path.suffix + ".json")
        if sidecar.exists():
            manifest = Manifest.from_json(sidecar.read_text())
        elif self.pattern is not None:
            manifest = self.pattern.manifest(path.name, self.contract)
        else:
            raise InvalidContract("delivery needs a sidecar manifest or a declared filename pattern")
        return self.store.deliver(self.contract, path, manifest)
