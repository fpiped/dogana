from dataclasses import replace
from datetime import UTC, date, timedelta

import pytest

pytest.importorskip("duckdb")

from dogana import Claims, Contract, Ledger, Manifest, Receipt, State, next_extraction
from dogana.reference._sources import _FileSource
from dogana.reference.duckdb import DuckDBStore
from dogana.reference.files import FileDrop, FilePattern
from tests.integration.support import MARKED, csv_file, marked, row, sidecar
from tests.support import COLUMNS, FEED, FULL, SINCE, WINDOW, at, contract, schema_hash


def test_a_week_of_erp_files_for_three_tables_preserves_rows_dates_receipts_and_surface_claims(tmp_path):
    contracts = [
        contract("moves"),
        contract("invoices", shapes={"full": FULL, "since": SINCE}),
        contract("stock", shapes={"full": FULL, "feed": FEED}),
    ]
    source = {1: {"id": 1, "day": date(2026, 9, 1), "value": "old"}, 2: row(2)}
    expected = {c.table: dict(source) for c in contracts}
    publication_ids = {}
    receipt_count = 0
    db_path = tmp_path / "landing.duckdb"
    for day in range(18, 26):
        if day > 18:
            source.pop(1, None)
            source[2] = row(2, value=f"day{day}")
            source[day] = row(day, day)
        with DuckDBStore(db_path) as store:
            for c in contracts:
                kwargs = {}
                if day in (18, 25):
                    shape, incoming = "full", list(source.values())
                    if c.table == "stock":
                        kwargs["generation"] = f"g{day}"
                    expected[c.table] = dict(source)
                elif c.table == "moves":
                    low = (at(day) - timedelta(days=30)).date()
                    high = at(day).date()
                    shape = "daily"
                    incoming = [r for r in source.values() if low <= r["day"] <= high]
                    kwargs["window"] = (low, high)
                    expected[c.table] = {
                        k: r for k, r in expected[c.table].items() if not low <= r["day"] <= high
                    }
                    expected[c.table].update({r["id"]: r for r in incoming})
                    if day == 21:
                        bad_rows = incoming + [{"id": 999, "day": date(2026, 9, 1), "value": "late"}]
                        path = csv_file(tmp_path, "late.csv", bad_rows)
                        sidecar(path, c, shape, day, len(bad_rows), **kwargs)
                        before = store.load_ledger(c).to_json(), store.read(c.table)
                        rejected = FileDrop(store, c, columns=COLUMNS).ingest(path, now=at(30))
                        receipt_count += 1
                        assert not rejected.accepted
                        assert any(x.name == "rows_in_window" and not x.passed for x in rejected.checks)
                        assert (store.load_ledger(c).to_json(), store.read(c.table)) == before
                else:
                    incoming = [source[2], source[day]]
                    shape = "since" if c.table == "invoices" else "feed"
                    kwargs["since"] = at(day - 1) if shape == "since" else f"g{day - 1}"
                    if shape == "feed":
                        deleted = (1,) if day == 19 else ()
                        kwargs.update(generation=f"g{day}", tombstones=len(deleted))
                        expected[c.table].pop(1, None)
                    expected[c.table].update({r["id"]: r for r in incoming})
                columns, delivered = COLUMNS, incoming
                if shape == "feed":
                    columns, delivered = MARKED, marked(incoming, deleted)
                path = csv_file(tmp_path, f"{c.table}_{day}.csv", delivered, columns)
                manifest = sidecar(path, c, shape, day, len(incoming), **kwargs)
                receipt = FileDrop(store, c, columns=columns).ingest(path, now=at(30))
                assert receipt.accepted, receipt.checks
                receipt_count += 1
                publication_ids[c.table, day] = receipt.publication_id
                assert store.read(c.table) == sorted(expected[c.table].values(), key=lambda r: r["id"])
                ledger = store.load_ledger(c)
                assert ledger == receipt.ledger
                replayed = Ledger(c)
                for previous_receipt in store.receipts(c.table):
                    if previous_receipt.accepted:
                        replayed = replayed.apply(previous_receipt.manifest)
                assert replayed == ledger
                assert ledger == Ledger.from_json(ledger.to_json())
                assert store.receipts(c.table)[-1] == Receipt.from_json(receipt.to_json())
                claim = ledger.claims(at(day))
                if day in (18, 25) or c.table == "stock":
                    assert (claim.state, claim.complete_as_of, claim.deletions_as_of) == (
                        State.COMPLETE,
                        at(day),
                        at(day),
                    )
                elif c.table == "moves":
                    assert (claim.complete_as_of, claim.deletions_as_of) == (at(18), at(18))
                    assert ledger.claims(at(day), window=kwargs["window"]).deletions_as_of == at(day)
                else:
                    assert (claim.state, claim.complete_as_of, claim.deletions_as_of) == (
                        State.PATCHED,
                        at(day),
                        at(18),
                    )
                assert store.deliver(c, path, receipt.manifest, now=at(30)) == replace(
                    receipt, outcome="replayed"
                )
                assert store.deliver(c, tmp_path / "already_removed.csv", manifest, now=at(30)).outcome == (
                    "rejected"
                )
                receipt_count += 1
                assert sum(r.accepted for r in store.receipts(c.table)) == day - 17
            assert len(store.receipts()) == receipt_count
            surface = Claims.combine(*(store.load_ledger(c).claims(at(day)) for c in contracts))
            assert surface.state is (State.COMPLETE if day in (18, 25) else State.PATCHED)
            assert surface.deletions_as_of == at(day if day in (18, 25) else 18)
            assert store.read("moves", publication_ids["moves", 18]) == sorted(
                [{"id": 1, "day": date(2026, 9, 1), "value": "old"}, row(2)], key=lambda r: r["id"]
            )


