"""Check protocol references in collected tests, including inherited conformance tests."""

from __future__ import annotations

import argparse
import io
import re
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Only premises and statements outside the receiver's observable behavior belong here.
# Mixed rules still need tests for their observable clauses.
UNTESTABLE: dict[str, str] = {
    "notation.rows": "Definitions of source rows and tombstones, not receiver behavior.",
    "notation.dates": "Notation for source time and evidence dates.",
    "notation.intervals": "Mathematical interval notation.",
    "notation.coverage": "Definition of coverage.",
    "contract.soft-deletes": "Introduces the source's soft-delete premises.",
    "contract.soft-deletes.1": "Only the source can guarantee that flag changes move its cursor.",
    "contract.soft-deletes.2": "Only the source can guarantee no physical deletion.",
    "contract.soft-deletes.3": "The receiver cannot establish where a source's flag originates.",
    "tables-together.theorem": "A conditional mathematical guarantee, not a finite test scenario.",
    "tables-together.premise": "The receiver cannot verify a source's transaction isolation or snapshot.",
    "tables-together.atomicity": "Explains the availability rationale of atomic group publication.",
    "boundaries.undecided": "Explicitly outside the decided protocol.",
}

LABEL = re.compile(r"<!-- protocol: ([a-z0-9_-]+) -->")
SCOPE = re.compile(r"<!-- protocol-scope: ([a-z0-9_-]+) -->")
SECTION = re.compile(r"<!-- protocol-section: ([a-z0-9-]+) -->")
HEADING = re.compile(r"## (.+)$")
BOLD = re.compile(r"\*\*(.+?)\.\*\*\s*(.*)", re.DOTALL)
NUMBER = re.compile(r"^(\d+)\. ", re.MULTILINE)


@dataclass(frozen=True)
class Rule:
    identifier: str
    line: int


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "-", value.lower()).strip("-")


def protocol_rules(source: str) -> dict[str, Rule]:
    """Parse the documented Markdown convention, refusing unlabelled prose and duplicate IDs."""
    rules: dict[str, Rule] = {}
    section, part = "protocol", ""
    pending: str | None = None

    def add(suffix: str, line: int) -> None:
        identifier = f"{section}.{suffix}"
        if identifier in rules:
            raise ValueError(f"protocol.md:{line}: duplicate rule {identifier}")
        rules[identifier] = Rule(identifier, line)

    for match in re.finditer(r"\S[^\n]*(?:\n(?!\n)[^\n]*)*", source):
        block = match.group()
        line = source.count("\n", 0, match.start()) + 1
        if heading := HEADING.fullmatch(block):
            if pending is not None:
                raise ValueError(f"protocol.md:{line}: unused label {pending}")
            section, part = slug(heading[1]), ""
        elif block.startswith("# "):
            continue
        elif alias := SECTION.fullmatch(block):
            section = alias[1]
        elif scope := SCOPE.fullmatch(block):
            part = scope[1]
        elif label := LABEL.fullmatch(block):
            if pending is not None:
                raise ValueError(f"protocol.md:{line}: unused label {pending}")
            pending = label[1]
        elif part and block.endswith(":") and "\n" not in block and not block.startswith("**"):
            # A one-line caption introduces the following list, not another obligation.
            continue
        elif block.startswith("|"):
            rows = block.splitlines()
            if len(rows) < 3 or not re.fullmatch(r"\|[ :|\-]+", rows[1]):
                raise ValueError(f"protocol.md:{line}: expected a table header and separator")
            for offset, row in enumerate(rows[2:], 2):
                cell = row.split("|")[1].strip()
                label = LABEL.search(cell)
                name = label[1] if label else slug(cell)
                add(f"{part}.{name}" if part else name, line + offset)
        elif NUMBER.match(block):
            for item in NUMBER.finditer(block):
                number = item[1]
                add(f"{part}.{number}" if part else number, line + block.count("\n", 0, item.start()))
        elif block.startswith("- "):
            for item in re.finditer(r"^- .*?(?=\n- |\Z)", block, re.MULTILINE | re.DOTALL):
                label = LABEL.search(item.group())
                if label is None:
                    raise ValueError(f"protocol.md:{line}: bullet needs a protocol label")
                add(label[1], line + block.count("\n", 0, item.start()))
        elif bold := BOLD.fullmatch(block):
            part = pending or slug(bold[1])
            pending = None
            if bold[2]:
                add(part, line)
        elif pending is not None:
            add(pending, line)
            pending = None
        else:
            raise ValueError(f"protocol.md:{line}: prose needs a protocol label")
    if pending is not None:
        raise ValueError(f"protocol.md: unused label {pending}")
    if not rules:
        raise ValueError("protocol.md: no rules found")
    return rules


class Claims:
    def __init__(self) -> None:
        self.rules: dict[str, set[str]] = {}
        self.errors: list[str] = []

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        for item in session.items:
            for mark in item.iter_markers(name="protocol"):
                if not mark.args or mark.kwargs or any(not isinstance(arg, str) for arg in mark.args):
                    self.errors.append(f"{item.nodeid}: protocol needs one or more string IDs, no keywords")
                    continue
                for identifier in mark.args:
                    self.rules.setdefault(identifier, set()).add(item.nodeid)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list every rule, status and protocol line")
    parser.add_argument("--strict", action="store_true", help="also fail on unclaimed testable rules")
    args = parser.parse_args()
    try:
        rules = protocol_rules((ROOT / "docs/protocol.md").read_text(encoding="utf-8"))
    except ValueError as parse_error:
        print(parse_error)
        return 1

    claims, output = Claims(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        result = pytest.main(
            ["--collect-only", "-q", "-o", "addopts=", "-p", "no:cacheprovider", str(ROOT / "tests")],
            plugins=[claims],
        )
    if result != pytest.ExitCode.OK:
        print(output.getvalue(), end="")
        print("Protocol claim collection failed.")
        return 1

    errors = claims.errors
    for identifier in sorted(claims.rules.keys() - rules.keys()):
        for nodeid in sorted(claims.rules[identifier]):
            errors.append(f"{nodeid}: unknown protocol rule {identifier}")
    for identifier in sorted(UNTESTABLE.keys() - rules.keys()):
        errors.append(f"untestable exemption names unknown protocol rule {identifier}")
    for identifier in sorted(claims.rules.keys() & UNTESTABLE.keys()):
        errors.append(f"{identifier}: claimed and exempt; remove the exemption or correct the claim")

    claimed = rules.keys() & claims.rules.keys()
    exempt = rules.keys() & UNTESTABLE.keys()
    unclaimed = rules.keys() - claimed - exempt
    print(
        f"Protocol: {len(rules)} rules, {len(claimed)} claimed, "
        f"{len(unclaimed)} unclaimed, {len(exempt)} untestable."
    )
    if args.list:
        for identifier, rule in rules.items():
            status = (
                "claimed" if identifier in claimed else "untestable" if identifier in exempt else "unclaimed"
            )
            print(f"{identifier}: {status} (docs/protocol.md:{rule.line})")
    print("Unclaimed rules:")
    for identifier in sorted(unclaimed):
        print(f"  {identifier} (docs/protocol.md:{rules[identifier].line})")
    print("Untestable rules:")
    for identifier in sorted(exempt):
        print(f"  {identifier}: {UNTESTABLE[identifier]}")
    for error in errors:
        print(f"ERROR: {error}")
    return int(bool(errors) or args.strict and bool(unclaimed))


if __name__ == "__main__":
    raise SystemExit(main())
