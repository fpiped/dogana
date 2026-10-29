"""Tables delivered and read together, as the source held them at one instant."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from .contract import Contract, Manifest, Window
from .errors import InvalidContract
from .gate import Check, Gate, Receipt
from .ledger import Claims, Ledger


@dataclass(frozen=True)
class Group:
    """Tables the receiver decides as one: a read of them is accepted whole or refused whole."""

    contracts: tuple[Contract, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "contracts", tuple(self.contracts))
        # Many databases resolve table names regardless of case, so two spellings would share one table.
        if not self.contracts or len({table.casefold() for table in self.tables}) != len(self.contracts):
            raise InvalidContract("a group names each of its tables once, whatever the letter case")

    @property
    def tables(self) -> tuple[str, ...]:
        return tuple(contract.table for contract in self.contracts)

    def contract(self, table: str) -> Contract:
        try:
            return next(contract for contract in self.contracts if contract.table == table)
        except StopIteration:
            raise InvalidContract(f"table {table} is not in the group") from None

    def apply(
        self,
        ledgers: Mapping[str, Ledger],
        manifest: Manifest,
        *,
        now: datetime,
        outcomes: Mapping[str, tuple[Check, ...]] | None = None,
        sources: Mapping[str, str] | None = None,
    ) -> dict[str, Receipt]:
        """Decide one read for the group's tables as one: every table is accepted, or every table is rejected.

        The read declares a part for each table of the group, or the tables it does declare are refused;
        parts for tables outside the group are not decided here. `outcomes` carries the checks the
        receiver ran on each table's rows. The receiver then saves every table's rows and ledger in one
        commit, or nothing.
        """
        self._own(ledgers)
        declared = tuple(t for t in self.tables if t in manifest.parts)
        if not declared:
            raise InvalidContract("the read declares none of the group's tables")
        missing = tuple(t for t in self.tables if t not in manifest.parts)
        outcomes = outcomes or {}
        sources = sources or {}
        for what, given in (("outcomes", outcomes), ("sources", sources)):
            if not set(given) <= set(declared):
                raise InvalidContract(f"{what} name tables the read does not declare for the group")
        alone = tuple(
            Gate.evaluate(ledgers[t], manifest, outcomes.get(t, ()), now=now, source=sources.get(t))
            for t in declared
        )
        joint = Gate.together(alone)
        if missing:
            joint = Check("together", False, f"the read declares no {', '.join(missing)}")
        return {
            t: Gate.evaluate(
                ledgers[t], manifest, outcomes.get(t, ()), now=now, source=sources.get(t), joint=(joint,)
            )
            for t in declared
        }

    def claims(
        self,
        ledgers: Mapping[str, Ledger],
        now: datetime,
        *,
        limit: timedelta,
        windows: Mapping[str, Window] | None = None,
    ) -> Claims:
        """Every table's claims at now, clipped to its window; `exact_as_of` is the group's common instant."""
        windows = windows or {}
        self._own(ledgers)
        if not set(windows) <= set(self.tables):
            raise InvalidContract("group windows name tables outside the group")
        return Claims(
            tuple(
                region
                for t in self.tables
                for region in ledgers[t].claims(now, limit=limit, window=windows.get(t)).regions
            )
        )

    def _own(self, ledgers: Mapping[str, Ledger]) -> None:
        tables = {t: ledger.contract.table for t, ledger in ledgers.items()}
        if set(tables) != set(self.tables) or any(tables[t] != t for t in tables):
            raise InvalidContract("a group needs one ledger per table of the group")
