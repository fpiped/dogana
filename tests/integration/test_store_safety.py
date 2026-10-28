from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

pytest.importorskip("duckdb")

from example_store import DuckDBStore, Rows
from example_store._sources import _FileSource

from dogana import Contract, Gate, Ledger, Manifest, deliver
from dogana.errors import InconsistentPublication, StoreError
from tests.integration.support import csv_file, row, sidecar
from tests.support import COLUMNS, FULL, SINCE, at, contract, fold, schema_hash


def publish(store, tmp_path, rows, *, c=None, columns=COLUMNS, day=18, shape="full", **kwargs):
    c = c or contract()
    path = csv_file(tmp_path, f"{shape}-{day}.csv", rows, columns)
    manifest = sidecar(path, c, shape, day, len(rows), **kwargs)
    return deliver(store, c, _FileSource(path, columns), manifest, at(30))


@pytest.mark.parametrize("name", ["*.csv", "?.csv", "[ab].csv", "{one,two}.csv"])
def test_a_delivered_path_never_expands_to_other_files(tmp_path, name):
    c = contract()
    supplied = csv_file(tmp_path, name, [row(1)])
    csv_file(tmp_path, "other.csv", [row(2)])
    with DuckDBStore(":memory:") as store:
        manifest = sidecar(supplied, c, "full", 18, 1)
        assert deliver(store, c, _FileSource(supplied, COLUMNS), manifest, at(20)).accepted
        assert [r["id"] for r in store.read(c.table)] == [1]
        assert not store._candidates


def test_existing_untracked_tables_require_explicit_adoption(tmp_path):
    path, c = tmp_path / "landing.duckdb", contract()
    with DuckDBStore(path) as store:
        store.connection.execute("CREATE TABLE moves(id BIGINT, day DATE, value VARCHAR)")
        store.connection.execute("INSERT INTO moves VALUES (1, DATE '2026-10-18', 'original')")
        with pytest.raises(StoreError, match="adopt=True"):
            publish(store, tmp_path, [row(2)], c=c)
        assert store.read(c.table) == [row(1, value="original")]
    with DuckDBStore(path, adopt=True) as store:
        assert publish(store, tmp_path, [row(2)], c=c).accepted
        assert store.read(c.table) == [row(2)]


def test_declared_csv_types_preserve_numeric_strings_nulls_and_integral_floats(tmp_path):
    columns = {"id": "str", "amount": "float", "nullable": "int"}
    c = Contract(
        "typed",
        ("id",),
        {"full": FULL},
        types={"id": "str"},
        schema_hash=schema_hash(columns),
    )
    path = tmp_path / "typed.csv"
    path.write_text("id,amount,nullable\n001,3,\n002,4,\n")
    m = Manifest(c.table, "full", at(18), rows=2, schema_hash=c.schema_hash)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, c, _FileSource(path, columns), m, at(20))
        assert receipt.accepted
        assert receipt.source == str(path)
        assert store.read(c.table) == [
            {"id": "001", "amount": 3.0, "nullable": None},
            {"id": "002", "amount": 4.0, "nullable": None},
        ]


def test_bad_csv_cast_reports_only_evaluated_predicates(tmp_path):
    c = contract()
    with DuckDBStore(":memory:") as store:
        receipt = publish(store, tmp_path, [{"id": "bad", "day": "2026-10-18", "value": "x"}], c=c)
        outcomes = {check.name: check for check in receipt.checks}
        assert not receipt.accepted
        assert not outcomes["schema_hash"].passed
        for name in ("key_integrity", "window_present"):
            assert outcomes[name].detail == "not evaluated"
            assert not outcomes[name].passed


def test_paths_and_row_iterators_publish_identical_values(tmp_path):
    c, rows = contract(), [row(1), row(2)]
    m = Manifest(c.table, "full", at(18), rows=2, schema_hash=c.schema_hash)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, c, (r for r in rows), m, at(20))
        assert receipt.accepted
        assert store.read(c.table) == rows
        assert store.accepted_receipt(m.identity) == receipt