@pytest.mark.parametrize(
    "problem",
    [
        "late",
        "count",
        "missing_column",
        "retyped_column",
        "shrink",
        "shape",
        "duplicate_key",
        "null_key",
        "schema_hash",
        "time_backwards",
    ],
)
def test_a_contradictory_file_is_rejected_without_changing_the_publication(tmp_path, problem):
    c = contract(max_shrink_fraction=0.25)
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "base.csv", [row(i) for i in range(4)])
        sidecar(path, c, "full", 18, 4)
        first = FileDrop(store, c, columns=COLUMNS).ingest(path, now=at(30))
        assert first.accepted
        incoming, shape, kwargs, columns = [row(i) for i in range(4)], "full", {}, COLUMNS
        if problem == "late":
            shape, kwargs = "daily", {"window": (at(19).date(), at(20).date())}
        elif problem == "missing_column":
            incoming, columns = (
                [{"id": i, "day": at(19).date()} for i in range(4)],
                {"id": "int", "day": "date"},
            )
        elif problem == "retyped_column":
            incoming[0]["id"] = "not_an_integer"
        elif problem == "shrink":
            incoming = incoming[:2]
        elif problem == "shape":
            shape = "not_admitted"
        elif problem == "duplicate_key":
            incoming[1]["id"] = 0
        elif problem == "null_key":
            incoming[1]["id"] = None
        path = csv_file(tmp_path, "bad.csv", incoming, columns)
        manifest = sidecar(
            path,
            c,
            shape,
            19 if problem != "time_backwards" else 17,
            len(incoming) + (problem == "count"),
            **kwargs,
        )
        if problem == "schema_hash":
            manifest = replace(manifest, schema_hash="wrong")
            path.with_suffix(".csv.json").write_text(manifest.to_json())
        before = store.load_ledger(c), store.read(c.table)
        receipt = FileDrop(store, c, columns=COLUMNS).ingest(path, now=at(30))
        assert not receipt.accepted
        assert receipt.publication_id == first.publication_id
        assert (store.load_ledger(c), store.read(c.table)) == before
        assert len(store.receipts()) == 2
        assert not store.connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE '__dogana_candidate_%'"
        ).fetchone()[0]


