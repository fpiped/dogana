import csv
from pathlib import Path

from dogana import Manifest
from tests.support import at


def csv_file(folder: Path, name: str, rows: list[dict], columns=("id", "day", "value")) -> Path:
    path = folder / name
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def sidecar(path, c, shape, day, rows, **kwargs):
    manifest = Manifest(c.table, shape, at(day), rows=rows, schema_hash=c.schema_hash, **kwargs)
    path.with_suffix(path.suffix + ".json").write_text(manifest.to_json())
    return manifest


def row(key, day=18, value="current"):
    return {"id": key, "day": at(day).date(), "value": value}