def test_a_held_csv_can_be_checked_twice_and_published_once(tmp_path):
    c, rows = contract(), [row(1), row(2)]
    path = csv_file(tmp_path, "incoming.csv", rows)
    manifest = sidecar(path, c, "full", 18, len(rows))
    with DuckDBStore(":memory:") as store:
        candidate = store.hold(_FileSource(path, COLUMNS), manifest, c)
        requirements = Gate.requirements(c, manifest)
        first = store.check(candidate, c, manifest, requirements)
        repeated = store.check(candidate, c, manifest, requirements)
        assert repeated == first and all(check.passed for check in repeated)
        receipt = Gate.evaluate(store.load_ledger(c), manifest, repeated, now=at(20))
        assert receipt.accepted
        store.publish(candidate, receipt)
        assert store.read_publication(c) == (rows, fold(receipt))
        assert store.receipts(c.table) == (receipt,)
        assert not store._candidates and store._transaction is None


@pytest.mark.parametrize("typed", [True, False])
def test_a_held_file_with_tombstones_can_be_checked_twice_and_published_once(tmp_path, typed):
    from tests.integration.support import MARKED, marked

    c = contract()
    with DuckDBStore(":memory:") as store:
        base = csv_file(tmp_path, "base.csv", [row(1), row(2)])
        first = store.deliver(
            c, _FileSource(base, COLUMNS), sidecar(base, c, "full", 18, 2, generation="g1"), now=at(30)
        )
        path = csv_file(tmp_path, "feed.csv", marked([row(2, value="new")], (1,)), MARKED)
        manifest = sidecar(path, c, "feed", 19, 1, since="g1", generation="g2", tombstones=1)
        candidate = store.hold(_FileSource(path, MARKED) if typed else path, manifest, c)
        requirements = Gate.requirements(c, manifest)
        checks = store.check(candidate, c, manifest, requirements)
        assert store.check(candidate, c, manifest, requirements) == checks
        assert all(check.passed for check in checks), checks
        receipt = Gate.evaluate(fold(first), manifest, checks, now=at(20))
        store.publish(candidate, receipt)
        assert store.read(c.table) == [row(2, value="new")]


def test_a_file_without_its_key_column_is_rejected_with_a_receipt(tmp_path):
    c = contract()
    path = csv_file(
        tmp_path, "keyless.csv", [{"day": at(18).date(), "value": "x"}], {"day": "date", "value": "str"}
    )
    manifest = sidecar(path, c, "full", 18, 1)
    with DuckDBStore(":memory:") as store:
        receipt = store.deliver(c, path, manifest, now=at(30))
        assert not receipt.accepted
        assert any(check.name == "schema_hash" and not check.passed for check in receipt.checks)
        assert store.receipts(c.table) == (receipt,)


def test_arrow_tables_publish_through_the_same_checks():
    pa = pytest.importorskip("pyarrow")
    c, rows = contract(), [row(1), row(2)]
    m = Manifest(c.table, "full", at(18), rows=2, schema_hash=c.schema_hash)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, c, pa.Table.from_pylist(rows), m, at(20))
        assert receipt.accepted
        assert store.read(c.table) == rows


def test_each_rejection_attempt_is_retained_even_with_an_identical_receiver_time():
    c = contract()
    m = Manifest(c.table, "missing", at(18), rows=0)
    with DuckDBStore(":memory:") as store:
        first = deliver(store, c, object(), m, at(20))
        second = deliver(store, c, object(), m, at(20))
        third = deliver(store, c, object(), m, at(21))
        assert first == second
        assert tuple(r.received_at for r in store.receipts()) == (at(20), at(20), at(21))
        assert len(store.receipts()) == 3
        assert not third.accepted


@pytest.mark.parametrize("older", [False, True])
def test_lost_accepted_receipts_are_never_replaced_by_replay(tmp_path, older):
    c = contract()
    with DuckDBStore(":memory:") as store:
        first = publish(store, tmp_path, [row(1)])
        if older:
            publish(store, tmp_path, [row(2)], day=19)
        store.connection.execute(
            "DELETE FROM __dogana_receipts WHERE manifest_id=?", [first.manifest.identity]
        )
        for operation in (
            lambda: store.accepted_receipt(first.manifest.identity),
            lambda: deliver(store, c, object(), first.manifest, at(30)),
        ):
            with pytest.raises(InconsistentPublication, match="receipt missing"):
                operation()
        assert len(store.receipts()) == int(older)