def test_a_source_restore_is_refused_and_a_new_full_export_reanchors_the_tombstone_feed(tmp_path):
    c = contract(shapes={"full": FULL, "feed": FEED})
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "base.csv", [row(1), row(2)])
        sidecar(path, c, "full", 18, 2, generation="g41")
        assert FileDrop(store, c, columns=COLUMNS).ingest(path, now=at(30)).accepted
        path = csv_file(tmp_path, "restored.csv", marked([row(3)], (1,)), MARKED)
        sidecar(path, c, "feed", 19, 1, since="backup-g3", generation="backup-g4", tombstones=1)
        rejected = FileDrop(store, c, columns=MARKED).ingest(path, now=at(30))
        assert not rejected.accepted
        assert store.read(c.table) == [row(1), row(2)]
        path = csv_file(tmp_path, "rebase.csv", [row(2), row(3)])
        sidecar(path, c, "full", 20, 2, generation="new-g1")
        assert FileDrop(store, c, columns=COLUMNS).ingest(path, now=at(30)).accepted
        path = csv_file(tmp_path, "feed.csv", marked([row(4)], (2,)), MARKED)
        sidecar(path, c, "feed", 21, 1, since="new-g1", generation="new-g2", tombstones=1)
        accepted = FileDrop(store, c, columns=MARKED).ingest(path, now=at(30))
        assert accepted.accepted
        assert store.read(c.table) == [row(3), row(4)]
        assert accepted.claims.deletions_as_of == at(21)


def test_parquet_and_csv_use_the_same_gate_and_deletions_by_absence(tmp_path):
    import duckdb

    c = contract()
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        csv = csv_file(tmp_path, "base.csv", [row(1), row(2)])
        m = sidecar(csv, c, "full", 18, 2)
        assert store.deliver(c, _FileSource(csv, COLUMNS), m, now=at(30)).accepted
        parquet = tmp_path / "next.parquet"
        with duckdb.connect() as writer:
            writer.execute("CREATE TABLE rows (id BIGINT, day DATE, value VARCHAR)")
            writer.execute("INSERT INTO rows VALUES (2, DATE '2026-10-18', 'new')")
            writer.execute("COPY rows TO ? (FORMAT PARQUET)", [str(parquet)])
        m = sidecar(parquet, c, "full", 19, 1)
        receipt = FileDrop(store, c, columns=COLUMNS).ingest(parquet, now=at(30))
        assert receipt.accepted
        assert store.read(c.table) == [row(2, value="new")]


def test_an_empty_plant_file_decommissions_only_that_partition_and_leaves_undelivered_plants_unknown(
    tmp_path,
):
    from dogana import Checkpoint, Completeness, Coverage, Deletions, Shape

    plant = Shape(
        coverage=Coverage.WINDOW,
        completeness=Completeness.COMPLETE,
        deletions=Deletions.ABSENCE,
        checkpoint=Checkpoint.NONE,
    )
    columns = {"id": "int", "plant": "str"}
    c = Contract(
        "plants",
        ("id",),
        {"plant": plant},
        window_column="plant",
        types={"id": "int", "plant": "str"},
        schema_hash=schema_hash(columns),
        max_shrink_fraction=None,
    )
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        for day, name, rows in [
            (18, "IT01", [{"id": 1, "plant": "IT01"}]),
            (19, "DE01", [{"id": 2, "plant": "DE01"}]),
            (20, "IT01", []),
        ]:
            path = csv_file(tmp_path, f"{name}_{day}.csv", rows, columns)
            sidecar(path, c, "plant", day, len(rows), window=(name, name))
            receipt = FileDrop(store, c, columns=columns).ingest(path, now=at(30))
            assert receipt.accepted, receipt.checks
        assert store.read(c.table) == [{"id": 2, "plant": "DE01"}]
        assert store.load_ledger(c).claims(at(20), window=("IT01", "IT01")).deletions_as_of == at(20)
        assert store.load_ledger(c).claims(at(20), window=("US01", "US01")).state is State.UNKNOWN


def test_a_failure_after_row_changes_rolls_back_rows_ledger_and_receipt_together(tmp_path, monkeypatch):
    c = contract()
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "base.csv", [row(1)])
        m = sidecar(path, c, "full", 18, 1)
        first = store.deliver(c, _FileSource(path, COLUMNS), m, now=at(30))
        assert first.accepted

        def fail():
            raise ValueError("publication storage failed")

        monkeypatch.setattr(store, "_publication_point", fail)
        path = csv_file(tmp_path, "new.csv", [row(2)])
        m = sidecar(path, c, "full", 19, 1)
        from dogana.errors import StoreError

        with pytest.raises(StoreError, match="publication storage failed"):
            store.deliver(c, _FileSource(path, COLUMNS), m, now=at(30))
        assert store.read(c.table) == [row(1)]
        assert store.load_ledger(c) == first.ledger
        assert [r.accepted for r in store.receipts()] == [True]
        assert store.read(c.table, first.publication_id) == [row(1)]


