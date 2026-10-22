"""Inherit StoreConformance in pytest and supply a factory fixture."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from threading import Event
from typing import Any, Protocol

import pytest

from dogana import (
    Checkpoint,
    Claims,
    Column,
    Completeness,
    Contract,
    Coverage,
    Deletions,
    InconsistentPublication,
    Ledger,
    Manifest,
    Receipt,
    Shape,
    State,
    Store,
    StoreError,
    next_extraction,
)
from dogana import (
    deliver as _core_deliver,
)

FULL = Shape(Coverage.ALL, Completeness.COMPLETE, Deletions.ABSENCE, Checkpoint.NONE)
WINDOW = Shape(Coverage.WINDOW, Completeness.COMPLETE, Deletions.ABSENCE, Checkpoint.NONE)
SINCE = Shape(Coverage.ALL, Completeness.CHANGED_ONLY, Deletions.NONE, Checkpoint.TIMESTAMP)
FEED = Shape(Coverage.KEYS, Completeness.CHANGED_ONLY, Deletions.TOMBSTONE, Checkpoint.GENERATION)
SCHEMA = (Column("id", "int"), Column("day", "date"), Column("value", "str"))


class StoreFactory(Protocol):
    """Transport and deliberate persistence faults supplied by the store's tests."""

    def make_store(self) -> Store: ...
    def write_source(self, rows: Sequence[Mapping[str, Any]], schema: tuple[Column, ...]) -> object: ...
    def read_table(self, store: Store, table: str) -> list[dict[str, Any]]: ...
    def fail_after_rows(self, store: Store) -> AbstractContextManager[None]: ...
    def set_ledger(self, store: Store, ledger: Ledger) -> None: ...
    def set_stamp(self, store: Store, table: str, publication_id: str | None) -> None: ...
    def remove_ledger(self, store: Store, table: str) -> None: ...
    def read_publication(self, store: Store, contract: Contract) -> tuple[list[dict[str, Any]], Ledger]: ...
    def pause_after_rows(self, store: Store) -> AbstractContextManager[tuple[Event, Event]]: ...


def _at(day: int) -> datetime:
    return datetime(2026, 10, day, tzinfo=UTC)


def _contract(table: str = "moves", shapes: dict[str, Shape] | None = None, **kwargs: Any) -> Contract:
    shapes = shapes or {"full": FULL, "daily": WINDOW, "since": SINCE, "feed": FEED}
    window_column = "day" if any(s.coverage == "window" for s in shapes.values()) else None
    kwargs.setdefault("max_shrink_fraction", None)
    return Contract(table, ("id",), shapes, window_column, SCHEMA, **kwargs)


def _submit(store: Store, contract: Contract, source: object, manifest: Manifest) -> Receipt:
    return _core_deliver(store, contract, source, manifest, _at(30))


def _row(key: int, day: int = 18, value: str = "current") -> dict[str, Any]:
    return {"id": key, "day": _at(day).date(), "value": value}