@pytest.mark.parametrize("rebase", [False, True])
def test_cached_checks_cannot_be_reused_after_the_base_moves(tmp_path, rebase):
    c = contract()
    with DuckDBStore(":memory:") as store:
        first = publish(store, tmp_path, [row(1), row(2)])
        path = csv_file(tmp_path, "held.csv", [row(1)])
        manifest = sidecar(path, c, "full", 20, 1)
        candidate = store.hold(_FileSource(path, COLUMNS), manifest, c)
        checks = store.check(candidate, c, manifest, Gate.requirements(c, manifest))
        assert all(check.passed for check in checks)
        latest = publish(store, tmp_path, [row(i) for i in range(10)], day=19)
        stale = Gate.evaluate(fold(first, latest) if rebase else fold(first), manifest, checks, now=at(30))
        with pytest.raises(StoreError, match="different base" if rebase else "changed after"):
            store.publish(candidate, stale)
        assert store.read(c.table) == [row(i) for i in range(10)]
        assert store.load_ledger(c).identity == latest.ledger_identity
        assert not store._candidates


def test_publication_rejects_a_receipt_bound_to_another_base(tmp_path):
    c = contract()
    with DuckDBStore(":memory:") as store:
        first = publish(store, tmp_path, [row(1)])
        m = Manifest(c.table, "full", at(19), rows=1, schema_hash=c.schema_hash)
        held = store.hold([row(2)], m, c)
        checks = store.check(held, c, m, Gate.requirements(c, m))
        receipt = Gate.evaluate(fold(first), m, checks, now=at(20))
        with pytest.raises(StoreError, match="different base"):
            store.publish(held, replace(receipt, base_publication_id="wrong"))
        assert store.read_publication(c) == ([row(1)], fold(first))


@pytest.mark.parametrize("primitive", [False, True])
def test_same_connection_readers_wait_for_the_whole_publication(tmp_path, monkeypatch, primitive):
    c = contract()
    with DuckDBStore(":memory:") as store:
        first = publish(store, tmp_path, [row(1)])
        staged, release, reader_started = Event(), Event(), Event()

        def paused():
            staged.set()
            assert release.wait(5)

        monkeypatch.setattr(store, "_publication_point", paused)
        m = Manifest(c.table, "full", at(19), rows=1, schema_hash=c.schema_hash)

        def writer():
            if not primitive:
                return deliver(store, c, [row(2)], m, at(20))
            held = store.hold([row(2)], m, c)
            checks = store.check(held, c, m, Gate.requirements(c, m))
            receipt = Gate.evaluate(fold(first), m, checks, now=at(20))
            store.publish(held, receipt)
            return receipt

        def reader():
            reader_started.set()
            return store.read_publication(c)

        with ThreadPoolExecutor(max_workers=2) as pool:
            writing = pool.submit(writer)
            assert staged.wait(5)
            reading = pool.submit(reader)
            assert reader_started.wait(5)
            try:
                assert not reading.done()
            finally:
                release.set()
            second = writing.result(timeout=5)
            rows, ledger = reading.result(timeout=5)
        assert rows == [row(2)]
        assert ledger == fold(first, second)
        assert ledger.publication_id != first.publication_id


def test_one_batched_delete_applies_many_composite_tombstones(tmp_path):
    from tests.integration.test_crashes import StatementFault
    from tests.support import FEED

    columns = {"a": "int", "b": "str"}
    c = Contract(
        "pairs",
        ("a", "b"),
        {"full": FULL, "feed": FEED},
        types={"a": "int", "b": "str", "deleted": "bool"},
        schema_hash=schema_hash(columns),
        deleted_column="deleted",
    )
    rows = [{"a": i, "b": str(i)} for i in range(500)]
    with DuckDBStore(":memory:") as store:
        assert publish(store, tmp_path, rows, c=c, columns=columns, generation="g1").accepted
        trace = StatementFault(store.connection)
        store.connection = trace
        m = Manifest(
            c.table,
            "feed",
            at(19),
            since="g1",
            generation="g2",
            rows=0,
            schema_hash=c.schema_hash,
            tombstones=499,
        )
        deleted = [{"a": i, "b": str(i), "deleted": True} for i in range(499)]
        assert deliver(store, c, Rows(deleted, {**columns, "deleted": "bool"}), m, at(20)).accepted
        deletes = [s for s in trace.statements if s.startswith('DELETE FROM "pairs"')]
        assert len(deletes) == 2
        assert "_deleted" in deletes[-1]
        assert store.read(c.table) == [{"a": 499, "b": "499"}]