def test_a_filename_pattern_derives_a_manifest_when_the_sender_cannot_write_sidecars(tmp_path):
    c = contract()
    pattern = FilePattern(
        r"(?P<shape>full)_(?P<produced_at>\d{4}-\d{2}-\d{2})_(?P<rows>\d+)\.csv", timezone=UTC
    )
    path = csv_file(tmp_path, "full_2026-10-18_2.csv", [row(1), row(2)])
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        receipt = FileDrop(store, c, pattern, columns=COLUMNS).ingest(path, now=at(30))
        assert receipt.accepted
        assert receipt.manifest.rows == 2
        assert receipt.manifest.produced_at == at(18)
        assert store.read(c.table) == [row(1), row(2)]


def test_the_same_table_under_push_and_pull_has_the_same_rows_ledger_and_receipt(tmp_path):
    c = contract(shapes={"full": FULL, "since": SINCE})
    files = [csv_file(tmp_path, "full.csv", [row(1)]), csv_file(tmp_path, "since.csv", [row(2)])]
    manifests = [sidecar(files[0], c, "full", 18, 1), sidecar(files[1], c, "since", 19, 1, since=at(18))]

    class FileExtractor:
        def extract(self, contract, request):
            index = 0 if request.shape == "full" else 1
            m = manifests[index]
            assert (request.until, request.since) == (m.produced_at, m.since)
            return m, _FileSource(files[index], COLUMNS)

    extractor = FileExtractor()
    with DuckDBStore(tmp_path / "push.duckdb") as push, DuckDBStore(tmp_path / "pull.duckdb") as pull:
        for m, path in zip(manifests, files, strict=True):
            pushed = push.deliver(c, _FileSource(path, COLUMNS), m, now=at(30))
            request = next_extraction(pull.load_ledger(c), m.shape, m.produced_at)
            pulled_m, source = extractor.extract(c, request)
            pulled = pull.deliver(c, source, pulled_m, now=at(30))
            assert pushed == pulled
            assert push.read(c.table) == pull.read(c.table)
            assert push.load_ledger(c) == pull.load_ledger(c)


def test_an_append_only_log_has_no_keys_and_replay_does_not_duplicate_events(tmp_path):
    columns = {"event": "str"}
    c = Contract("events", (), {"log": SINCE}, schema_hash=schema_hash(columns), start_at=at(1))
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        for day, since in [(18, at(1)), (19, at(18))]:
            path = csv_file(tmp_path, f"{day}.csv", [{"event": f"event{day}"}], columns)
            m = sidecar(path, c, "log", day, 1, since=since)
            receipt = store.deliver(c, _FileSource(path, columns), m, now=at(30))
            assert receipt.accepted
            assert receipt.transition.plan.actions[0].operation.value == "append"
            assert store.deliver(c, _FileSource(path, columns), m, now=at(30)) == replace(
                receipt, outcome="replayed"
            )
        assert store.read(c.table) == [{"event": "event18"}, {"event": "event19"}]
        assert len(store.receipts()) == 2


def test_a_keyless_table_is_replaced_whole_or_by_window_without_key_matching(tmp_path):
    columns = {"plant": "str", "qty": "int"}
    c = Contract(
        "receipts",
        (),
        {"full": FULL, "plant": replace(WINDOW, window_size=None)},
        window_column="plant",
        types={"plant": "str"},
        schema_hash=schema_hash(columns),
    )
    rows = [{"plant": "A", "qty": 1}, {"plant": "A", "qty": 1}, {"plant": "B", "qty": 2}]
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "full.csv", rows, columns)
        assert store.deliver(
            c, _FileSource(path, columns), sidecar(path, c, "full", 18, 3), now=at(30)
        ).accepted
        assert store.read(c.table) == rows
        path = csv_file(tmp_path, "a.csv", [{"plant": "A", "qty": 5}], columns)
        receipt = store.deliver(
            c, _FileSource(path, columns), sidecar(path, c, "plant", 19, 1, window=("A", "A")), now=at(30)
        )
        assert receipt.accepted
        assert sorted(store.read(c.table), key=lambda r: r["plant"]) == [
            {"plant": "A", "qty": 5},
            {"plant": "B", "qty": 2},
        ]
        assert store.load_ledger(c).claims(at(19), window=("B", "B")).deletions_as_of == at(18)


