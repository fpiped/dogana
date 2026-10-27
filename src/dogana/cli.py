"""The dogana command line: write a delivery's manifest beside its file, and check a file against one."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from dogana import Contract, Coverage, Deletions, DoganaError, InvalidContract, Manifest
from dogana.contract import Window
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
another path or - for standard output. Rows, tombstones (rows whose deleted column is true) and the
SHA-256 of FILE are computed, never typed."""

CHECK_EXAMPLES = """\
example:
  dogana check orders.csv --contract orders.contract.json

Reads FILE.json unless --manifest names it, then checks the manifest against the contract and
FILE against the manifest: the counts of rows and tombstones, and the SHA-256 of the bytes. Keys,
windows and column types are checked by the receiver's store, not here."""


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
    write.add_argument("--rows", type=int, metavar="N", help="live rows, for files dogana cannot count")
    write.add_argument(
        "--tombstones", type=int, metavar="N", help="deleted rows, for files dogana cannot count"
    )
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
    marked = shape.deletions is Deletions.TOMBSTONE
    counting = args.rows is None or (args.tombstones is None and marked)
    live, deleted = _counted(args.file, contract, marked) if counting else (0, 0)
    manifest = contract.manifest(
        args.shape,
        produced_at,
        window=_window(contract, args.shape, produced_at, args.window),
        since=_checkpoint(args.since, shape) if args.since is not None else None,
        generation=_position(args.generation, contract) if args.generation is not None else None,
        rows=args.rows if args.rows is not None else live,
        tombstones=args.tombstones if args.tombstones is not None else deleted,
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
    digest = _file_digest(args.file)
    compared, problems = [], []
    try:
        live, deleted = _counted(args.file, contract)
        compared += ["rows", "tombstones"]
        if manifest.rows is not None and live != manifest.rows:
            problems.append(f"rows: the manifest declares {manifest.rows}, {args.file} has {live}")
        if deleted != manifest.tombstones:
            problems.append(
                f"tombstones: the manifest declares {manifest.tombstones}, {args.file} has {deleted}"
            )
    except _UsageError:
        pass
    if manifest.content_hash is not None:
        compared.append("SHA-256")
        if digest != manifest.content_hash:
            problems.append(
                f"content_hash: the manifest declares {manifest.content_hash}, {args.file} hashes to {digest}"
            )
    if not compared:
        problems.append("nothing compared: dogana counts CSV and Parquet, and the manifest declares no hash")
    for problem in problems:
        print(f"dogana check: {problem}", file=sys.stderr)
    if problems:
        return 1
    matched = ", ".join(compared[:-1]) + " and " + compared[-1] if len(compared) > 1 else compared[0]
    print(f"{args.file}: {matched} match the manifest; keys, windows and types are checked on delivery")
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


def _counted(path: Path, contract: Contract, marked: bool = False) -> tuple[int, int]:
    declare = "--rows" + (" and --tombstones" if marked else "")
    try:
        return _count_rows(path, contract.deleted_column)
    except ModuleNotFoundError as exc:
        raise _UsageError(
            f"counting Parquet rows needs the duckdb extra; install it or pass {declare}"
        ) from exc
    except InvalidContract as exc:
        if path.suffix.lower() in (".csv", ".parquet"):
            raise
        raise _UsageError(f"{exc}; pass {declare}") from exc


def _sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".json")


def _replace(target: Path, document: str, *, force: bool) -> None:
    """Write the whole manifest or nothing: a receiver polling the folder never reads half a file."""
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        umask = os.umask(0)
        os.umask(umask)
        # mkstemp creates the file for its owner only; a receiver under another account must read it.
        os.fchmod(handle, 0o666 & ~umask)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise FileExistsError(f"{target} exists; pass --force to replace it") from None
    finally:
        Path(temporary).unlink(missing_ok=True)


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