def test_schema_evolution_retains_each_original_publication(tmp_path):
    c = contract()
    with DuckDBStore(":memory:") as store:
        first = publish(store, tmp_path, [row(1)], c=c)
        columns = {**COLUMNS, "extra": "str"}
        changed = replace(c, schema_hash=schema_hash(columns), freshness=timedelta(days=10))
        second = publish(store, tmp_path, [{**row(2), "extra": "new"}], c=changed, columns=columns, day=19)
        assert second.accepted
        assert store.read(c.table, first.publication_id) == [row(1)]
        assert store.read(c.table, second.publication_id) == [{**row(2), "extra": "new"}]
        assert Ledger.from_receipts(changed, store.receipts()).identity == second.ledger_identity


def test_keyless_replacement_closes_replaced_history_versions(tmp_path):
    columns = {"event": "str"}
    c = Contract("events", (), {"full": FULL}, schema_hash=schema_hash(columns))
    with DuckDBStore(":memory:") as store:
        first = publish(store, tmp_path, [{"event": "old"}, {"event": "old"}], c=c, columns=columns)
        second = publish(store, tmp_path, [{"event": "new"}], c=c, columns=columns, day=19)
        assert store.read(c.table, first.publication_id) == [{"event": "old"}, {"event": "old"}]
        assert store.read(c.table, second.publication_id) == [{"event": "new"}]


def test_timestamp_rows_are_checked_against_the_declared_extraction_interval(tmp_path):
    columns = {"id": "int", "updated": "datetime"}
    c = Contract(
        "updates",
        ("id",),
        {"full": FULL, "since": SINCE},
        types={"id": "int", "updated": "datetime"},
        schema_hash=schema_hash(columns),
        cursor_column="updated",
    )
    with DuckDBStore(":memory:") as store:
        publish(store, tmp_path, [{"id": 1, "updated": at(18)}], c=c, columns=columns)
        rejected = publish(
            store,
            tmp_path,
            [{"id": 2, "updated": at(18)}],
            c=c,
            columns=columns,
            shape="since",
            day=19,
            since=at(18),
        )
        assert not rejected.accepted
        assert next(check for check in rejected.checks if check.name == "rows_in_interval").passed is False


def test_wrapper_default_uses_the_receiver_clock():
    c = contract()
    now = datetime.now(UTC)
    future = Manifest(c.table, "full", now + timedelta(days=1), rows=0, schema_hash=c.schema_hash)
    with DuckDBStore(":memory:") as store:
        receipt = store.deliver(c, [], future)
        assert not receipt.accepted
        assert now <= receipt.received_at < future.produced_at


@pytest.mark.parametrize("manifest_hash", [None, "sender-only-hash"])
def test_row_mappings_publish_without_a_contract_schema_hash(manifest_hash):
    c = Contract("rows", ("id",), {"full": FULL}, types={"id": "int"})
    rows = [{"id": 1, "name": "first"}, {"id": 2, "name": "second"}]
    m = Manifest(c.table, "full", at(18), rows=2, schema_hash=manifest_hash)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, c, iter(rows), m, at(20))
        assert receipt.accepted, receipt.checks
        assert not any(check.name == "schema_hash" for check in receipt.checks)
        assert store.read_publication(c) == (rows, fold(receipt))
        assert store.receipts(c.table) == (receipt,)
        assert not store._candidates