def test_a_window_cannot_move_a_key_whose_old_region_it_does_not_cover(tmp_path):
    """Day 18 stays claimed exact at the full's boundary, so taking key 1 out of it would be false."""
    c = contract()
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "base.csv", [row(1, 18)])
        m = sidecar(path, c, "full", 18, 1)
        assert store.deliver(c, _FileSource(path, COLUMNS), m, now=at(30)).accepted
        path = csv_file(tmp_path, "moved.csv", [row(1, 19)])
        m = sidecar(path, c, "daily", 19, 1, window=(at(19).date(), at(19).date()))
        refused = store.deliver(c, _FileSource(path, COLUMNS), m, now=at(30))
        assert any(x.name == "rows_in_window" and not x.passed for x in refused.checks)
        assert store.read(c.table) == [row(1, 18)]


def test_a_window_tombstone_cannot_delete_a_key_from_a_different_region(tmp_path):
    from dogana import Checkpoint, Completeness, Coverage, Deletions, Shape

    feed = Shape(
        coverage=Coverage.WINDOW,
        completeness=Completeness.CHANGED_ONLY,
        deletions=Deletions.TOMBSTONE,
        checkpoint=Checkpoint.TIMESTAMP,
    )
    c = contract(shapes={"full": FULL, "feed_window": feed})
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "base.csv", [row(1, 18), row(2, 19)])
        m = sidecar(path, c, "full", 19, 2, generation="g1")
        first = store.deliver(c, _FileSource(path, COLUMNS), m, now=at(30))
        path = csv_file(tmp_path, "delete.csv", marked([], (1,)), MARKED)
        m = sidecar(
            path, c, "feed_window", 20, 0, window=(at(19).date(), at(19).date()), since=at(19), tombstones=1
        )
        receipt = store.deliver(c, _FileSource(path, MARKED), m, now=at(30))
        assert not receipt.accepted
        assert [x.name for x in receipt.checks if not x.passed] == ["rows_in_window"]
        assert store.read(c.table) == [row(1, 18), row(2, 19)]
        assert store.load_ledger(c) == first.ledger


def test_an_out_of_order_overlapping_file_cannot_overwrite_newer_rows(tmp_path):
    c = contract()
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "new.csv", [row(1, 19, value="new")])
        m = sidecar(path, c, "daily", 21, 1, window=(at(18).date(), at(20).date()))
        first = store.deliver(c, _FileSource(path, COLUMNS), m, now=at(30))
        assert first.accepted
        path = csv_file(tmp_path, "old.csv", [row(1, 19, value="old")])
        m = sidecar(path, c, "daily", 20, 1, window=(at(17).date(), at(19).date()))
        refused = store.deliver(c, _FileSource(path, COLUMNS), m, now=at(30))
        assert not refused.accepted
        assert store.read(c.table) == [row(1, 19, value="new")]
        assert store.load_ledger(c) == first.ledger


def test_marked_rows_must_match_the_declared_counts_and_never_name_a_live_key(tmp_path):
    c = contract(shapes={"full": FULL, "feed": FEED})
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "base.csv", [row(1)])
        m = sidecar(path, c, "full", 18, 1, generation="g1")
        assert store.deliver(c, _FileSource(path, COLUMNS), m, now=at(30)).accepted
        for name, deleted, declared, failed in [
            ("undeclared.csv", (1,), 0, "row_count"),
            ("overdeclared.csv", (), 1, "row_count"),
            ("both.csv", (2,), 1, "key_integrity"),
        ]:
            path = csv_file(tmp_path, name, marked([row(2)], deleted), MARKED)
            m = sidecar(path, c, "feed", 19, 1, since="g1", generation="g2", tombstones=declared)
            receipt = store.deliver(c, _FileSource(path, MARKED), m, now=at(30))
            assert [x.name for x in receipt.checks if not x.passed] == [failed], name
        assert store.read(c.table) == [row(1)]


