"""The dogana command line: write a delivery's manifest beside its file, check a file against one, and
print the dates a ledger claims. It hashes files but never parses them."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from hashlib import file_digest
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from dogana import Contract, Coverage, Deletions, DoganaError, Ledger, Manifest, Region
from dogana._text import _checkpoint, _instant, _position, _scalar
from dogana.contract import Scalar, Window

MANIFEST_EXAMPLES = """\
examples:
  dogana manifest orders.csv --contract orders.contract.json --shape full \\
      --produced-at 2026-10-26T06:00:00Z --rows 1200
  dogana manifest orders-30d.csv --contract orders.contract.json --shape daily \\
      --produced-at 2026-10-26T06:00:00Z --window 2026-09-27 2026-10-26 --rows 310
  dogana manifest changes.csv --contract orders.contract.json --shape changes \\
      --produced-at 2026-10-26T06:00:00Z --since 2026-10-25T06:00:00Z --rows 40 --tombstones 3

The manifest is written to FILE.json, the sidecar a receiver's file drop reads, unless -o names
another path or - for standard output. The counts come from the export that wrote FILE, which knows
them; the SHA-256 of FILE is computed."""

CHECK_EXAMPLES = """\
example:
  dogana check orders.csv --contract orders.contract.json

Reads FILE.json unless --manifest names it, then checks the manifest against the contract and the
SHA-256 of FILE against the manifest. Counts, keys, windows and column types are checked by the
receiver's store, which reads the rows."""

CLAIMS_EXAMPLES = """\
example:
  dogana claims orders.ledger.json --at 2026-10-27T08:00:00Z

LEDGER is a ledger as JSON, as a store returns it from load_ledger(contract).to_json(). Prints one
line per region: its bounds, its state at TIME (now by default) and its two dates, then the table's
state and the instant it is exact at, if any."""


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
    write.add_argument("file", type=Path, metavar="FILE", help="the delivered file")
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
    write.add_argument("--rows", required=True, type=int, metavar="N", help="live rows in FILE")
    write.add_argument(
        "--tombstones", type=int, metavar="N", help="rows marked deleted in FILE, for a tombstone shape"
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
    check.add_argument("file", type=Path, metavar="FILE", help="the delivered file")
    _contract_argument(check)
    check.add_argument("--manifest", type=Path, metavar="PATH", help="the manifest, if not FILE.json")
    check.set_defaults(run=_check, command="check")

    claims = commands.add_parser(
        "claims",
        help="print the dates a ledger claims for its table",
        description="Print what LEDGER claims about its table at one instant.",
        epilog=CLAIMS_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    claims.add_argument("ledger", type=Path, metavar="LEDGER", help="a ledger as JSON")
    claims.add_argument("--at", metavar="TIME", help="the instant to judge freshness at, with a UTC offset")
    claims.add_argument(
        "--window", nargs=2, metavar=("LOW", "HIGH"), help="only this window, regions cut to it"
    )
    claims.set_defaults(run=_claims, command="claims")
    return parser


def _contract_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--contract", required=True, type=Path, metavar="PATH", help="the receiver's contract, as JSON"
    )


def _write_manifest(args: argparse.Namespace) -> int:
    contract = Contract.from_json(args.contract.read_text(encoding="utf-8"))
    shape = contract.shape(args.shape)
    produced_at = _instant(args.produced_at)
    if args.tombstones is None and shape.deletions is Deletions.TOMBSTONE:
        raise _UsageError(f"shape {args.shape!r} delivers tombstones; pass --tombstones")
    manifest = contract.manifest(
        args.shape,
        produced_at,
        window=_window(contract, args.shape, produced_at, args.window),
        since=_checkpoint(args.since, shape) if args.since is not None else None,
        generation=_position(args.generation, contract) if args.generation is not None else None,
        rows=args.rows,
        tombstones=args.tombstones or 0,
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
    if contract.schema_hash is not None and manifest.schema_hash != contract.schema_hash:
        print(
            f"dogana check: schema_hash: the manifest declares {manifest.schema_hash}, "
            f"the contract {contract.schema_hash}",
            file=sys.stderr,
        )
        return 1
    digest = _file_digest(args.file)
    if manifest.content_hash is None:
        print(
            f"dogana check: {path} declares no content_hash, so {args.file} was not compared", file=sys.stderr
        )
        return 1
    if digest != manifest.content_hash:
        print(
            f"dogana check: content_hash: the manifest declares {manifest.content_hash}, "
            f"{args.file} hashes to {digest}",
            file=sys.stderr,
        )
        return 1
    print(
        f"{args.file}: the manifest fits the contract and names these bytes; the rows are checked on delivery"
    )
    return 0


def _claims(args: argparse.Namespace) -> int:
    ledger = Ledger.from_json(args.ledger.read_text(encoding="utf-8"))
    at = _instant(args.at) if args.at is not None else datetime.now(UTC)
    window = None
    if args.window is not None:
        if ledger.contract.window_column is None:
            raise _UsageError(f"table {ledger.contract.table} has no window column")
        kind = ledger.contract.types[ledger.contract.window_column]
        low, high = (_scalar(bound, kind) for bound in args.window)
        window = (low, high)
    claims = ledger.claims(at, window=window)
    for claim in claims.regions:
        region = claim.region
        print(
            f"{claim.table} {_span(region)}: {claim.state.value}; complete as of "
            f"{_date(region.complete_as_of)}; deletions as of {_date(region.deletions_as_of)}"
        )
    exact = claims.exact_as_of
    print(
        f"{ledger.contract.table}: {claims.state.value}; "
        + (f"exact as of {_date(exact)}" if exact is not None else "exact at no single instant")
    )
    return 0


def _span(region: Region) -> str:
    """Closed bounds where the domain is discrete, so a window asked as [Oct 1, Oct 2] prints as such."""
    if region.low is None and region.high is None:
        return "all rows"
    low, low_open = _closed(region.low, region.low_open, 1)
    high, high_open = _closed(region.high, region.high_open, -1)
    left = "(-inf" if low is None else f"{'(' if low_open else '['}{low}"
    right = "+inf)" if high is None else f"{high}{')' if high_open else ']'}"
    return f"{left}, {right}"


def _closed(bound: Scalar | None, open_: bool, step: int) -> tuple[Scalar | None, bool]:
    if not open_ or isinstance(bound, datetime):
        return bound, open_
    if isinstance(bound, date):
        return bound + timedelta(days=step), False
    if isinstance(bound, int):
        return bound + step, False
    return bound, open_


def _date(value: datetime | None) -> str:
    return "unknown" if value is None else value.isoformat()


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


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".json")


def _replace(target: Path, document: str, *, force: bool) -> None:
    """Write the whole manifest or nothing: a receiver polling the folder never reads half a file."""
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        umask = os.umask(0)
        os.umask(umask)
        # mkstemp creates the file for its owner only; a receiver under another account must read it.
        os.chmod(temporary, 0o666 & ~umask)
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