@pytest.mark.parametrize("rows", [[], [{"id": 1, "amount": None}]])
def test_explicit_row_columns_preserve_empty_and_all_null_candidate_types(rows):
    columns = {"id": "int", "amount": "float"}
    c = Contract("amounts", ("id",), {"full": FULL}, types={"id": "int"}, schema_hash=schema_hash(columns))
    manifest = c.manifest("full", at(18), rows=len(rows))
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, c, Rows(rows, columns), manifest, at(20))
        assert receipt.accepted, receipt.checks
        assert store.read_publication(c) == (rows, fold(receipt))
        assert dict(store._description(c.table)) == {"id": "BIGINT", "amount": "DOUBLE"}


@pytest.mark.parametrize("incoming", [[{"id": 1, "renamed": "new"}], [{"id": 1, "value": 7}]])
def test_candidate_payload_renames_and_retypes_fail_the_external_schema_hash(incoming):
    columns = {"id": "int", "value": "str"}
    c = Contract("items", ("id",), {"full": FULL}, types={"id": "int"}, schema_hash=schema_hash(columns))
    original = [{"id": 1, "value": "old"}]
    with DuckDBStore(":memory:") as store:
        first = deliver(store, c, original, c.manifest("full", at(18), rows=1), at(20))
        assert first.accepted, first.checks
        receipt = deliver(store, c, incoming, c.manifest("full", at(19), rows=1), at(20))
        assert not receipt.accepted
        assert not next(check for check in receipt.checks if check.name == "schema_hash").passed
        assert store.read_publication(c) == (original, fold(first))
        assert store.receipts(c.table) == (first, receipt)


def test_adopting_a_table_with_reordered_columns_refuses_publication():
    columns = {"id": "int", "amount": "int"}
    c = Contract("pairs", ("id",), {"full": FULL}, types={"id": "int"}, schema_hash=schema_hash(columns))
    manifest = Manifest(c.table, "full", at(18), rows=1, schema_hash=c.schema_hash)
    with DuckDBStore(":memory:", adopt=True) as store:
        store.connection.execute("CREATE TABLE pairs(amount BIGINT, id BIGINT)")
        store.connection.execute("INSERT INTO pairs VALUES (99, 1)")
        with pytest.raises(StoreError, match="published column order or types differ"):
            deliver(store, c, [{"id": 2, "amount": 80}], manifest, at(20))
        assert store.read_publication(c) == ([{"id": 1, "amount": 99}], Ledger(c))
        assert store.receipts(c.table) == ()
        assert store.publication_stamp(c.table) is None
        assert not store._candidates and store._transaction is None


@pytest.mark.parametrize("shape", ["full", "since"])
@pytest.mark.parametrize("after_check", [False, True])
@pytest.mark.parametrize("change", ["reorder", "rename", "retype"])
def test_changing_published_columns_refuses_publication_without_losing_history(shape, after_check, change):
    columns = {"id": "int", "amount": "int"}
    c = Contract(
        "pairs",
        ("id",),
        {"full": FULL, "since": SINCE},
        types={"id": "int"},
        schema_hash=schema_hash(columns),
    )
    original = [{"id": 1, "amount": 99}, {"id": 2, "amount": 70}]
    with DuckDBStore(":memory:") as store:
        first_manifest = Manifest(c.table, "full", at(18), rows=2, schema_hash=c.schema_hash)
        first = deliver(store, c, original, first_manifest, at(20))
        manifest = Manifest(
            c.table,
            shape,
            at(19),
            since=at(18) if shape == "since" else None,
            rows=1,
            schema_hash=c.schema_hash,
        )
        incoming = [{"id": 3, "amount": 80}]
        candidate = None
        if after_check:
            candidate = store.hold(incoming, manifest, c)
            checks = store.check(candidate, c, manifest, Gate.requirements(c, manifest))
            checked = Gate.evaluate(fold(first), manifest, checks, now=at(20))
            assert checked.accepted
        statements = {
            "reorder": "CREATE OR REPLACE TABLE pairs AS SELECT amount, id FROM pairs",
            "rename": "ALTER TABLE pairs RENAME COLUMN amount TO total",
            "retype": "ALTER TABLE pairs ALTER COLUMN amount TYPE VARCHAR",
        }
        store.connection.execute(statements[change])
        changed_rows = store.read(c.table)
        with pytest.raises(StoreError, match="published column order or types differ"):
            if after_check:
                store.publish(candidate, checked)
            else:
                deliver(store, c, incoming, manifest, at(20))
        assert store.read(c.table) == changed_rows
        assert store.read_publication(c) == (original, fold(first))
        assert store.read(c.table, first.publication_id) == original
        assert store.receipts(c.table) == (first,)
        assert store.publication_stamp(c.table) == first.publication_id
        assert not store._candidates and store._transaction is None


