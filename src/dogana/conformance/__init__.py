"""Inherit StoreConformance in pytest and supply a factory fixture."""

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from threading import Event
from typing import Any, Protocol, cast

import pytest

from dogana import (
    Checkpoint,
    Claims,
    Completeness,
    Contract,
    Coverage,
    Deletions,
    Gate,
    Group,
    InconsistentPublication,
    Ledger,
    Manifest,
    Outcome,
    Receipt,
    Reference,
    Shape,
    State,
    Store,
    StoreError,
    TogetherStore,
    deliver_together,
    next_extraction,
)
from dogana import (
    deliver as _core_deliver,
)
from dogana.errors import LedgerAhead, SerializationError, StampAhead

FULL = Shape(
    coverage=Coverage.ALL,
    completeness=Completeness.COMPLETE,
    deletions=Deletions.ABSENCE,
    checkpoint=Checkpoint.NONE,
)
WINDOW = Shape(
    coverage=Coverage.WINDOW,
    completeness=Completeness.COMPLETE,
    deletions=Deletions.ABSENCE,
    checkpoint=Checkpoint.NONE,
)
SINCE = Shape(
    coverage=Coverage.ALL,
    completeness=Completeness.CHANGED_ONLY,
    deletions=Deletions.NONE,
    checkpoint=Checkpoint.TIMESTAMP,
)
FEED = Shape(
    coverage=Coverage.ALL,
    completeness=Completeness.CHANGED_ONLY,
    deletions=Deletions.TOMBSTONE,
    checkpoint=Checkpoint.GENERATION,
)
CHANGES = Shape(
    coverage=Coverage.ALL,
    completeness=Completeness.CHANGED_ONLY,
    deletions=Deletions.TOMBSTONE,
    checkpoint=Checkpoint.TIMESTAMP,
)
COLUMNS = {"id": "int", "day": "date", "value": "str"}
LINE_COLUMNS = {"id": "int", "order_id": "int", "day": "date", "value": "str"}


def _schema_hash(columns: Mapping[str, str]) -> str:
    pairs = json.dumps(list(columns.items()), ensure_ascii=True, separators=(",", ":"))
    return sha256(pairs.encode("ascii")).hexdigest()


class StoreFactory(Protocol):
    """Transport and deliberate persistence faults supplied by the store's tests."""

    def make_store(self) -> Store: ...
    def write_source(self, rows: Sequence[Mapping[str, Any]], columns: Mapping[str, str]) -> object: ...
    def content_hash(self, source: object) -> str | None:
        """The SHA-256 of the bytes `source` delivers, or None when it delivers rows without bytes."""
        ...

    def read_table(self, store: Store, table: str) -> list[dict[str, Any]]: ...
    def fail_after_rows(self, store: Store) -> AbstractContextManager[None]: ...
    def read_publication(self, store: Store, contract: Contract) -> tuple[list[dict[str, Any]], Ledger]: ...
    def pause_after_rows(self, store: Store) -> AbstractContextManager[tuple[Event, Event]]: ...


class StampedStore(Store, Protocol):
    def publication_stamp(self, table: str) -> str | None: ...
    def repair(self, contract: Contract) -> Ledger: ...


class StampedStoreFactory(StoreFactory, Protocol):
    def set_ledger(self, store: Store, ledger: Ledger) -> None: ...
    def set_stamp(self, store: Store, table: str, publication_id: str | None) -> None: ...
    def remove_ledger(self, store: Store, table: str) -> None: ...


def _stamped(store: Store) -> StampedStore:
    if not all(callable(getattr(store, name, None)) for name in ("publication_stamp", "repair")):
        pytest.skip("store does not expose both stamped-ledger operations")
    return cast(StampedStore, store)


def _grouped(store: Store) -> TogetherStore:
    if not all(callable(getattr(store, name, None)) for name in ("check_references", "publish_together")):
        pytest.skip("store does not publish tables together")
    return cast(TogetherStore, store)


def _at(day: int) -> datetime:
    return datetime(2026, 10, day, tzinfo=UTC)


def _contract(table: str = "moves", shapes: dict[str, Shape] | None = None, **kwargs: Any) -> Contract:
    shapes = shapes or {"full": FULL, "daily": WINDOW, "since": SINCE, "feed": FEED}
    window_column = "day" if any(s.coverage == "window" for s in shapes.values()) else None
    deleted_column = "deleted" if any(s.deletions == "tombstone" for s in shapes.values()) else None
    kwargs.setdefault("max_shrink_fraction", None)
    kwargs.setdefault("schema_hash", _schema_hash(COLUMNS))
    types = {
        "id": "int",
        **({"day": "date"} if window_column else {}),
        **({"deleted": "bool"} if deleted_column else {}),
    }
    return Contract(
        table=table,
        key=("id",),
        shapes=shapes,
        window_column=window_column,
        types=types,
        deleted_column=deleted_column,
        **kwargs,
    )


def _orders(references: bool = True) -> Group:
    """Orders and their lines, each line naming its order."""
    orders = _contract("orders", {"full": FULL, "changes": CHANGES})
    lines = _contract(
        "order_lines",
        {"full": FULL, "daily": WINDOW, "changes": CHANGES, "since": SINCE},
        schema_hash=_schema_hash(LINE_COLUMNS),
    )
    named = (Reference("order_lines", ("order_id",), "orders"),) if references else ()
    return Group((orders, lines), named)


def _line(key: int, order: int | None, day: int = 18) -> dict[str, Any]:
    return {"id": key, "order_id": order, "day": _at(day).date(), "value": "current"}


def _submit(store: Store, contract: Contract, source: object, manifest: Manifest) -> Receipt:
    return _core_deliver(store, contract, source, manifest, _at(30))


def _row(key: int, day: int = 18, value: str = "current") -> dict[str, Any]:
    return {"id": key, "day": _at(day).date(), "value": value}


def _marked(
    contract: Contract,
    shape: str,
    rows: Sequence[Mapping[str, Any]],
    tombstones: Sequence[int],
    columns: Mapping[str, str],
) -> tuple[list[dict[str, Any]], Mapping[str, str]]:
    """Rows as a tombstone shape's file holds them: live rows unmarked, then a marked row per deleted key."""
    marker = contract.deleted_column
    if marker is None or contract.shape(shape).deletions is not Deletions.TOMBSTONE:
        assert not tombstones
        return [dict(row) for row in rows], columns
    deleted = [{**dict.fromkeys(columns), "id": key, marker: True} for key in tombstones]
    return [*({**row, marker: False} for row in rows), *deleted], {**columns, marker: "bool"}


class _StoreScenarios:
    """Shared publication fixtures and assertions."""

    @pytest.fixture
    def store(self, factory: StoreFactory) -> Iterator[Store]:
        store = factory.make_store()
        yield store
        close = getattr(store, "close", None)
        if close is not None:
            close()

    def _deliver(
        self,
        factory: StoreFactory,
        store: Store,
        contract: Contract,
        rows: Sequence[Mapping[str, Any]],
        shape: str = "full",
        day: int = 18,
        *,
        columns: Mapping[str, str] = COLUMNS,
        tombstones: Sequence[int] = (),
        **kwargs: Any,
    ) -> Receipt:
        """Deliver rows; a contract with a deleted column receives it, with a marked row per tombstone."""
        manifest = Manifest(
            table=contract.table,
            shape=shape,
            produced_at=_at(day),
            rows=len(rows),
            schema_hash=contract.schema_hash,
            tombstones=len(tombstones),
            **kwargs,
        )
        rows, columns = _marked(contract, shape, rows, tombstones, columns)
        receipt = _submit(store, contract, factory.write_source(rows, columns), manifest)
        self._fold(store, contract)
        return receipt

    def _fold(self, store: Store, contract: Contract) -> None:
        receipts = store.receipts(contract.table)
        assert Ledger.from_receipts(contract, receipts) == store.load_ledger(contract)
        assert all(Receipt.from_json(r.to_json()) == r for r in receipts)

    def _rows(self, factory: StoreFactory, store: Store, table: str) -> list[dict[str, Any]]:
        return sorted(factory.read_table(store, table), key=lambda r: tuple(r.values()))


