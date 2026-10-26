"""The dogana command line: write a delivery's manifest beside its file, and check a file against one."""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from collections.abc import Iterator, Sequence
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from dogana import Contract, Coverage, DoganaError, InvalidContract, Manifest
from dogana.contract import Scalar, Window
from dogana.reference._sources import _count_rows, _file_digest
from dogana.reference._text import _checkpoint, _instant, _position, _scalar

MANIFEST_EXAMPLES = """\
examples:
  dogana manifest orders.csv --contract orders.contract.json --shape full \\
      --produced-at 2026-10-26T06:00:00Z
  dogana manifest orders-30d.csv --contract orders.contract.json --shape daily \\
      --produced-at 2026-10-26T06:00:00Z --window 2026-09-27 2026-10-26
  dogana manifest changes.csv --contract orders.contract.json --shape changes \\
      --produced-at 2026-10-26T06:00:00Z --since 2026-10-25T06:00:00Z

The manifest is written to FILE.json, the sidecar a receiver's file drop reads, unless -o names
another path or - for standard output. Rows and the SHA-256 of FILE are computed, never typed."""

CHECK_EXAMPLES = """\
example:
  dogana check orders.csv --contract orders.contract.json

Reads FILE.json unless --manifest names it, then checks the manifest against the contract and
FILE against the manifest: the row count and the SHA-256 of the bytes."""


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; exit 0 on success, 1 when dogana refuses, 2 on a usage error."""
    args = _parser().parse_args(argv)
    try:
        return int(args.run(args))
    except _UsageError as exc:
        print(f"dogana {args.command}: error: {exc}", file=sys.stderr)
        return 2
    except (DoganaError, OSError) as exc:
        print(f"dogana {args.command}: error: {_reason(exc)}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dogana", description="Declare and check what a data delivery proves about its table."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    commands = parser.add_subparsers(title="commands", required=True, metavar="COMMAND")

    write = commands.add_parser(
        "manifest",
        help="write the manifest that describes a delivered file",
        description="Write the manifest that describes FILE, validated against the receiver's contract.",
        epilog=MANIFEST_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    write.add_argument("file", type=Path, metavar="FILE", help="the delivered CSV or Parquet file")
    _contract_argument(write)
    write.add_argument("--shape", required=True, help="a shape the contract admits")
    write.add_argument(
        "--produced-at",
        required=True,
        metavar="TIME",
        help="the source read boundary, with a UTC offset: every change through it is in FILE",
    )
    write.add_argument("--window", nargs=2, metavar=("LOW", "HIGH"), help="closed window of a window shape")
    write.add_argument("--since", metavar="VALUE", help="checkpoint of a changed-only shape")
    write.add_argument("--generation", metavar="VALUE", help="table position this read issues")
    write.add_argument("--tombstones", type=Path, metavar="KEYS", help="CSV or Parquet of deleted keys")
    write.add_argument("--rows", type=int, metavar="N", help="row count, for files dogana cannot count")
    write.add_argument("-o", "--output", metavar="PATH", help="where to write, or - for standard output")
    write.add_argument("--force", action="store_true", help="replace an existing manifest")
    write.set_defaults(run=_write_manifest, command="manifest")

    check = commands.add_parser(
        "check",
        help="check a delivered file against its manifest and contract",
        description="Check FILE against its manifest, and the manifest against the receiver's contract.",
        epilog=CHECK_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    check.add_argument("file", type=Path, metavar="FILE", help="the delivered CSV or Parquet file")
    _contract_argument(check)
    check.add_argument("--manifest", type=Path, metavar="PATH", help="the manifest, if not FILE.json")
    check.set_defaults(run=_check, command="check")
    return parser


def _contract_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--contract", required=True, type=Path, metavar="PATH", help="the receiver's contract, as JSON"
    )


def _write_manifest(args: argparse.Namespace) -> int:
    contract = Contract.from_json(args.contract.read_text(encoding="utf-8"))
    shape = contract.shape(args.shape)
    produced_at = _instant(args.produced_at)
    manifest = contract.manifest(
        args.shape,
        produced_at,
        window=_window(contract, args.shape, produced_at, args.window),
        since=_checkpoint(args.since, shape) if args.since is not None else None,
        generation=_position(args.generation, contract) if args.generation is not None else None,
        rows=args.rows if args.rows is not None else _counted(args.file),
        keys=tuple(_keys(args.file, contract)) if shape.coverage is Coverage.KEYS else (),
        tombstones=tuple(_keys(args.tombstones, contract)) if args.tombstones is not None else (),
        content_hash=_file_digest(args.file),
    )
    document = manifest.to_json() + "\n"
    if args.output == "-":
        sys.stdout.write(document)
        return 0
    target = Path(args.output) if args.output is not None else _sidecar(args.file)
    _replace(target, document, force=args.force)
    print(target)
    return 0


def _check(args: argparse.Namespace) -> int:
    contract = Contract.from_json(args.contract.read_text(encoding="utf-8"))
    path = args.manifest if args.manifest is not None else _sidecar(args.file)
    manifest = Manifest.from_json(path.read_text(encoding="utf-8"))
    manifest.check(contract)
    problems = []
    if manifest.rows is not None:
        try:
            counted = _counted(args.file)
        except _UsageError:
            print(
                f"dogana check: note: {args.file}: rows not compared, dogana counts CSV and Parquet",
                file=sys.stderr,
            )
            counted = manifest.rows
        if counted != manifest.rows:
            problems.append(f"rows: the manifest declares {manifest.rows}, {args.file} has {counted}")
    if manifest.content_hash is not None and (digest := _file_digest(args.file)) != manifest.content_hash:
        problems.append(
            f"content_hash: the manifest declares {manifest.content_hash}, {args.file} hashes to {digest}"
        )
    for problem in problems:
        print(f"dogana check: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"{args.file}: a valid {manifest.shape!r} delivery for {contract.table!r}")
    return 0


def _window(
    contract: Contract, shape_name: str, produced_at: datetime, bounds: list[str] | None
) -> Window | None:
    shape = contract.shape(shape_name)
    if shape.coverage is not Coverage.WINDOW:
        if bounds is not None:
            raise _UsageError(f"shape {shape_name!r} does not deliver a window")
        return None
    assert contract.window_column is not None
    kind = contract.types[contract.window_column]
    if bounds is not None:
        low, high = (_scalar(bound, kind) for bound in bounds)
        return low, high
    if shape.window_size is None or kind not in ("date", "datetime"):
        raise _UsageError(f"shape {shape_name!r} needs --window LOW HIGH")
    return shape.window_ending_at(produced_at, kind)


def _counted(path: Path) -> int:
    try:
        return _count_rows(path)
    except ModuleNotFoundError as exc:
        raise _UsageError("counting Parquet rows needs the duckdb extra; install it or pass --rows") from exc
    except InvalidContract as exc:
        raise _UsageError(f"{exc}; pass --rows") from exc


def _keys(path: Path, contract: Contract) -> Iterator[tuple[Scalar, ...]]:
    for row in _records(path, contract.key):
        yield tuple(_scalar(row[name], contract.types[name]) for name in contract.key)


def _records(path: Path, columns: tuple[str, ...]) -> Iterator[dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            missing = [name for name in columns if name not in (reader.fieldnames or ())]
            if missing:
                raise InvalidContract(f"{path} has no key column {', '.join(missing)}")
            yield from reader
    elif suffix == ".parquet":
        try:
            import duckdb
        except ModuleNotFoundError as exc:
            raise _UsageError("reading Parquet keys needs the duckdb extra") from exc
        selected = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
        with duckdb.connect() as connection:
            for values in connection.execute(
                f"SELECT {selected} FROM read_parquet(?)", [str(path)]
            ).fetchall():
                yield {name: str(value) for name, value in zip(columns, values, strict=True)}
    else:
        raise _UsageError(f"{path}: keys can be read from CSV or Parquet files")


def _sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".json")


def _replace(target: Path, document: str, *, force: bool) -> None:
    """Write the whole manifest or nothing: a receiver polling the folder never reads half a file."""
    if target.exists() and not force:
        raise FileExistsError(f"{target} exists; pass --force to replace it")
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _reason(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError) and exc.filename is not None:
        return f"{exc.filename}: no such file"
    return str(exc)


def _version() -> str:
    try:
        return version("dogana")
    except PackageNotFoundError:
        return "unknown"


class _UsageError(Exception):
    """The command was called in a way that cannot describe the delivery; exit status 2, like argparse."""
