"""The I/O boundaries a store and an extractor implement outside the core."""

from collections.abc import Mapping
from typing import Protocol

from .contract import Contract, Manifest
from .gate import Check, CheckRequirement, Receipt
from .group import Group
from .ledger import Ledger
from .planner import ExtractionRequest


class Store(Protocol):
    def digest(self, source: object) -> str | None: ...
    def hold(self, source: object, manifest: Manifest, contract: Contract) -> str: ...
    def check(
        self,
        candidate: str,
        contract: Contract,
        manifest: Manifest,
        requirements: tuple[CheckRequirement, ...],
    ) -> tuple[Check, ...]: ...
    def publish(self, candidate: str | None, receipt: Receipt) -> None: ...
    def abort(self, candidate: str) -> None: ...
    def load_ledger(self, contract: Contract) -> Ledger: ...
    def accepted_receipt(self, identity: str) -> Receipt | None: ...
    def receipts(self, table: str | None = None) -> tuple[Receipt, ...]: ...


class TogetherStore(Store, Protocol):
    """A store that publishes the tables of a group in one atomic step."""

    def check_references(self, group: Group, candidates: Mapping[str, str]) -> Check: ...
    def publish_together(self, group: Group, entries: tuple[tuple[str, Receipt], ...]) -> None: ...


class Extractor(Protocol):
    def extract(self, contract: Contract, request: ExtractionRequest) -> tuple[Manifest, object]: ...