class StoreConformance(_StoreScenarios):
    """Required publication, replay and atomicity obligations."""

    @pytest.mark.parametrize("rows", [[], [_row(1)], [{**_row(1), "value": None}]])
    def test_a_full_delivery_without_a_schema_hash_publishes_rows_and_evidence(
        self, factory: StoreFactory, store: Store, rows: list[dict[str, Any]]
    ) -> None:
        c = _contract(schema_hash=None)
        receipt = self._deliver(factory, store, c, rows)
        assert receipt.accepted, receipt.checks
        assert receipt.manifest.schema_hash is None
        assert all(check.name != "schema_hash" for check in receipt.checks)
        assert factory.read_table(store, c.table) == rows
        assert store.load_ledger(c) == receipt.ledger
        assert receipt.claims.complete_as_of == receipt.claims.deletions_as_of == _at(18)
        assert _submit(store, c, object(), receipt.manifest) == replace(receipt, outcome=Outcome.REPLAYED)
        assert store.receipts(c.table) == (receipt,)

    def test_a_null_payload_value_keeps_its_declared_column_type(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        rows = [{**_row(1), "value": None}]
        receipt = self._deliver(factory, store, c, rows)
        assert receipt.accepted, receipt.checks
        assert any(check.name == "schema_hash" and check.passed for check in receipt.checks)
        assert factory.read_table(store, c.table) == rows

    def test_a_week_of_erp_deliveries_proves_three_tables_rows_dates_receipts_and_claims(
        self, factory: StoreFactory, store: Store
    ) -> None:
        contracts = [
            _contract(),
            _contract("invoices", {"full": FULL, "since": SINCE}),
            _contract("stock", {"full": FULL, "feed": FEED}),
        ]
        source = {1: {"id": 1, "day": date(2026, 9, 1), "value": "old"}, 2: _row(2)}
        expected = {c.table: dict(source) for c in contracts}
        for day in range(18, 26):
            if day > 18:
                source.pop(1, None)
                source.update({2: _row(2, value=f"day{day}"), day: _row(day, day)})
            for c in contracts:
                kwargs: dict[str, Any] = {}
                if day in (18, 25):
                    shape, incoming = "full", list(source.values())
                    expected[c.table] = dict(source)
                    if c.table == "stock":
                        kwargs["generation"] = f"g{day}"
                elif c.table == "moves":
                    low, high = (_at(day) - timedelta(days=29)).date(), _at(day).date()
                    shape, kwargs = "daily", {"window": (low, high)}
                    incoming = [r for r in source.values() if low <= r["day"] <= high]
                    expected[c.table] = {
                        k: r for k, r in expected[c.table].items() if not low <= r["day"] <= high
                    }
                    if day == 21:
                        before = store.load_ledger(c), self._rows(factory, store, c.table)
                        bad = self._deliver(
                            factory,
                            store,
                            c,
                            [*incoming, {"id": 999, "day": date(2026, 9, 1), "value": "outside"}],
                            shape,
                            day,
                            **kwargs,
                        )
                        assert not bad.accepted
                        assert (store.load_ledger(c), self._rows(factory, store, c.table)) == before
                else:
                    incoming = [source[2], source[day]]
                    shape = "since" if c.table == "invoices" else "feed"
                    kwargs["since"] = _at(day - 1) if shape == "since" else f"g{day - 1}"
                    if shape == "feed":
                        kwargs.update(generation=f"g{day}", tombstones=(1,) if day == 19 else ())
                        expected[c.table].pop(1, None)
                expected[c.table].update({r["id"]: r for r in incoming})
                receipt = self._deliver(factory, store, c, incoming, shape, day, **kwargs)
                assert receipt.accepted, receipt.checks
                assert self._rows(factory, store, c.table) == sorted(
                    expected[c.table].values(), key=lambda r: r["id"]
                )
                ledger = store.load_ledger(c)
                claim = ledger.claims(_at(day))
                if day in (18, 25) or c.table == "stock":
                    assert (claim.state, claim.complete_as_of, claim.deletions_as_of) == (
                        State.COMPLETE,
                        _at(day),
                        _at(day),
                    )
                elif c.table == "moves":
                    assert (claim.complete_as_of, claim.deletions_as_of) == (_at(18), _at(18))
                    assert ledger.claims(_at(day), window=kwargs["window"]).deletions_as_of == _at(day)
                else:
                    assert (claim.state, claim.complete_as_of, claim.deletions_as_of) == (
                        State.PATCHED,
                        _at(day),
                        _at(18),
                    )
                audit_before = store.receipts(c.table)
                assert _submit(store, c, object(), receipt.manifest) == replace(
                    receipt, outcome=Outcome.REPLAYED
                )
                assert store.receipts(c.table) == audit_before
                assert sum(r.accepted for r in store.receipts(c.table)) == day - 17
            surface = Claims.combine(*(store.load_ledger(c).claims(_at(day)) for c in contracts))
            assert surface.state is (State.COMPLETE if day in (18, 25) else State.PATCHED)
            assert surface.deletions_as_of == _at(day if day in (18, 25) else 18)
        assert sum(len(store.receipts(c.table)) for c in contracts) == 25

    @pytest.mark.parametrize(
        "problem",
        [
            "window",
            "count",
            "schema",
            "duplicate",
            "null",
            "shrink",
            "missing_column",
            "renamed_column",
            "retyped_column",
            "invalid_value",
            "shape",
            "time_backwards",
        ],
    )
    def test_a_contradictory_delivery_keeps_the_previous_publication(
        self, factory: StoreFactory, store: Store, problem: str
    ) -> None:
        c = _contract(max_shrink_fraction=0.25)
        first = self._deliver(factory, store, c, [_row(i) for i in range(4)])
        kwargs: dict[str, Any] = {}
        rows, shape = [_row(i) for i in range(4)], "full"
        if problem == "window":
            shape, kwargs = "daily", {"window": (_at(19).date(), _at(20).date())}
        elif problem == "duplicate":
            rows[1]["id"] = 0
        elif problem == "null":
            rows[1]["id"] = None
        elif problem == "shrink":
            rows = rows[:2]
        columns = dict(COLUMNS)
        if problem == "missing_column":
            columns.pop("value")
            rows = [{name: row[name] for name in columns} for row in rows]
        elif problem == "renamed_column":
            columns = {"id": "int", "day": "date", "label": "str"}
            rows = [{"id": row["id"], "day": row["day"], "label": row["value"]} for row in rows]
        elif problem == "retyped_column":
            columns["value"] = "int"
            for row in rows:
                row["value"] = 1
        elif problem == "invalid_value":
            rows[0]["id"] = "not_an_integer"
        elif problem == "shape":
            shape = "not_admitted"
        manifest = Manifest(
            table=c.table,
            shape=shape,
            produced_at=_at(17) if problem == "time_backwards" else _at(19),
            rows=len(rows) + (problem == "count"),
            schema_hash="wrong" if problem == "schema" else c.schema_hash,
            **kwargs,
        )
        before = self._rows(factory, store, c.table)
        refused = _submit(store, c, factory.write_source(rows, columns), manifest)
        assert not refused.accepted
        if problem in {"schema", "missing_column", "renamed_column", "retyped_column"}:
            assert any(check.name == "schema_hash" and not check.passed for check in refused.checks)
        assert refused.publication_id == first.publication_id
        assert self._rows(factory, store, c.table) == before
        assert store.load_ledger(c) == first.ledger
        assert store.receipts(c.table) == (first, refused)
        self._fold(store, c)

    @pytest.mark.parametrize("shape", ["full", "daily", "since", "feed"])
    def test_failure_after_rows_rolls_back_before_retry_and_replay(
        self, factory: StoreFactory, store: Store, shape: str
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1), _row(2)], generation="g1")
        choices: dict[str, dict[str, Any]] = {
            "full": {},
            "daily": {"window": (_at(18).date(), _at(19).date())},
            "since": {"since": _at(18)},
            "feed": {"since": "g1", "generation": "g2", "tombstones": 1},
        }
        kwargs = choices[shape]
        rows = [_row(2, value="new")]
        manifest = Manifest(
            table=c.table, shape=shape, produced_at=_at(19), rows=1, schema_hash=c.schema_hash, **kwargs
        )
        source = factory.write_source(*_marked(c, shape, rows, (1,) if shape == "feed" else (), COLUMNS))
        with factory.fail_after_rows(store):
            with pytest.raises(StoreError):
                _submit(store, c, source, manifest)
        assert store.load_ledger(c) == first.ledger
        assert self._rows(factory, store, c.table) == [_row(1), _row(2)]
        assert not any(r.accepted and r.manifest == manifest for r in store.receipts(c.table))
        retried = _submit(store, c, source, manifest)
        assert retried.accepted, retried.checks
        assert self._rows(factory, store, c.table) == ([_row(1), *rows] if shape == "since" else rows)
        assert _submit(store, c, object(), manifest) == replace(retried, outcome=Outcome.REPLAYED)
        self._fold(store, c)

    def test_replay_after_a_newer_delivery_returns_the_original_receipt_once(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        second = self._deliver(factory, store, c, [_row(2)], day=19)
        assert _submit(store, c, object(), first.manifest) == replace(first, outcome=Outcome.REPLAYED)
        assert store.load_ledger(c) == second.ledger
        assert self._rows(factory, store, c.table) == [_row(2)]
        assert store.receipts(c.table) == (first, second)
        self._fold(store, c)

    def test_an_out_of_order_overlapping_window_is_refused_whole(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        first = self._deliver(
            factory, store, c, [_row(1, 19)], "daily", 21, window=(_at(18).date(), _at(20).date())
        )
        refused = self._deliver(
            factory, store, c, [_row(1, 19, "old")], "daily", 20, window=(_at(17).date(), _at(19).date())
        )
        assert not refused.accepted
        assert store.load_ledger(c) == first.ledger
        assert self._rows(factory, store, c.table) == [_row(1, 19)]

    def test_append_requires_an_atomic_commit_and_replay_never_duplicates_events(
        self, factory: StoreFactory, store: Store
    ) -> None:
        columns = {"event": "str"}
        c = Contract(
            table="events", key=(), shapes={"log": SINCE}, schema_hash=_schema_hash(columns), start_at=_at(1)
        )
        source = factory.write_source([{"event": "one"}], columns)
        manifest = Manifest(
            table=c.table, shape="log", produced_at=_at(18), since=_at(1), rows=1, schema_hash=c.schema_hash
        )
        first = _submit(store, c, source, manifest)
        assert first.accepted
        assert _submit(store, c, source, manifest) == replace(first, outcome=Outcome.REPLAYED)
        assert factory.read_table(store, c.table) == [{"event": "one"}]
        self._fold(store, c)
        source = factory.write_source([{"event": "two"}], columns)
        second_manifest = replace(manifest, produced_at=_at(19), since=_at(18))
        with factory.fail_after_rows(store):
            with pytest.raises(StoreError):
                _submit(store, c, source, second_manifest)
        assert store.load_ledger(c) == first.ledger
        assert factory.read_table(store, c.table) == [{"event": "one"}]
        self._fold(store, c)
        second = _submit(store, c, source, second_manifest)
        assert second.accepted
        assert _submit(store, c, object(), second_manifest) == replace(second, outcome=Outcome.REPLAYED)
        gap = _submit(store, c, source, replace(second_manifest, produced_at=_at(20), since=_at(17)))
        assert not gap.accepted
        assert factory.read_table(store, c.table) == [{"event": "one"}, {"event": "two"}]
        self._fold(store, c)

    def test_a_cdc_stream_advances_both_dates_for_each_offset_batch_after_a_snapshot(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract(shapes={"full": Shape.full(), "feed": Shape.feed()})
        snapshot = self._deliver(factory, store, c, [_row(1), _row(2)], generation="log:100")
        assert snapshot.accepted
        previous = snapshot
        for day, rows, tombstones, offset in (
            (19, [_row(2, value="updated"), _row(3)], (1,), "log:103"),
            (20, [], (2,), "log:104"),
        ):
            receipt = self._deliver(
                factory,
                store,
                c,
                rows,
                "feed",
                day,
                since=previous.ledger.generation,
                generation=offset,
                tombstones=tombstones,
            )
            assert receipt.accepted, receipt.checks
            assert receipt.base_publication_id == previous.publication_id
            assert receipt.ledger.generation == offset
            assert (receipt.claims.complete_as_of, receipt.claims.deletions_as_of) == (_at(day), _at(day))
            retained = store.receipts(c.table)
            replay = _submit(store, c, object(), receipt.manifest)
            assert replay == replace(receipt, outcome=Outcome.REPLAYED)
            assert replay.identity == receipt.identity
            assert store.receipts(c.table) == retained
            previous = receipt
        assert self._rows(factory, store, c.table) == [_row(3)]
        assert len(store.receipts(c.table)) == 3

    def test_a_cdc_stream_with_a_different_offset_needs_a_new_snapshot(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract(shapes={"full": Shape.full(), "feed": Shape.feed()})
        first = self._deliver(factory, store, c, [_row(1)], generation="log:100")
        for day in (19, 20):
            refused = self._deliver(
                factory,
                store,
                c,
                [_row(2)],
                "feed",
                day,
                since="restored:200",
                generation="restored:201",
            )
            assert not refused.accepted
            failures = [check for check in refused.checks if not check.passed]
            assert len(failures) == 1 and failures[0].name == "transition"
            assert failures[0].detail.endswith(
                "generation_chain: generation chain differs from the base; request a complete delivery"
            )
            assert store.load_ledger(c) == first.ledger
            assert self._rows(factory, store, c.table) == [_row(1)]
        assert self._deliver(factory, store, c, [_row(2)], day=21, generation="restored:201").accepted
        assert self._deliver(
            factory,
            store,
            c,
            [_row(3)],
            "feed",
            22,
            since="restored:201",
            generation="restored:202",
            tombstones=(2,),
        ).accepted
        assert self._rows(factory, store, c.table) == [_row(3)]

    def test_an_event_stream_uses_contiguous_arrival_intervals_without_deduplicating_new_batches(
        self, factory: StoreFactory, store: Store
    ) -> None:
        columns = {"event": "str", "occurred_at": "datetime"}
        c = Contract(
            "events",
            (),
            {"append": Shape.append()},
            schema_hash=_schema_hash(columns),
            start_at=_at(18),
        )
        event = {"event": "late", "occurred_at": _at(17)}
        source = factory.write_source([event], columns)
        manifest = c.manifest("append", _at(19), since=_at(18), rows=1)
        first = _submit(store, c, source, manifest)
        assert first.accepted
        for since in (_at(18), _at(20)):
            refused = _submit(store, c, source, replace(manifest, produced_at=_at(21), since=since))
            assert not refused.accepted
            failures = [check for check in refused.checks if not check.passed]
            assert len(failures) == 1 and failures[0].name == "transition"
            assert failures[0].detail.endswith(
                "append_contiguous: append-only intervals must be contiguous without overlap"
            )
            assert store.load_ledger(c) == first.ledger
            assert factory.read_table(store, c.table) == [event]
        request = next_extraction(store.load_ledger(c), "append", _at(20))
        assert request.since == _at(19)
        second = _submit(store, c, source, c.manifest("append", request.until, since=request.since, rows=1))
        assert second.accepted
        assert (second.claims.complete_as_of, second.claims.deletions_as_of) == (_at(20), _at(20))
        assert factory.read_table(store, c.table) == [event, event]
        retained = store.receipts(c.table)
        assert _submit(store, c, object(), second.manifest) == replace(second, outcome=Outcome.REPLAYED)
        assert factory.read_table(store, c.table) == [event, event]
        assert store.receipts(c.table) == retained
        self._fold(store, c)

    def test_an_upsert_stream_lookback_recovers_a_late_commit_in_the_next_batch(
        self, factory: StoreFactory, store: Store
    ) -> None:
        columns = {"id": "int", "updated_at": "datetime"}
        c = Contract(
            "updates",
            ("id",),
            {
                "full": Shape.full(),
                "upserts": Shape.changes(lookback=timedelta(seconds=10)),
            },
            types=columns,
            schema_hash=_schema_hash(columns),
            cursor_column="updated_at",
        )
        start = _at(18)
        initial: dict[str, Any] = {"id": 1, "updated_at": start}
        assert self._deliver(factory, store, c, [initial], columns=columns).accepted
        late: dict[str, Any] = {"id": 2, "updated_at": start + timedelta(seconds=3)}
        committed_at = start + timedelta(seconds=7)
        for seconds in (5, 10):
            until = start + timedelta(seconds=seconds)
            request = next_extraction(store.load_ledger(c), "upserts", until)
            assert isinstance(request.since, datetime)
            assert request.since == start - timedelta(seconds=10 if seconds == 5 else 5)
            visible = [initial, late] if committed_at <= until else [initial]
            rows = [r for r in visible if request.since < r["updated_at"] <= until]
            manifest = c.manifest("upserts", until, since=request.since, rows=len(rows))
            receipt = _submit(store, c, factory.write_source(rows, columns), manifest)
            assert receipt.accepted, receipt.checks
            assert (receipt.claims.complete_as_of, receipt.claims.deletions_as_of) == (until, start)
            assert self._rows(factory, store, c.table) == visible
        assert late["updated_at"] < start + timedelta(seconds=5) < committed_at
        assert receipt.ledger.claims(until).state is State.PATCHED
        self._fold(store, c)

    def test_a_keyed_row_moving_into_a_window_replaces_its_earlier_copy(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        self._deliver(factory, store, c, [_row(1)])
        accepted = self._deliver(
            factory, store, c, [_row(1, 19)], "daily", 19, window=(_at(19).date(), _at(19).date())
        )
        assert accepted.accepted
        assert self._rows(factory, store, c.table) == [_row(1, 19)]

    def test_a_window_tombstone_cannot_delete_a_key_outside_its_coverage(
        self, factory: StoreFactory, store: Store
    ) -> None:
        feed = Shape(
            coverage=Coverage.WINDOW,
            completeness=Completeness.CHANGED_ONLY,
            deletions=Deletions.TOMBSTONE,
            checkpoint=Checkpoint.TIMESTAMP,
        )
        c = _contract(shapes={"full": FULL, "feed_window": feed})
        first = self._deliver(factory, store, c, [_row(1), _row(2, 19)], generation="g1")
        refused = self._deliver(
            factory,
            store,
            c,
            [],
            "feed_window",
            20,
            since=_at(18),
            window=(_at(19).date(), _at(19).date()),
            tombstones=(1,),
        )
        assert not refused.accepted
        assert store.load_ledger(c) == first.ledger
        assert self._rows(factory, store, c.table) == [_row(1), _row(2, 19)]

    def test_a_key_cannot_be_both_live_and_deleted_in_one_delivery(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)], generation="g1")
        refused = self._deliver(
            factory, store, c, [_row(2)], "feed", 19, since="g1", generation="g2", tombstones=(2,)
        )
        assert not refused.accepted
        assert [check.name for check in refused.checks if not check.passed] == ["key_integrity"]
        assert store.load_ledger(c) == first.ledger

    @pytest.mark.parametrize("declared,delivered", [(0, (1,)), (1, ())])
    def test_the_declared_tombstones_must_be_the_marked_rows(
        self, factory: StoreFactory, store: Store, declared: int, delivered: tuple[int, ...]
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)], generation="g1")
        manifest = c.manifest("feed", _at(19), since="g1", generation="g2", rows=1, tombstones=declared)
        rows, columns = _marked(c, "feed", [_row(2)], delivered, COLUMNS)
        refused = _submit(store, c, factory.write_source(rows, columns), manifest)
        assert [check.name for check in refused.checks if not check.passed] == ["row_count"]
        assert store.load_ledger(c) == first.ledger
        assert self._rows(factory, store, c.table) == [_row(1)]

    @pytest.mark.parametrize("deleted,accepted", [(False, True), (True, False)])
    def test_a_complete_delivery_may_carry_the_deleted_column_but_no_tombstone(
        self, factory: StoreFactory, store: Store, deleted: bool, accepted: bool
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        rows = [{**_row(1), "deleted": False}, {**_row(2), "deleted": deleted}]
        manifest = c.manifest("full", _at(19), rows=2)
        receipt = _submit(store, c, factory.write_source(rows, {**COLUMNS, "deleted": "bool"}), manifest)
        assert receipt.accepted is accepted
        assert [check.name for check in receipt.checks if not check.passed] == (
            [] if accepted else ["row_count"]
        )
        assert self._rows(factory, store, c.table) == ([_row(1), _row(2)] if accepted else [_row(1)])
        assert store.load_ledger(c) == (receipt.ledger if accepted else first.ledger)
        self._fold(store, c)

    def test_an_unmarked_row_is_neither_live_nor_deleted(self, factory: StoreFactory, store: Store) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)], generation="g1")
        manifest = c.manifest("feed", _at(19), since="g1", generation="g2", rows=1)
        rows = [{**_row(2), "deleted": False}, {**_row(3), "deleted": None}]
        refused = _submit(store, c, factory.write_source(rows, {**COLUMNS, "deleted": "bool"}), manifest)
        assert [check.name for check in refused.checks if not check.passed] == ["row_count"]
        assert store.load_ledger(c) == first.ledger

    def test_an_empty_partition_decommissions_it_without_erasing_unknown_history(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        self._deliver(factory, store, c, [_row(1)], "daily", window=(_at(18).date(), _at(18).date()))
        receipt = self._deliver(factory, store, c, [], "daily", 19, window=(_at(18).date(), _at(18).date()))
        assert receipt.accepted
        assert self._rows(factory, store, c.table) == []
        assert receipt.ledger.claims(_at(19)).state is State.UNKNOWN
        day = (_at(18).date(), _at(18).date())
        assert receipt.ledger.claims(_at(19), window=day).deletions_as_of == _at(19)

    def test_a_foreign_manifest_is_audited_under_the_receiving_contract(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        manifest = Manifest(
            table="other", shape="full", produced_at=_at(18), rows=0, schema_hash=c.schema_hash
        )
        receipt = _submit(store, c, object(), manifest)
        assert not receipt.accepted
        assert store.receipts(c.table) == (receipt,)
        self._fold(store, c)

    @pytest.mark.parametrize("remaining,accepted", [(3, True), (2, False)])
    def test_a_complete_export_respects_the_exact_shrink_fraction(
        self, factory: StoreFactory, store: Store, remaining: int, accepted: bool
    ) -> None:
        c = _contract(max_shrink_fraction=0.7)
        first = self._deliver(factory, store, c, [_row(i) for i in range(10)])
        receipt = self._deliver(factory, store, c, [_row(i) for i in range(remaining)], day=19)
        assert receipt.accepted is accepted
        assert len(factory.read_table(store, c.table)) == (remaining if accepted else 10)
        assert store.load_ledger(c) == (receipt.ledger if accepted else first.ledger)

    def test_empty_named_plant_exports_preserve_other_plants_and_unknown_history(
        self, factory: StoreFactory, store: Store
    ) -> None:
        columns = {"id": "int", "plant": "str"}
        c = Contract(
            table="plants",
            key=("id",),
            shapes={"plant": WINDOW},
            window_column="plant",
            types=columns,
            schema_hash=_schema_hash(columns),
            max_shrink_fraction=None,
        )
        for day, name, rows in (
            (18, "IT01", [{"id": 1, "plant": "IT01"}]),
            (19, "DE01", [{"id": 2, "plant": "DE01"}]),
            (20, "IT01", []),
        ):
            assert self._deliver(
                factory, store, c, rows, "plant", day, columns=columns, window=(name, name)
            ).accepted
        assert factory.read_table(store, c.table) == [{"id": 2, "plant": "DE01"}]
        ledger = store.load_ledger(c)
        assert ledger.claims(_at(20), window=("IT01", "IT01")).deletions_as_of == _at(20)
        assert ledger.claims(_at(20), window=("US01", "US01")).state is State.UNKNOWN

    def test_push_and_pull_publish_the_same_rows_ledger_and_receipt(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract(shapes={"full": FULL, "since": SINCE})
        pull = factory.make_store()
        try:
            for day, shape, rows, since in ((18, "full", [_row(1)], None), (19, "since", [_row(2)], _at(18))):
                pushed = self._deliver(factory, store, c, rows, shape, day, since=since)
                request = next_extraction(pull.load_ledger(c), shape, _at(day))
                manifest = Manifest(
                    table=c.table,
                    shape=request.shape,
                    produced_at=request.until,
                    since=request.since,
                    rows=len(rows),
                    schema_hash=c.schema_hash,
                )
                pulled = _submit(pull, c, factory.write_source(rows, COLUMNS), manifest)
                assert pushed.ledger == pulled.ledger
                assert pushed.checks == pulled.checks
                assert self._rows(factory, store, c.table) == self._rows(factory, pull, c.table)
                assert store.load_ledger(c) == pull.load_ledger(c)
                self._fold(pull, c)
        finally:
            close = getattr(pull, "close", None)
            if close is not None:
                close()

    def test_pulling_changed_rows_by_updated_at_keeps_unreported_deletions(
        self, factory: StoreFactory, store: Store
    ) -> None:
        columns = {"id": "int", "updated_at": "datetime", "value": "str"}
        c = Contract(
            table="updates",
            key=("id",),
            shapes={"full": FULL, "since": SINCE},
            types={"id": "int"},
            schema_hash=_schema_hash(columns),
        )
        source: list[dict[str, Any]] = [
            {"id": 1, "updated_at": _at(17), "value": "old"},
            {"id": 2, "updated_at": _at(18), "value": "initial"},
        ]
        for day, shape in ((18, "full"), (19, "since")):
            request = next_extraction(store.load_ledger(c), shape, _at(day))
            if shape == "full":
                rows = source
            else:
                assert isinstance(request.since, datetime)
                rows = [r for r in source if request.since <= r["updated_at"] <= request.until]
            receipt = self._deliver(factory, store, c, rows, shape, day, columns=columns, since=request.since)
            assert receipt.accepted
            if shape == "full":
                source = [
                    {"id": 2, "updated_at": _at(19), "value": "updated"},
                    {"id": 3, "updated_at": _at(19), "value": "new"},
                ]
        assert factory.read_table(store, c.table) == [
            {"id": 1, "updated_at": _at(17), "value": "old"},
            *source,
        ]
        assert receipt.claims.complete_as_of == _at(19)
        assert receipt.claims.deletions_as_of == _at(18)

    def test_two_writers_from_one_base_publish_only_one_candidate(
        self, factory: StoreFactory, store: Store
    ) -> None:
        from dogana import Gate

        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        base = store.load_ledger(c)
        prepared = []
        for key in (2, 3):
            manifest = Manifest(
                table=c.table, shape="full", produced_at=_at(17 + key), rows=1, schema_hash=c.schema_hash
            )
            candidate = store.hold(factory.write_source([_row(key)], COLUMNS), manifest, c)
            checks = store.check(candidate, c, manifest, Gate.requirements(c, manifest))
            receipt = Gate.evaluate(base, manifest, checks, now=_at(30))
            prepared.append((candidate, receipt))
        winner, receipt = prepared[0]
        loser, stale = prepared[1]
        try:
            assert receipt.transition is not None and stale.transition is not None
            store.publish(winner, receipt)
            with pytest.raises(StoreError):
                store.publish(loser, stale)
        finally:
            store.abort(winner)
            store.abort(loser)
        assert store.load_ledger(c) == receipt.ledger
        assert self._rows(factory, store, c.table) == [_row(2)]
        assert store.receipts(c.table) == (first, receipt)

    def test_a_declared_content_hash_that_differs_from_the_delivered_bytes_is_refused(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        source = factory.write_source([_row(1)], COLUMNS)
        manifest = Manifest(
            c.table, "full", _at(18), rows=1, schema_hash=c.schema_hash, content_hash="0" * 64
        )
        refused = _submit(store, c, source, manifest)
        assert not refused.accepted
        assert any(check.name == "content_hash" and not check.passed for check in refused.checks)
        assert store.receipts(c.table) == (refused,)
        assert store.load_ledger(c).publication_id is None

    def test_a_replayed_manifest_over_different_bytes_is_refused(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        original = factory.write_source([_row(1)], COLUMNS)
        if factory.content_hash(original) is None:
            pytest.skip("the store's sources deliver rows without bytes")
        first = self._deliver(factory, store, c, [_row(1)], content_hash=factory.content_hash(original))
        assert _submit(store, c, factory.write_source([_row(1)], COLUMNS), first.manifest).replayed
        refused = _submit(store, c, factory.write_source([_row(2)], COLUMNS), first.manifest)
        assert not refused.accepted and not refused.replayed
        assert any(check.name == "content_hash" and not check.passed for check in refused.checks)
        assert store.load_ledger(c) == first.ledger
        assert self._rows(factory, store, c.table) == [_row(1)]

    def test_a_sequence_read_refuses_rows_outside_its_positions(
        self, factory: StoreFactory, store: Store
    ) -> None:
        columns = {"id": "int", "seq": "int", "value": "str"}
        c = Contract(
            "positions",
            ("id",),
            {"full": FULL, "changes": Shape.changes(checkpoint=Checkpoint.SEQUENCE)},
            types={"id": "int", "seq": "int"},
            cursor_column="seq",
            schema_hash=_schema_hash(columns),
            max_shrink_fraction=None,
        )

        def read(day: int, rows: list[dict[str, Any]], **kwargs: Any) -> Receipt:
            shape = "changes" if "since" in kwargs else "full"
            manifest = Manifest(c.table, shape, _at(day), rows=len(rows), schema_hash=c.schema_hash, **kwargs)
            return _submit(store, c, factory.write_source(rows, columns), manifest)

        assert read(18, [{"id": 1, "seq": 90, "value": "a"}], generation=90).accepted
        assert read(19, [{"id": 2, "seq": 100, "value": "b"}], since=90, generation=100).accepted
        refused = read(20, [{"id": 3, "seq": 99, "value": "c"}], since=100, generation=110)
        assert not refused.accepted
        assert any(check.name == "rows_in_interval" and not check.passed for check in refused.checks)
        assert store.load_ledger(c).generation == 100
        assert [row["id"] for row in self._rows(factory, store, c.table)] == [1, 2]

    def test_corrected_content_hash_is_a_new_delivery_subject_to_time_admission(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        original = factory.write_source([_row(1)], COLUMNS)
        if factory.content_hash(original) is None:
            pytest.skip("the store's sources deliver rows without bytes")
        first = self._deliver(factory, store, c, [_row(1)], content_hash=factory.content_hash(original))
        assert any(check.name == "content_hash" and check.passed for check in first.checks)
        source = factory.write_source([_row(2)], COLUMNS)
        corrected = replace(first.manifest, content_hash=factory.content_hash(source))
        refused = _submit(store, c, source, corrected)
        assert not refused.accepted and not refused.replayed
        assert corrected.identity != first.manifest.identity
        accepted = _submit(store, c, source, replace(corrected, produced_at=_at(19)))
        assert accepted.accepted and not accepted.replayed
        assert self._rows(factory, store, c.table) == [_row(2)]

    def test_future_dated_full_delivery_is_refused_before_source_access(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        manifest = replace(first.manifest, produced_at=_at(31))
        refused = _submit(store, c, object(), manifest)
        assert not refused.accepted
        assert any(not check.passed and check.name == "receiver_time" for check in refused.checks)
        assert store.load_ledger(c) == first.ledger
        assert self._rows(factory, store, c.table) == [_row(1)]

    def test_schema_failure_marks_dependent_checks_not_evaluated(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        manifest = replace(first.manifest, produced_at=_at(19))
        columns = {"id": "int", "day": "date"}
        source = factory.write_source([{"id": 1, "day": _at(18).date()}], columns)
        refused = _submit(store, c, source, manifest)
        checks = {check.name: check for check in refused.checks}
        assert not checks["schema_hash"].passed
        for name in ("key_integrity", "window_stability"):
            assert checks[name].passed is False
            assert checks[name].detail == "not evaluated"

    def test_repeated_refusals_are_distinct_attempts_even_with_the_same_clock(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        manifest = replace(first.manifest, shape="unknown")
        for _ in range(2):
            assert not _submit(store, c, object(), manifest).accepted
        attempts = store.receipts(c.table)
        assert len(attempts) == 3
        assert attempts[1].received_at == attempts[2].received_at == _at(30)

    def test_reader_observes_rows_and_claims_from_one_publication(
        self, factory: StoreFactory, store: Store
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        source = factory.write_source([_row(2)], COLUMNS)
        manifest = replace(first.manifest, produced_at=_at(19))
        reading = Event()

        def read() -> tuple[list[dict[str, Any]], Ledger]:
            reading.set()
            return factory.read_publication(store, c)

        with ThreadPoolExecutor(max_workers=2) as pool:
            with factory.pause_after_rows(store) as (written, resume):
                writer = pool.submit(_submit, store, c, source, manifest)
                try:
                    assert written.wait(5)
                    reader = pool.submit(read)
                    assert reading.wait(5)
                finally:
                    resume.set()
                second = writer.result(timeout=5)
                rows, ledger = reader.result(timeout=5)
        assert (rows, ledger.publication_id) in (
            ([_row(1)], first.publication_id),
            ([_row(2)], second.publication_id),
        )

    def test_a_rejected_manifest_that_cannot_round_trip_leaves_the_audit_readable(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        manifest = replace(first.manifest, produced_at=_at(19))
        object.__setattr__(manifest, "window", (_at(19).date(), _at(18).date()))
        with pytest.raises(SerializationError):
            _submit(store, c, object(), manifest)
        assert store.receipts(c.table) == (first,)
        assert store.load_ledger(c) == first.ledger
        self._fold(store, c)

    def test_a_rejection_cannot_be_persisted_against_a_newer_publication(
        self, factory: StoreFactory, store: Store
    ) -> None:
        from dogana import Gate

        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        refused = Gate.evaluate(first.ledger, replace(first.manifest, shape="missing"), now=_at(30))
        second = self._deliver(factory, store, c, [_row(2)], day=19)
        with pytest.raises(StoreError):
            store.publish(None, refused)
        assert store.receipts(c.table) == (first, second)
        assert store.load_ledger(c) == second.ledger

    def test_checks_require_a_candidate_held_by_the_store(self, factory: StoreFactory, store: Store) -> None:
        from dogana import Gate

        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        manifest = replace(first.manifest, produced_at=_at(19))
        with pytest.raises(StoreError):
            store.check("not_held", c, manifest, Gate.requirements(c, manifest))
        assert store.receipts(c.table) == (first,)
        assert store.load_ledger(c) == first.ledger
        assert self._rows(factory, store, c.table) == [_row(1)]


class StampedLedgerConformance(_StoreScenarios):
    """Optional recovery tests for stores exposing both stamp and repair."""

    def test_stamp_ahead_refuses_until_receipts_repair_the_lost_ledger(
        self, factory: StampedStoreFactory, store: Store
    ) -> None:
        stamped = _stamped(store)
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        second = self._deliver(factory, store, c, [_row(2)], day=19)
        factory.set_ledger(store, first.ledger)
        with pytest.raises(StampAhead):
            store.load_ledger(c)
        with pytest.raises(InconsistentPublication):
            _submit(store, c, object(), second.manifest)
        assert stamped.repair(c) == second.ledger
        assert _submit(store, c, object(), second.manifest) == replace(second, outcome=Outcome.REPLAYED)
        assert self._rows(factory, store, c.table) == [_row(2)]
        self._fold(store, c)

    def test_ledger_ahead_requires_a_new_complete_delivery(
        self, factory: StampedStoreFactory, store: Store
    ) -> None:
        stamped = _stamped(store)
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        second = self._deliver(factory, store, c, [_row(2)], day=19)
        factory.set_stamp(store, c.table, first.publication_id)
        with pytest.raises(LedgerAhead):
            store.load_ledger(c)
        with pytest.raises(InconsistentPublication):
            stamped.repair(c)
        with pytest.raises(InconsistentPublication):
            _submit(store, c, object(), second.manifest)
        with pytest.raises(InconsistentPublication):
            self._deliver(factory, store, c, [_row(2)], "daily", 20, window=(_at(18).date(), _at(19).date()))
        assert self._deliver(factory, store, c, [_row(3)], day=20).accepted
        assert self._rows(factory, store, c.table) == [_row(3)]

    def test_lost_ledger_with_retained_receipts_requires_repair(
        self, factory: StampedStoreFactory, store: Store
    ) -> None:
        stamped = _stamped(store)
        c = _contract()
        receipt = self._deliver(factory, store, c, [_row(1)])
        factory.remove_ledger(store, c.table)
        with pytest.raises(InconsistentPublication):
            _submit(store, c, object(), receipt.manifest)
        assert stamped.repair(c) == receipt.ledger
        assert _submit(store, c, object(), receipt.manifest) == replace(receipt, outcome=Outcome.REPLAYED)


_Deliveries = Mapping[str, tuple[str, Sequence[Mapping[str, Any]], Mapping[str, Any]]]


class TogetherConformance(_StoreScenarios):
    """Optional tests for stores that publish the tables of a group in one atomic step."""

    def _together(
        self,
        factory: StoreFactory,
        store: Store,
        group: Group,
        deliveries: _Deliveries,
        day: int = 18,
    ) -> dict[str, Receipt]:
        """Deliver `(shape, rows, manifest fields)` per table, read at `day` unless a table says otherwise."""
        receipts = deliver_together(
            store, group, self._sources(factory, store, group, deliveries, day), _at(30)
        )
        for contract in group.contracts:
            self._fold(store, contract)
        return receipts

    def _sources(
        self, factory: StoreFactory, store: Store, group: Group, deliveries: _Deliveries, day: int
    ) -> dict[str, tuple[object, Manifest]]:
        _grouped(store)
        sources: dict[str, tuple[object, Manifest]] = {}
        for table, (shape, rows, fields) in deliveries.items():
            contract, fields = group.contract(table), dict(fields)
            tombstones = fields.pop("tombstones", ())
            manifest = Manifest(
                table=table,
                shape=shape,
                produced_at=_at(fields.pop("day", day)),
                rows=fields.pop("rows", len(rows)),
                schema_hash=contract.schema_hash,
                tombstones=len(tombstones),
                **fields,
            )
            marked, columns = _marked(
                contract, shape, rows, tombstones, LINE_COLUMNS if table == "order_lines" else COLUMNS
            )
            sources[table] = (factory.write_source(marked, columns), manifest)
        return sources

    def _held(
        self, factory: StoreFactory, store: Store, group: Group, deliveries: _Deliveries, day: int
    ) -> tuple[dict[str, str], dict[str, Receipt]]:
        """Hold and check each table through the port; return the candidates and their receipts alone."""
        held, alone = {}, {}
        for table, (source, manifest) in self._sources(factory, store, group, deliveries, day).items():
            contract = group.contract(table)
            held[table] = store.hold(source, manifest, contract)
            checks = store.check(held[table], contract, manifest, Gate.requirements(contract, manifest))
            alone[table] = Gate.evaluate(store.load_ledger(contract), manifest, checks, now=_at(30))
        return held, alone

    def _receipts(
        self, store: Store, group: Group, alone: Mapping[str, Receipt], joint: tuple[Any, ...]
    ) -> tuple[tuple[str, Receipt], ...]:
        return tuple(
            (
                table,
                Gate.evaluate(
                    store.load_ledger(group.contract(table)),
                    receipt.manifest,
                    tuple(
                        c
                        for c in receipt.checks
                        if c.name not in ("manifest", "receiver_time", "transition", "check_set")
                    ),
                    now=_at(30),
                    joint=joint,
                ),
            )
            for table, receipt in alone.items()
        )

    def _state(self, factory: StoreFactory, store: Store, group: Group) -> dict[str, Any]:
        return {c.table: factory.read_publication(store, c) for c in group.contracts}

    def _baseline(self, factory: StoreFactory, store: Store, group: Group) -> dict[str, Receipt]:
        receipts = self._together(
            factory,
            store,
            group,
            {
                "orders": ("full", [_row(1), _row(2)], {}),
                "order_lines": ("full", [_line(10, 1), _line(11, 2)], {}),
            },
        )
        assert all(r.accepted for r in receipts.values()), receipts
        return receipts

    def test_a_group_publishes_every_table_at_one_instant(self, factory: StoreFactory, store: Store) -> None:
        group = _orders()
        first = self._baseline(factory, store, group)
        assert {c.name: c.passed for c in first["orders"].checks if c.name in ("together", "references")} == {
            "together": True,
            "references": True,
        }
        ledgers = {c.table: store.load_ledger(c) for c in group.contracts}
        assert group.claims(ledgers, _at(18)).exact_as_of == _at(18)
        second = self._together(
            factory,
            store,
            group,
            {
                "orders": ("changes", [_row(3, 19)], {"since": _at(18)}),
                "order_lines": ("changes", [_line(12, 3, 19)], {"since": _at(18)}),
            },
            day=19,
        )
        assert all(r.accepted for r in second.values()), second
        ledgers = {c.table: store.load_ledger(c) for c in group.contracts}
        assert group.claims(ledgers, _at(19)).exact_as_of == _at(19)
        assert [r["id"] for r in self._rows(factory, store, "order_lines")] == [10, 11, 12]

    def test_a_line_without_its_order_refuses_the_whole_group(
        self, factory: StoreFactory, store: Store
    ) -> None:
        group = _orders()
        self._baseline(factory, store, group)
        before = self._state(factory, store, group)
        receipts = self._together(
            factory,
            store,
            group,
            {
                "orders": ("changes", [], {"since": _at(18)}),
                "order_lines": ("changes", [_line(12, 3, 19)], {"since": _at(18)}),
            },
            day=19,
        )
        assert not any(r.accepted for r in receipts.values())
        failed = {r.ledger.contract.table: [c for c in r.checks if not c.passed] for r in receipts.values()}
        assert [c.name for c in failed["orders"]] == [c.name for c in failed["order_lines"]] == ["references"]
        assert self._state(factory, store, group) == before

    def test_an_order_deleted_with_its_lines_kept_refuses_the_group(
        self, factory: StoreFactory, store: Store
    ) -> None:
        group = _orders()
        self._baseline(factory, store, group)
        kept: _Deliveries = {
            "orders": ("changes", [], {"since": _at(18), "tombstones": (1,)}),
            "order_lines": ("changes", [], {"since": _at(18)}),
        }
        assert not any(r.accepted for r in self._together(factory, store, group, kept, day=19).values())
        cascaded: _Deliveries = {
            **kept,
            "order_lines": ("changes", [], {"since": _at(18), "tombstones": (10,)}),
        }
        receipts = self._together(factory, store, group, cascaded, day=19)
        assert all(r.accepted for r in receipts.values()), receipts
        assert [r["id"] for r in self._rows(factory, store, "order_lines")] == [11]

    def test_a_null_reference_and_rows_outside_the_delivered_window_are_not_checked(
        self, factory: StoreFactory, store: Store
    ) -> None:
        group = _orders()
        receipts = self._together(
            factory,
            store,
            group,
            {
                "orders": ("full", [_row(1), _row(2)], {}),
                "order_lines": ("full", [_line(10, 1, 1), _line(11, 2), _line(12, None)], {}),
            },
        )
        assert all(r.accepted for r in receipts.values()), receipts
        window = (_at(19).date(), _at(19).date())
        receipts = self._together(
            factory,
            store,
            group,
            {
                "orders": ("changes", [], {"since": _at(18), "tombstones": (1,)}),
                "order_lines": ("daily", [], {"window": window}),
            },
            day=19,
        )
        assert all(r.accepted for r in receipts.values()), receipts
        ledgers = {c.table: store.load_ledger(c) for c in group.contracts}
        assert group.claims(ledgers, _at(19)).exact_as_of is None
        assert group.claims(ledgers, _at(19), {"order_lines": window}).exact_as_of == _at(19)

    def test_a_table_failing_its_own_checks_refuses_the_group(
        self, factory: StoreFactory, store: Store
    ) -> None:
        group = _orders()
        self._baseline(factory, store, group)
        before = self._state(factory, store, group)
        receipts = self._together(
            factory,
            store,
            group,
            {
                "orders": ("changes", [_row(3, 19)], {"since": _at(18)}),
                "order_lines": ("changes", [_line(12, 3, 19)], {"since": _at(18), "rows": 2}),
            },
            day=19,
        )
        orders, lines = receipts["orders"], receipts["order_lines"]
        assert not orders.accepted and not lines.accepted
        assert [c.detail for c in orders.checks if not c.passed] == ["refused with order_lines"]
        assert {c.name for c in lines.checks if not c.passed} == {"row_count", "together"}
        assert self._state(factory, store, group) == before

    def test_tables_read_at_different_instants_are_refused_together(
        self, factory: StoreFactory, store: Store
    ) -> None:
        group = _orders()
        self._baseline(factory, store, group)
        receipts = self._together(
            factory,
            store,
            group,
            {
                "orders": ("changes", [_row(3, 19)], {"since": _at(18), "day": 19}),
                "order_lines": ("changes", [_line(12, 3, 19)], {"since": _at(18), "day": 20}),
            },
        )
        for receipt in receipts.values():
            assert [c.detail for c in receipt.checks if not c.passed] == [
                "read boundaries differ: "
                "order_lines 2026-10-20T00:00:00+00:00, orders 2026-10-19T00:00:00+00:00"
            ]

    def test_a_failure_after_rows_publishes_no_table(self, factory: StoreFactory, store: Store) -> None:
        group = _orders(references=False)
        self._baseline(factory, store, group)
        before = self._state(factory, store, group)
        deliveries = {
            "orders": ("changes", [_row(3, 19)], {"since": _at(18)}),
            "order_lines": ("changes", [_line(12, 3, 19)], {"since": _at(18)}),
        }
        with factory.fail_after_rows(store), pytest.raises(StoreError):
            self._together(factory, store, group, deliveries, day=19)
        assert self._state(factory, store, group) == before
        assert all(r.accepted for r in self._together(factory, store, group, deliveries, day=19).values())

    def test_a_group_replays_whole_or_is_refused_whole(self, factory: StoreFactory, store: Store) -> None:
        group = _orders()
        first = self._baseline(factory, store, group)
        audit = store.receipts()
        replayed = self._together(
            factory,
            store,
            group,
            {
                "orders": ("full", [_row(1), _row(2)], {}),
                "order_lines": ("full", [_line(10, 1), _line(11, 2)], {}),
            },
        )
        assert replayed == {t: replace(r, outcome=Outcome.REPLAYED) for t, r in first.items()}
        assert store.receipts() == audit
        alone = self._deliver(
            factory, store, group.contract("orders"), [_row(3, 19)], "changes", 19, since=_at(18)
        )
        assert alone.accepted
        later: _Deliveries = {
            "orders": ("changes", [_row(3, 19)], {"since": _at(18)}),
            "order_lines": ("changes", [_line(12, 3, 19)], {"since": _at(18)}),
        }
        receipts = self._together(factory, store, group, later, day=19)
        for receipt in receipts.values():
            assert [c.detail for c in receipt.checks] == [
                "a group replays whole, and orders was not accepted together with this group"
            ]
        assert self._deliver(
            factory,
            store,
            group.contract("order_lines"),
            [_line(12, 3, 19)],
            "changes",
            19,
            since=_at(18),
            columns=LINE_COLUMNS,
        ).accepted
        receipts = self._together(factory, store, group, later, day=19)
        assert not any(r.accepted for r in receipts.values())

    def test_a_child_that_cannot_be_exact_leaves_its_reference_unchecked(
        self, factory: StoreFactory, store: Store
    ) -> None:
        group = _orders()
        self._baseline(factory, store, group)
        receipts = self._together(
            factory,
            store,
            group,
            {
                "orders": ("changes", [], {"since": _at(18), "tombstones": (1,)}),
                "order_lines": ("since", [], {"since": _at(18)}),
            },
            day=19,
        )
        assert all(r.accepted for r in receipts.values()), receipts
        ledgers = {c.table: store.load_ledger(c) for c in group.contracts}
        assert group.claims(ledgers, _at(19)).exact_as_of is None

    def test_publish_together_needs_the_stores_own_answer_on_exactly_these_candidates(
        self, factory: StoreFactory, store: Store
    ) -> None:
        group = _orders()
        deliveries: _Deliveries = {
            "orders": ("full", [_row(1)], {}),
            "order_lines": ("full", [_line(10, 2)], {}),
        }
        held, alone = self._held(factory, store, group, deliveries, 18)
        together = (Gate.together(tuple(alone.values())),)
        unanswered = tuple((held[t], r) for t, r in self._receipts(store, group, alone, together))
        with pytest.raises(StoreError):
            _grouped(store).publish_together(group, unanswered)
        held, alone = self._held(factory, store, group, deliveries, 18)
        answer = _grouped(store).check_references(group, held)
        assert not answer.passed
        omitted = tuple((held[t], r) for t, r in self._receipts(store, group, alone, together))
        with pytest.raises(StoreError):
            _grouped(store).publish_together(group, omitted)
        held, alone = self._held(factory, store, group, deliveries, 18)
        _grouped(store).check_references(group, held)
        lines, manifest = group.contract("order_lines"), alone["order_lines"].manifest
        store.check(held["order_lines"], lines, manifest, Gate.requirements(lines, manifest))
        stale = tuple((held[t], r) for t, r in self._receipts(store, group, alone, (*together, answer)))
        with pytest.raises(StoreError):
            _grouped(store).publish_together(group, stale)
        assert all(store.load_ledger(c).publication_id is None for c in group.contracts)
        assert store.receipts() == ()

    def test_a_table_published_alone_in_between_leaves_every_table_unpublished(
        self, factory: StoreFactory, store: Store
    ) -> None:
        group = _orders(references=False)
        self._baseline(factory, store, group)
        before = self._state(factory, store, group)["orders"]
        held, alone = self._held(
            factory,
            store,
            group,
            {
                "orders": ("changes", [_row(3, 19)], {"since": _at(18)}),
                "order_lines": ("changes", [_line(12, 3, 19)], {"since": _at(18)}),
            },
            19,
        )
        joint = (Gate.together(tuple(alone.values())),)
        entries = tuple((held[t], r) for t, r in self._receipts(store, group, alone, joint))
        moved = self._deliver(
            factory,
            store,
            group.contract("order_lines"),
            [_line(13, 1, 20)],
            "changes",
            20,
            since=_at(18),
            columns=LINE_COLUMNS,
        )
        assert moved.accepted
        with pytest.raises(StoreError):
            _grouped(store).publish_together(group, entries)
        assert self._state(factory, store, group)["orders"] == before
