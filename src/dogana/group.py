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
    """Tables a receiver reads together: their files come from one snapshot, and are decided as one."""

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
        manifests: Mapping[str, Manifest],
        *,
        now: datetime,
        outcomes: Mapping[str, tuple[Check, ...]] | None = None,
        sources: Mapping[str, str] | None = None,
    ) -> dict[str, Receipt]:
        """Decide one delivery per table as one: every table is accepted, or every table is rejected.

        The joint check needs one `produced_at` on every manifest and every table accepted on its own;
        `outcomes` carries the checks the receiver ran on each table's rows. The receiver then saves
        every table's rows and ledger in one commit, or nothing.
        """
        self._own(ledgers, "ledger")
        self._own(manifests, "manifest")
        outcomes = outcomes or {}
        sources = sources or {}
        alone = tuple(
            Gate.evaluate(ledgers[t], manifests[t], outcomes.get(t, ()), now=now, source=sources.get(t))
            for t in self.tables
        )
        joint = (Gate.together(alone),)
        return {
            t: Gate.evaluate(
                ledgers[t], manifests[t], outcomes.get(t, ()), now=now, source=sources.get(t), joint=joint
            )
            for t in self.tables
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
        self._own(ledgers, "ledger")
        if not set(windows) <= set(self.tables):
            raise InvalidContract("group windows name tables outside the group")
        return Claims(
            tuple(
                region
                for t in self.tables
                for region in ledgers[t].claims(now, limit=limit, window=windows.get(t)).regions
            )
        )

    def _own(self, values: Mapping[str, Ledger] | Mapping[str, Manifest], what: str) -> None:
        tables = {t: v.contract.table if isinstance(v, Ledger) else v.table for t, v in values.items()}
        if set(tables) != set(self.tables) or any(tables[t] != t for t in tables):
            raise InvalidContract(f"a group needs one {what} per table of the group")
