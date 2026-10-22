from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from dogana import Contract, Manifest, Receipt

EXAMPLES = Path(__file__).parents[2] / "docs" / "examples"


def test_the_documented_wire_receipt_describes_a_real_publication(tmp_path):
    pytest.importorskip("duckdb")
    from dogana.reference.duckdb import DuckDBStore

    c = Contract.from_json((EXAMPLES / "contract.json").read_text())
    m = Manifest.from_json((EXAMPLES / "manifest.json").read_text())
    receipt = Receipt.from_json((EXAMPLES / "receipt.json").read_text())
    path = tmp_path / "stock.csv"
    path.write_text("id,value\n1,first\n2,second\n")
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        actual = store.deliver(c, path, m, now=receipt.received_at)
        assert [(check.name, check.passed) for check in actual.checks] == [
            (check.name, check.passed) for check in receipt.checks
        ]
        assert actual == replace(receipt, source=str(path), checks=actual.checks)