@pytest.mark.parametrize(
    ("value", "accepted"), [("2026-10-18 00:00:00", False), ("2026-10-18 02:00:00+02", True)]
)
def test_a_csv_time_names_an_instant_only_with_its_offset(tmp_path, value, accepted):
    columns = {"id": "int", "updated": "datetime"}
    c = Contract("updates", ("id",), {"full": FULL}, types={"id": "int"}, schema_hash=schema_hash(columns))
    path = tmp_path / "updates.csv"
    path.write_text(f"id,updated\n1,{value}\n")
    manifest = Manifest(c.table, "full", at(18), rows=1, schema_hash=c.schema_hash)
    with DuckDBStore(":memory:") as store:
        assert deliver(store, c, _FileSource(path, columns), manifest, at(20)).accepted is accepted
        assert not accepted or store.read(c.table) == [{"id": 1, "updated": at(18)}]


@pytest.mark.parametrize("metadata", ["ledgers", "receipts"])
def test_invalid_persisted_evidence_is_an_operational_failure(tmp_path, metadata):
    c = contract()
    with DuckDBStore(":memory:") as store:
        first = publish(store, tmp_path, [row(1)])
        store.connection.execute(f"UPDATE __dogana_{metadata} SET document='{{'")
        with pytest.raises(InconsistentPublication, match="invalid"):
            deliver(store, c, object(), first.manifest, at(30))
        assert store.connection.execute("SELECT count(*) FROM __dogana_receipts").fetchone()[0] == 1
        assert store.read(c.table) == [row(1)]


def test_aborting_an_unknown_candidate_cannot_drop_a_published_table(tmp_path):
    c = contract()
    with DuckDBStore(":memory:") as store:
        first = publish(store, tmp_path, [row(1)])
        store.abort(c.table)
        assert store.read_publication(c) == ([row(1)], fold(first))


def test_evolved_history_cannot_collide_with_another_user_table():
    from hashlib import sha256

    columns = {"id": "int", "value": "str"}
    original = Contract(
        "items", ("id",), {"full": FULL}, types={"id": "int"}, schema_hash=schema_hash(columns)
    )
    evolved = replace(original, schema_hash=schema_hash({**columns, "extra": "str"}))
    with DuckDBStore(":memory:") as store:
        first = Manifest(original.table, "full", at(18), rows=1, schema_hash=original.schema_hash)
        assert deliver(store, original, [{"id": 1, "value": "old"}], first, at(20)).accepted
        next_manifest = Manifest(evolved.table, "full", at(19), rows=1, schema_hash=evolved.schema_hash)
        expected = [{"id": 1, "value": "new", "extra": "own"}]
        second = deliver(store, evolved, expected, next_manifest, at(20))
        suffix = sha256(repr(store._description(original.table)).encode()).hexdigest()[:16]
        other = replace(evolved, table=f"items_{suffix}")
        manifest = Manifest(other.table, "full", at(19), rows=1, schema_hash=other.schema_hash)
        foreign = [{"id": 2, "value": "foreign", "extra": "other"}]
        assert deliver(store, other, foreign, manifest, at(20)).accepted
        rows, ledger = store.read_publication(evolved)
        assert (rows, ledger.identity) == (expected, second.ledger_identity)
        assert store.read(other.table) == foreign


