"""I/O boundaries implemented by reference implementations."""

from typing import Protocol

from .contract import Contract, Manifest
from .gate import Check, CheckRequirement, Receipt
from .ledger import Ledger
from .planner import ExtractionRequest


class Store(Protocol):
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


class Extractor(Protocol):
    def extract(self, contract: Contract, request: ExtractionRequest) -> tuple[Manifest, object]: ...
