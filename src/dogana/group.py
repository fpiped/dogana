"""Tables delivered and read together, as the source held them at one instant."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .contract import Contract, Manifest, Window, coverage_window, identifier
from .errors import InvalidContract
from .ledger import Claims, Ledger


@dataclass(frozen=True)
class Reference:
    """Every live row of `table` whose `columns` are all non-null names the key of a row of `parent`."""

    table: str
    columns: tuple[str, ...]
    parent: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))
        for name in (self.table, self.parent, *self.columns):
            identifier(name)
        if not self.columns or len(set(self.columns)) != len(self.columns):
            raise InvalidContract("a reference names distinct columns")

    def __str__(self) -> str:
        return f"{self.table}({', '.join(self.columns)}) references {self.parent}"


@dataclass(frozen=True)
class Group:
    """Tables a receiver reads together, and the references the source keeps between them."""

    contracts: tuple[Contract, ...]
    references: tuple[Reference, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "contracts", tuple(self.contracts))
        object.__setattr__(self, "references", tuple(self.references))
        # Many stores resolve table names regardless of letter case, so two spellings would share one table.
        if not self.contracts or len({table.casefold() for table in self.tables}) != len(self.contracts):
            raise InvalidContract("a group names each of its tables once, whatever the letter case")
        for reference in self.references:
            if reference.table not in self.tables or reference.parent not in self.tables:
                raise InvalidContract(f"{reference}: a table outside the group")
            if len(reference.columns) != len(self.contract(reference.parent).key):
                raise InvalidContract(f"{reference}: one column per key column of the parent")

    @property
    def tables(self) -> tuple[str, ...]:
        return tuple(contract.table for contract in self.contracts)

    def contract(self, table: str) -> Contract:
        try:
            return next(contract for contract in self.contracts if contract.table == table)
        except StopIteration:
            raise InvalidContract(f"table {table} is not in the group") from None

    def unchecked(self, reference: Reference, after: Mapping[str, Ledger], child: Manifest) -> str | None:
        """Why the ledgers after a delivery cannot test a reference, or None when an orphan disproves it.

        A child row in the child's delivered coverage was live at p only where that coverage ends exact
        at p, and the row it names is present only where the parent is complete as of p.
        """
        at = child.produced_at
        window = coverage_window(child.check(after[reference.table].contract))
        if any(
            (r.complete_as_of, r.deletions_as_of) != (at, at) for r in after[reference.table].covered(window)
        ):
            return f"{reference.table} is not exact as of {at.isoformat()} where it was delivered"
        if any(r.complete_as_of != at for r in after[reference.parent].regions):
            return f"{reference.parent} is not complete as of {at.isoformat()} over the whole table"
        return None

    def claims(
        self, ledgers: Mapping[str, Ledger], now: datetime, windows: Mapping[str, Window] | None = None
    ) -> Claims:
        """Every table's claims at now, clipped to its window; `exact_as_of` is the group's common instant."""
        windows = windows or {}
        if set(ledgers) != set(self.tables) or any(ledgers[t].contract.table != t for t in ledgers):
            raise InvalidContract("group claims need one ledger per table of the group")
        if not set(windows) <= set(self.tables):
            raise InvalidContract("group windows name tables outside the group")
        return Claims.combine(*(ledgers[t].claims(now, window=windows.get(t)) for t in self.tables))