@pytest.mark.parametrize("source_kind", ["csv", "rows"])
def test_fractional_keys_cannot_be_rounded_into_another_published_key(tmp_path, source_kind):
    columns = {"id": "int", "value": "str"}
    c = Contract(
        "amounts",
        ("id",),
        {"full": FULL, "since": SINCE},
        types={"id": "int"},
        schema_hash=schema_hash(columns),
    )
    original = [{"id": 2, "value": "original"}]
    with DuckDBStore(":memory:") as store:
        first = Manifest(c.table, "full", at(18), rows=1, schema_hash=c.schema_hash)
        published = deliver(store, c, original, first, at(20))
        rows = [{"id": 1.6, "value": "replacement"}]
        source = (
            _FileSource(csv_file(tmp_path, "fraction.csv", rows, columns), columns)
            if source_kind == "csv"
            else rows
        )
        changed = Manifest(c.table, "since", at(19), since=at(18), rows=1, schema_hash=c.schema_hash)
        receipt = deliver(store, c, source, changed, at(20))
        assert not receipt.accepted
        assert store.read_publication(c) == (original, fold(published))


@pytest.mark.parametrize("after", [False, True])
def test_a_lost_rollback_response_is_resolved_before_unlocking(tmp_path, after):
    c = contract()
    with DuckDBStore(":memory:") as store:
        first = publish(store, tmp_path, [row(1)])
        m = Manifest(c.table, "full", at(19), rows=1, schema_hash=c.schema_hash)
        held = store.hold([row(2)], m, c)
        checks = store.check(held, c, m, Gate.requirements(c, m))
        receipt = Gate.evaluate(fold(first), m, checks, now=at(20))
        connection = store.connection

        def interrupted():
            raise OSError("interrupted after rows")

        class RollbackFault:
            fired = False

            def execute(self, sql, *args):
                if sql == "ROLLBACK" and not self.fired:
                    self.fired = True
                    if after:
                        connection.execute(sql, *args)
                    raise OSError("rollback response unavailable")
                return connection.execute(sql, *args)

        store.connection = RollbackFault()
        store._publication_point = interrupted
        try:
            with pytest.raises(StoreError, match="interrupted after rows"):
                store.publish(held, receipt)
        finally:
            store.connection = connection
        assert store._transaction is None
        assert not store._candidates
        assert store.read_publication(c) == ([row(1)], fold(first))


def test_an_unresolved_rollback_keeps_uncommitted_rows_unreadable(tmp_path):
    c = contract()
    with DuckDBStore(":memory:") as store:
        first = publish(store, tmp_path, [row(1)])
        m = Manifest(c.table, "full", at(19), rows=1, schema_hash=c.schema_hash)
        held = store.hold([row(2)], m, c)
        checks = store.check(held, c, m, Gate.requirements(c, m))
        receipt = Gate.evaluate(fold(first), m, checks, now=at(20))
        assert receipt.transition is not None
        store._open((held,))
        store._write_rows(held, receipt.transition.plan)
        connection = store.connection

        class RollbackFault:
            def execute(self, sql, *args):
                if sql == "ROLLBACK":
                    raise OSError("rollback unavailable")
                return connection.execute(sql, *args)

        store.connection = RollbackFault()
        try:
            with pytest.raises(StoreError, match="rollback unavailable"):
                store.abort(held)
            assert store._transaction == (held,)
            with pytest.raises(StoreError, match="publish or abort"):
                store.read_publication(c)
        finally:
            store.connection = connection
            store.abort(held)
        assert store.read_publication(c) == ([row(1)], fold(first))


def test_schema_evolution_removes_replaced_rows_before_converting_survivors():
    columns = {"id": "int", "value": "str"}
    original = Contract(
        "amounts",
        ("id",),
        {"full": FULL, "since": SINCE},
        types={"id": "int"},
        schema_hash=schema_hash(columns),
    )
    changed = replace(original, schema_hash=schema_hash({"id": "int", "value": "int"}))
    with DuckDBStore(":memory:") as store:
        first = Manifest(original.table, "full", at(18), rows=1, schema_hash=original.schema_hash)
        published = deliver(store, original, [{"id": 1, "value": "old"}], first, at(20))
        next_manifest = Manifest(
            changed.table, "since", at(19), since=at(18), rows=1, schema_hash=changed.schema_hash
        )
        receipt = deliver(store, changed, [{"id": 1, "value": 7}], next_manifest, at(20))
        assert receipt.accepted
        assert store.read_publication(changed) == ([{"id": 1, "value": 7}], fold(published, receipt))
        assert store.read(original.table, published.publication_id) == [{"id": 1, "value": "old"}]