@pytest.mark.parametrize("remaining,accepted", [(3, True), (2, False)])
def test_shrink_tolerance_includes_its_exact_fraction_boundary(tmp_path, remaining, accepted):
    c = contract(max_shrink_fraction=0.7)
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "base.csv", [row(i) for i in range(10)])
        m = sidecar(path, c, "full", 18, 10)
        assert store.deliver(c, _FileSource(path, COLUMNS), m, now=at(30)).accepted
        path = csv_file(tmp_path, "next.csv", [row(i) for i in range(remaining)])
        m = sidecar(path, c, "full", 19, remaining)
        receipt = store.deliver(c, _FileSource(path, COLUMNS), m, now=at(30))
        assert receipt.accepted is accepted
        assert len(store.read(c.table)) == (remaining if accepted else 10)


def test_a_manifest_naming_the_wrong_table_is_audited_against_the_receiving_contract(tmp_path):
    c = contract()
    manifest = Manifest("other", "full", at(18), rows=0, schema_hash=c.schema_hash)
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        receipt = store.deliver(c, tmp_path / "not_read.csv", manifest, now=at(30))
        assert not receipt.accepted
        assert store.receipts(c.table) == (receipt,)
        assert receipt.manifest.table == "other"
        assert store.load_ledger(c) == Ledger(c)


def test_a_pull_extractor_reads_updated_at_from_the_ledgers_interval_and_keeps_unreported_deletes(tmp_path):
    import csv

    columns = {"id": "int", "updated_at": "datetime", "value": "str"}
    c = contract(shapes={"full": FULL, "since": SINCE}, columns=columns)
    source = [
        {"id": 1, "updated_at": at(17), "value": "old"},
        {"id": 2, "updated_at": at(18), "value": "initial"},
    ]

    class ERPExtractor:
        def extract(self, contract, request):
            rows = (
                source
                if request.shape == "full"
                else [r for r in source if request.since <= r["updated_at"] <= request.until]
            )
            path = tmp_path / f"{request.shape}.csv"
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows)
            manifest = Manifest(
                contract.table,
                request.shape,
                request.until,
                since=request.since,
                rows=len(rows),
                schema_hash=contract.schema_hash,
            )
            return manifest, _FileSource(path, columns)

    extractor = ERPExtractor()
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        request = next_extraction(store.load_ledger(c), "full", at(18))
        manifest, source_file = extractor.extract(c, request)
        assert store.deliver(c, source_file, manifest, now=at(30)).accepted
        source[:] = [
            {"id": 2, "updated_at": at(19), "value": "updated"},
            {"id": 3, "updated_at": at(19), "value": "new"},
        ]
        request = next_extraction(store.load_ledger(c), "since", at(19))
        assert request.interval == (at(18), at(19))
        manifest, source_file = extractor.extract(c, request)
        receipt = store.deliver(c, source_file, manifest, now=at(30))
        assert receipt.accepted, receipt.checks
        assert store.read(c.table) == [{"id": 1, "updated_at": at(17), "value": "old"}, *source]
        assert receipt.claims.complete_as_of == at(19)
        assert receipt.claims.deletions_as_of == at(18)
        assert receipt.claims.state is State.EXPIRED
        assert receipt.ledger.claims(at(19)).state is State.PATCHED


def test_different_spelling_cannot_create_two_ledgers_for_one_duckdb_table(tmp_path):
    from dogana import InvalidContract

    c = contract("Moves")
    alias = contract("moves")
    with DuckDBStore(tmp_path / "landing.duckdb") as store:
        path = csv_file(tmp_path, "base.csv", [row(1)])
        manifest = sidecar(path, c, "full", 18, 1)
        first = store.deliver(c, _FileSource(path, COLUMNS), manifest, now=at(30))
        assert first.accepted
        with pytest.raises(InvalidContract, match="letter case"):
            store.deliver(
                alias,
                _FileSource(path, COLUMNS),
                replace(manifest, table=alias.table, produced_at=at(19)),
                now=at(30),
            )
        with pytest.raises(InvalidContract, match="letter case"):
            store.read(alias.table)
        assert store.read(c.table) == [row(1)]
        assert store.load_ledger(c) == first.ledger