class StoreConformance:
    """Publication, replay and recovery obligations for atomic stores."""

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
        **kwargs: Any,
    ) -> Receipt:
        manifest = Manifest(
            contract.table, shape, _at(day), rows=len(rows), schema_hash=contract.schema_hash, **kwargs
        )
        receipt = _submit(store, contract, factory.write_source(rows, contract.schema), manifest)
        self._fold(store, contract)
        return receipt

    def _fold(self, store: Store, contract: Contract) -> None:
        receipts = store.receipts(contract.table)
        assert Ledger.from_receipts(contract, receipts) == store.load_ledger(contract)
        assert all(Receipt.from_json(r.to_json()) == r for r in receipts)

    def _rows(self, factory: StoreFactory, store: Store, table: str) -> list[dict[str, Any]]:
        return sorted(factory.read_table(store, table), key=lambda r: tuple(r.values()))

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
                        kwargs.update(
                            generation=f"g{day}",
                            keys=tuple((r["id"],) for r in incoming),
                            tombstones=((1,),) if day == 19 else (),
                        )
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
                    assert (claim.state, claim.complete_as_of, claim.valid_as_of) == (
                        State.COMPLETE,
                        _at(day),
                        _at(day),
                    )
                elif c.table == "moves":
                    assert (claim.complete_as_of, claim.valid_as_of) == (_at(18), _at(18))
                    assert ledger.claims(_at(day), window=kwargs["window"]).valid_as_of == _at(day)
                else:
                    assert (claim.state, claim.complete_as_of, claim.valid_as_of) == (
                        State.PATCHED,
                        _at(day),
                        _at(18),
                    )
                audit_before = store.receipts(c.table)
                assert _submit(store, c, object(), receipt.manifest) == replace(receipt, replayed=True)
                assert store.receipts(c.table) == audit_before
                assert sum(r.accepted for r in store.receipts(c.table)) == day - 17
            surface = Claims.combine(*(store.load_ledger(c).claims(_at(day)) for c in contracts))
            assert surface.state is (State.COMPLETE if day in (18, 25) else State.PATCHED)
            assert surface.valid_as_of == _at(day if day in (18, 25) else 18)
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
            "retyped_column",
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
        source_schema = c.schema
        if problem == "missing_column":
            source_schema = c.schema[:-1]
            rows = [{col.name: row[col.name] for col in source_schema} for row in rows]
        elif problem == "retyped_column":
            rows[0]["id"] = "not_an_integer"
        elif problem == "shape":
            shape = "not_admitted"
        manifest = Manifest(
            c.table,
            shape,
            _at(17) if problem == "time_backwards" else _at(19),
            rows=len(rows) + (problem == "count"),
            schema_hash="wrong" if problem == "schema" else c.schema_hash,
            **kwargs,
        )
        before = self._rows(factory, store, c.table)
        refused = _submit(store, c, factory.write_source(rows, source_schema), manifest)
        assert not refused.accepted
        assert refused.publication_id == first.publication_id
        assert self._rows(factory, store, c.table) == before
        assert store.load_ledger(c) == first.ledger
        assert store.receipts(c.table) == (first, refused)
        self._fold(store, c)

    @pytest.mark.parametrize("shape", ["full", "daily", "since", "feed"])
    def test_failure_after_rows_rolls_back_or_converges_on_the_same_idempotent_delivery(
        self, factory: StoreFactory, store: Store, shape: str
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1), _row(2)], generation="g1")
        choices: dict[str, dict[str, Any]] = {
            "full": {},
            "daily": {"window": (_at(18).date(), _at(19).date())},
            "since": {"since": _at(18)},
            "feed": {"since": "g1", "generation": "g2", "keys": ((2,),), "tombstones": ((1,),)},
        }
        kwargs = choices[shape]
        rows = [_row(2, value="new")]
        manifest = Manifest(c.table, shape, _at(19), rows=1, schema_hash=c.schema_hash, **kwargs)
        source = factory.write_source(rows, c.schema)
        with factory.fail_after_rows(store):
            try:
                failed = _submit(store, c, source, manifest)
                assert not failed.accepted
            except StoreError:
                pass
        assert store.load_ledger(c) == first.ledger
        assert self._rows(factory, store, c.table) == [_row(1), _row(2)]
        assert not any(r.accepted and r.manifest == manifest for r in store.receipts(c.table))
        retried = _submit(store, c, source, manifest)
        assert retried.accepted, retried.checks
        assert self._rows(factory, store, c.table) == ([_row(1), *rows] if shape == "since" else rows)
        assert _submit(store, c, object(), manifest) == replace(retried, replayed=True)
        self._fold(store, c)

    def test_replay_after_a_newer_delivery_returns_the_original_receipt_once(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        second = self._deliver(factory, store, c, [_row(2)], day=19)
        assert _submit(store, c, object(), first.manifest) == replace(first, replayed=True)
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
        c = Contract("events", (), {"log": SINCE}, schema=(Column("event", "str"),), start_at=_at(1))
        source = factory.write_source([{"event": "one"}], c.schema)
        manifest = Manifest(c.table, "log", _at(18), since=_at(1), rows=1, schema_hash=c.schema_hash)
        first = _submit(store, c, source, manifest)
        assert first.accepted
        assert _submit(store, c, source, manifest) == replace(first, replayed=True)
        assert factory.read_table(store, c.table) == [{"event": "one"}]
        self._fold(store, c)
        source = factory.write_source([{"event": "two"}], c.schema)
        second_manifest = replace(manifest, produced_at=_at(19), since=_at(18))
        with factory.fail_after_rows(store):
            try:
                failed = _submit(store, c, source, second_manifest)
                assert not failed.accepted
            except StoreError:
                pass
        assert store.load_ledger(c) == first.ledger
        assert factory.read_table(store, c.table) == [{"event": "one"}]
        self._fold(store, c)
        second = _submit(store, c, source, second_manifest)
        assert second.accepted
        assert _submit(store, c, object(), second_manifest) == replace(second, replayed=True)
        gap = _submit(store, c, source, replace(second_manifest, produced_at=_at(20), since=_at(17)))
        assert not gap.accepted
        assert factory.read_table(store, c.table) == [{"event": "one"}, {"event": "two"}]
        self._fold(store, c)

    def test_stamp_ahead_refuses_until_receipts_repair_the_lost_ledger(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        second = self._deliver(factory, store, c, [_row(2)], day=19)
        factory.set_ledger(store, first.ledger)
        with pytest.raises(InconsistentPublication, match="stamp ahead"):
            store.load_ledger(c)
        with pytest.raises(InconsistentPublication):
            _submit(store, c, object(), second.manifest)
        assert store.repair(c) == second.ledger
        assert _submit(store, c, object(), second.manifest) == replace(second, replayed=True)
        assert self._rows(factory, store, c.table) == [_row(2)]
        self._fold(store, c)

    def test_ledger_ahead_requires_a_new_complete_delivery(self, factory: StoreFactory, store: Store) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        second = self._deliver(factory, store, c, [_row(2)], day=19)
        factory.set_stamp(store, c.table, first.publication_id)
        with pytest.raises(InconsistentPublication, match="ledger ahead"):
            store.load_ledger(c)
        with pytest.raises(InconsistentPublication):
            store.repair(c)
        with pytest.raises(InconsistentPublication):
            _submit(store, c, object(), second.manifest)
        with pytest.raises(InconsistentPublication):
            self._deliver(factory, store, c, [_row(2)], "daily", 20, window=(_at(18).date(), _at(19).date()))
        assert self._deliver(factory, store, c, [_row(3)], day=20).accepted
        assert self._rows(factory, store, c.table) == [_row(3)]

    def test_a_generation_break_needs_a_full_export_before_the_feed_can_continue(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        self._deliver(factory, store, c, [_row(1)], generation="g41")
        refused = self._deliver(
            factory, store, c, [_row(2)], "feed", 19, since="restored", generation="g2", keys=((2,),)
        )
        assert not refused.accepted
        assert self._rows(factory, store, c.table) == [_row(1)]
        assert self._deliver(factory, store, c, [_row(2)], day=20, generation="new").accepted
        assert self._deliver(
            factory,
            store,
            c,
            [_row(3)],
            "feed",
            21,
            since="new",
            generation="next",
            keys=((3,),),
            tombstones=((2,),),
        ).accepted
        assert self._rows(factory, store, c.table) == [_row(3)]

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
        feed = Shape(Coverage.WINDOW, Completeness.CHANGED_ONLY, Deletions.TOMBSTONE, Checkpoint.TIMESTAMP)
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
            tombstones=((1,),),
        )
        assert not refused.accepted
        assert store.load_ledger(c) == first.ledger
        assert self._rows(factory, store, c.table) == [_row(1), _row(2, 19)]

    @pytest.mark.parametrize("keys,tombstones", [(((3,),), ()), ((), ((2,),))])
    def test_live_key_enumeration_cannot_hide_or_delete_an_incoming_row(
        self,
        factory: StoreFactory,
        store: Store,
        keys: tuple[tuple[int, ...], ...],
        tombstones: tuple[tuple[int, ...], ...],
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)], generation="g1")
        refused = self._deliver(
            factory,
            store,
            c,
            [_row(2)],
            "feed",
            19,
            since="g1",
            generation="g2",
            keys=keys,
            tombstones=tombstones,
        )
        assert not refused.accepted
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
        assert receipt.ledger.claims(_at(19), window=(_at(18).date(), _at(18).date())).valid_as_of == _at(19)

    def test_a_foreign_manifest_is_audited_under_the_receiving_contract(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        manifest = Manifest("other", "full", _at(18), rows=0, schema_hash=c.schema_hash)
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
        c = Contract(
            "plants",
            ("id",),
            {"plant": WINDOW},
            "plant",
            (Column("id", "int"), Column("plant", "str")),
            max_shrink_fraction=None,
        )
        for day, name, rows in (
            (18, "IT01", [{"id": 1, "plant": "IT01"}]),
            (19, "DE01", [{"id": 2, "plant": "DE01"}]),
            (20, "IT01", []),
        ):
            assert self._deliver(factory, store, c, rows, "plant", day, window=(name, name)).accepted
        assert factory.read_table(store, c.table) == [{"id": 2, "plant": "DE01"}]
        ledger = store.load_ledger(c)
        assert ledger.claims(_at(20), window=("IT01", "IT01")).valid_as_of == _at(20)
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
                    c.table,
                    request.shape,
                    request.until,
                    since=request.since,
                    rows=len(rows),
                    schema_hash=c.schema_hash,
                )
                pulled = _submit(pull, c, factory.write_source(rows, c.schema), manifest)
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
        c = Contract(
            "updates",
            ("id",),
            {"full": FULL, "since": SINCE},
            schema=(Column("id", "int"), Column("updated_at", "datetime"), Column("value", "str")),
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
            receipt = self._deliver(factory, store, c, rows, shape, day, since=request.since)
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
        assert receipt.claims.valid_as_of == _at(18)

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
                c.table, "full", _at(19), rows=1, schema_hash=c.schema_hash, content_hash=str(key)
            )
            candidate = store.hold(factory.write_source([_row(key)], c.schema), manifest, c)
            checks = store.check(candidate, c, manifest, Gate.requirements(c, manifest))
            receipt = Gate.evaluate(base, manifest, checks, now=_at(30))
            prepared.append((candidate, receipt))
        winner, receipt = prepared[0]
        loser, stale = prepared[1]
        try:
            assert receipt.transition is not None and stale.transition is not None
            store.execute(winner, receipt.transition.plan)
            store.publish(winner, receipt)
            with pytest.raises(StoreError, match="base|publication|changed"):
                store.execute(loser, stale.transition.plan)
                store.publish(loser, stale)
        finally:
            store.abort(winner)
            store.abort(loser)
        assert store.load_ledger(c) == receipt.ledger
        assert self._rows(factory, store, c.table) == [_row(2)]
        assert store.receipts(c.table) == (first, receipt)

    def test_lost_ledger_with_retained_receipts_requires_repair(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        receipt = self._deliver(factory, store, c, [_row(1)])
        factory.remove_ledger(store, c.table)
        with pytest.raises(InconsistentPublication):
            _submit(store, c, object(), receipt.manifest)
        assert store.repair(c) == receipt.ledger
        assert _submit(store, c, object(), receipt.manifest) == replace(receipt, replayed=True)

    def test_corrected_content_hash_is_a_new_delivery_subject_to_time_admission(
        self, factory: StoreFactory, store: Store
    ) -> None:
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)], content_hash="original")
        corrected = replace(first.manifest, content_hash="corrected")
        refused = _submit(store, c, factory.write_source([_row(2)], c.schema), corrected)
        assert not refused.accepted and not refused.replayed
        assert corrected.identity != first.manifest.identity
        accepted = _submit(
            store, c, factory.write_source([_row(2)], c.schema), replace(corrected, produced_at=_at(19))
        )
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
        manifest = replace(first.manifest, produced_at=_at(19), schema_hash="wrong")
        refused = _submit(store, c, factory.write_source([_row(1)], c.schema), manifest)
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
        source = factory.write_source([_row(2)], c.schema)
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