def test_nested_arrow_values_fail_schema_checks_as_a_rejected_delivery():
    pa = pytest.importorskip("pyarrow")
    columns = {"id": "int", "amount": "int"}
    c = Contract("amounts", ("id",), {"full": FULL}, types={"id": "int"}, schema_hash=schema_hash(columns))
    manifest = Manifest(c.table, "full", at(18), rows=1, schema_hash=c.schema_hash)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, c, pa.table({"id": [1], "amount": [[2]]}), manifest, at(20))
        assert not receipt.accepted
        assert next(check for check in receipt.checks if check.name == "schema_hash").passed is False
        assert store.receipts(c.table) == (receipt,)


@pytest.mark.parametrize("operation", ["receipts", "repair"])
def test_invalid_retained_history_is_an_operational_failure(tmp_path, operation):
    c = contract()
    with DuckDBStore(":memory:") as store:
        publish(store, tmp_path, [row(1)])
        store.connection.execute("UPDATE __dogana_receipts SET document='{' ")
        with pytest.raises(InconsistentPublication, match="stored receipt is invalid"):
            store.receipts(c.table) if operation == "receipts" else store.repair(c)


def test_primitive_checks_publish_typed_csv_without_a_contract_schema_hash(tmp_path):
    columns = {"id": "int"}
    c = Contract("rows", ("id",), {"full": FULL}, types={"id": "int"})
    path = csv_file(tmp_path, "rows.csv", [{"id": 1}], columns)
    manifest = Manifest(c.table, "full", at(18), rows=1)
    with DuckDBStore(":memory:") as store:
        candidate = store.hold(_FileSource(path, columns), manifest, c)
        requirements = Gate.requirements(c, manifest)
        assert not any(check.name == "schema_hash" for check in requirements)
        checks = store.check(candidate, c, manifest, requirements)
        receipt = Gate.evaluate(store.load_ledger(c), manifest, checks, now=at(20))
        assert receipt.accepted, receipt.checks
        store.publish(candidate, receipt)
        assert store.read_publication(c) == ([{"id": 1}], fold(receipt))
        assert store.receipts(c.table) == (receipt,)
        assert not store._candidates and store._transaction is None


def test_csv_header_renaming_cannot_hide_schema_drift(tmp_path):
    columns = {"id": "int", "id_1": "str"}
    c = Contract("items", ("id",), {"full": FULL}, types={"id": "int"}, schema_hash=schema_hash(columns))
    path = tmp_path / "renamed.csv"
    path.write_text("id,id\n1,value\n", encoding="utf-8")
    manifest = c.manifest("full", at(18), rows=1)
    with DuckDBStore(":memory:") as store:
        receipt = deliver(store, c, _FileSource(path, columns), manifest, at(20))
        assert not receipt.accepted
        check = next(check for check in receipt.checks if check.name == "schema_hash")
        assert not check.passed and "header" in check.detail
        assert store.load_ledger(c).publication_id is None


@pytest.mark.parametrize("hashed", [False, True])
def test_changed_candidate_types_cannot_recast_surviving_published_rows(hashed):
    columns = {"id": "int", "value": "str"}
    original = Contract(
        "items",
        ("id",),
        {"full": FULL, "since": SINCE},
        types={"id": "int"},
        schema_hash=schema_hash(columns) if hashed else None,
    )
    changed = replace(original, schema_hash=schema_hash({"id": "int", "value": "int"}) if hashed else None)
    before = [{"id": 1, "value": "one"}, {"id": 2, "value": "two"}]
    with DuckDBStore(":memory:") as store:
        first = deliver(store, original, before, original.manifest("full", at(18), rows=2), at(20))
        assert first.accepted
        manifest = changed.manifest("since", at(19), since=at(18), rows=1)
        receipt = deliver(store, changed, [{"id": 1, "value": 7}], manifest, at(20))
        assert not receipt.accepted
        assert store.read_publication(original) == (before, fold(first))
        assert store.receipts(original.table) == (first, receipt)
