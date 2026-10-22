"""Inherit StoreConformance in pytest and supply a factory fixture."""

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

import pytest

from dogana import (
    Claims,
    Column,
    Contract,
    InconsistentPublication,
    InvalidContract,
    Ledger,
    Manifest,
    Receipt,
    Shape,
    State,
    next_extraction,
)

FULL = Shape("all", "complete", "absence", "none")
WINDOW = Shape("window", "complete", "absence", "none")
SINCE = Shape("all", "changed_only", "none", "timestamp")
FEED = Shape("keys", "changed_only", "tombstone", "generation")
SCHEMA = (Column("id", "int"), Column("day", "date"), Column("value", "str"))


class DeliveryStore(Protocol):
    """The suite requires a delivery facade, not candidate staging methods."""

    def deliver(self, contract: Contract, source: object, manifest: Manifest) -> Receipt: ...
    def load_ledger(self, contract: Contract) -> Ledger: ...
    def receipts(self, table: str | None = None) -> tuple[Receipt, ...]: ...
    def repair(self, contract: Contract) -> Ledger: ...


class StoreFactory(Protocol):
    """Transport and deliberate persistence faults supplied by the store's tests."""

    atomic_append: bool

    def make_store(self) -> DeliveryStore: ...
    def write_source(self, rows: Sequence[Mapping], schema: tuple[Column, ...]) -> object: ...
    def read_table(self, store: DeliveryStore, table: str) -> list[dict]: ...
    def fail_after_rows(self, store: DeliveryStore) -> AbstractContextManager[None]: ...
    def set_ledger(self, store: DeliveryStore, ledger: Ledger) -> None: ...
    def set_stamp(self, store: DeliveryStore, table: str, publication_id: str | None) -> None: ...


def _at(day):
    return datetime(2026, 10, day, tzinfo=UTC)


def _contract(table="moves", shapes=None, **kwargs):
    shapes = shapes or {"full": FULL, "daily": WINDOW, "since": SINCE, "feed": FEED}
    window_column = "day" if any(s.coverage == "window" for s in shapes.values()) else None
    return Contract(table, ("id",), shapes, window_column, SCHEMA, **kwargs)


def _row(key, day=18, value="current"):
    return {"id": key, "day": _at(day).date(), "value": value}


class StoreConformance:
    """All four obligations, including recovery, for transactional or ordered stores."""

    @pytest.fixture
    def store(self, factory: StoreFactory):
        store = factory.make_store()
        yield store
        close = getattr(store, "close", None)
        if close is not None:
            close()

    def _deliver(self, factory, store, contract, rows, shape="full", day=18, **kwargs):
        manifest = Manifest(
            contract.table, shape, _at(day), rows=len(rows), schema_hash=contract.schema_hash, **kwargs
        )
        receipt = store.deliver(contract, factory.write_source(rows, contract.schema), manifest)
        self._fold(store, contract)
        return receipt

    def _fold(self, store, contract):
        receipts = store.receipts(contract.table)
        assert Ledger.from_receipts(contract, receipts) == store.load_ledger(contract)
        assert all(Receipt.from_json(r.to_json()) == r for r in receipts)

    def _rows(self, factory, store, table):
        return sorted(factory.read_table(store, table), key=lambda r: tuple(r.values()))

    def test_a_week_of_erp_deliveries_proves_three_tables_rows_dates_receipts_and_claims(
        self, factory, store
    ):
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
                kwargs = {}
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
                before = store.receipts(c.table)
                assert store.deliver(c, object(), receipt.manifest) == receipt
                assert store.receipts(c.table) == before
                assert len(ledger.accepted) == day - 17
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
    def test_a_contradictory_delivery_keeps_the_previous_publication(self, factory, store, problem):
        c = _contract(max_shrink_fraction=0.25)
        first = self._deliver(factory, store, c, [_row(i) for i in range(4)])
        rows, kwargs, shape = [_row(i) for i in range(4)], {}, "full"
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
        refused = store.deliver(c, factory.write_source(rows, source_schema), manifest)
        assert not refused.accepted
        assert refused.publication_id == first.publication_id
        assert self._rows(factory, store, c.table) == before
        assert store.load_ledger(c) == first.ledger
        assert store.receipts(c.table) == (first, refused)
        self._fold(store, c)

    @pytest.mark.parametrize("shape", ["full", "daily", "since", "feed"])
    def test_failure_after_rows_rolls_back_or_converges_on_the_same_idempotent_delivery(
        self, factory, store, shape
    ):
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1), _row(2)], generation="g1")
        kwargs = {
            "full": {},
            "daily": {"window": (_at(18).date(), _at(19).date())},
            "since": {"since": _at(18)},
            "feed": {"since": "g1", "generation": "g2", "keys": ((2,),), "tombstones": ((1,),)},
        }[shape]
        rows = [_row(2, value="new")]
        manifest = Manifest(c.table, shape, _at(19), rows=1, schema_hash=c.schema_hash, **kwargs)
        source = factory.write_source(rows, c.schema)
        with factory.fail_after_rows(store):
            try:
                failed = store.deliver(c, source, manifest)
                assert not failed.accepted
            except OSError:
                pass
        if factory.atomic_append:
            assert store.load_ledger(c) == first.ledger
            assert self._rows(factory, store, c.table) == [_row(1), _row(2)]
            assert not any(r.accepted and r.manifest == manifest for r in store.receipts(c.table))
        else:
            with pytest.raises(InconsistentPublication):
                store.load_ledger(c)
            with pytest.raises(InconsistentPublication):
                store.deliver(
                    c, source, replace(manifest, produced_at=manifest.produced_at + timedelta(days=1))
                )
        retried = store.deliver(c, source, manifest)
        assert retried.accepted, retried.checks
        assert self._rows(factory, store, c.table) == ([_row(1), *rows] if shape == "since" else rows)
        assert store.deliver(c, object(), manifest) == retried
        self._fold(store, c)

    def test_replay_after_a_newer_delivery_returns_the_original_receipt_once(self, factory, store):
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        second = self._deliver(factory, store, c, [_row(2)], day=19)
        assert store.deliver(c, object(), first.manifest) == first
        assert store.load_ledger(c) == second.ledger
        assert self._rows(factory, store, c.table) == [_row(2)]
        assert store.receipts(c.table) == (first, second)
        self._fold(store, c)

    def test_an_out_of_order_overlapping_window_is_refused_whole(self, factory, store):
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

    def test_append_requires_an_atomic_commit_and_replay_never_duplicates_events(self, factory, store):
        c = Contract("events", (), {"log": SINCE}, schema=(Column("event", "str"),), start_at=_at(1))
        source = factory.write_source([{"event": "one"}], c.schema)
        manifest = Manifest(c.table, "log", _at(18), since=_at(1), rows=1, schema_hash=c.schema_hash)
        if not factory.atomic_append:
            with pytest.raises(InvalidContract, match="append"):
                store.deliver(c, source, manifest)
            assert not store.receipts(c.table)
            return
        first = store.deliver(c, source, manifest)
        assert first.accepted
        assert store.deliver(c, source, manifest) == first
        assert factory.read_table(store, c.table) == [{"event": "one"}]
        self._fold(store, c)
        source = factory.write_source([{"event": "two"}], c.schema)
        second_manifest = replace(manifest, produced_at=_at(19), since=_at(18))
        with factory.fail_after_rows(store):
            try:
                failed = store.deliver(c, source, second_manifest)
                assert not failed.accepted
            except OSError:
                pass
        assert store.load_ledger(c) == first.ledger
        assert factory.read_table(store, c.table) == [{"event": "one"}]
        self._fold(store, c)
        second = store.deliver(c, source, second_manifest)
        assert second.accepted
        assert store.deliver(c, object(), second_manifest) == second
        gap = store.deliver(c, source, replace(second_manifest, produced_at=_at(20), since=_at(17)))
        assert not gap.accepted
        assert factory.read_table(store, c.table) == [{"event": "one"}, {"event": "two"}]
        self._fold(store, c)

    def test_stamp_ahead_refuses_until_receipts_repair_the_lost_ledger(self, factory, store):
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        second = self._deliver(factory, store, c, [_row(2)], day=19)
        factory.set_ledger(store, first.ledger)
        with pytest.raises(InconsistentPublication, match="stamp ahead"):
            store.load_ledger(c)
        with pytest.raises(InconsistentPublication):
            store.deliver(c, object(), second.manifest)
        assert store.repair(c) == second.ledger
        assert store.deliver(c, object(), second.manifest) == second
        assert self._rows(factory, store, c.table) == [_row(2)]
        self._fold(store, c)

    def test_ledger_ahead_requires_a_new_complete_delivery(self, factory, store):
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        second = self._deliver(factory, store, c, [_row(2)], day=19)
        factory.set_stamp(store, c.table, first.publication_id)
        with pytest.raises(InconsistentPublication, match="ledger ahead"):
            store.load_ledger(c)
        with pytest.raises(InconsistentPublication):
            store.repair(c)
        with pytest.raises(InconsistentPublication):
            store.deliver(c, object(), second.manifest)
        with pytest.raises(InconsistentPublication):
            self._deliver(factory, store, c, [_row(2)], "daily", 20, window=(_at(18).date(), _at(19).date()))
        assert self._deliver(factory, store, c, [_row(3)], day=20).accepted
        assert self._rows(factory, store, c.table) == [_row(3)]

    def test_a_generation_break_needs_a_full_export_before_the_feed_can_continue(self, factory, store):
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

    def test_a_window_cannot_move_a_key_but_a_full_export_can(self, factory, store):
        c = _contract()
        first = self._deliver(factory, store, c, [_row(1)])
        refused = self._deliver(
            factory, store, c, [_row(1, 19)], "daily", 19, window=(_at(19).date(), _at(19).date())
        )
        assert not refused.accepted
        assert store.load_ledger(c) == first.ledger
        assert self._deliver(factory, store, c, [_row(1, 19)], day=19).accepted

    def test_a_window_tombstone_cannot_delete_a_key_outside_its_coverage(self, factory, store):
        feed = Shape("window", "changed_only", "tombstone", "generation")
        c = _contract(shapes={"full": FULL, "feed_window": feed})
        first = self._deliver(factory, store, c, [_row(1), _row(2, 19)], generation="g1")
        refused = self._deliver(
            factory,
            store,
            c,
            [],
            "feed_window",
            20,
            since="g1",
            generation="g2",
            window=(_at(19).date(), _at(19).date()),
            tombstones=((1,),),
        )
        assert not refused.accepted
        assert store.load_ledger(c) == first.ledger
        assert self._rows(factory, store, c.table) == [_row(1), _row(2, 19)]

    @pytest.mark.parametrize("keys,tombstones", [(((3,),), ()), ((), ((2,),))])
    def test_live_key_enumeration_cannot_hide_or_delete_an_incoming_row(
        self, factory, store, keys, tombstones
    ):
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

    def test_an_empty_partition_decommissions_it_without_erasing_unknown_history(self, factory, store):
        c = _contract()
        self._deliver(factory, store, c, [_row(1)], "daily", window=(_at(18).date(), _at(18).date()))
        receipt = self._deliver(factory, store, c, [], "daily", 19, window=(_at(18).date(), _at(18).date()))
        assert receipt.accepted
        assert self._rows(factory, store, c.table) == []
        assert receipt.ledger.claims(_at(19)).state is State.UNKNOWN
        assert receipt.ledger.claims(_at(19), window=(_at(18).date(), _at(18).date())).valid_as_of == _at(19)

    def test_a_foreign_manifest_is_audited_under_the_receiving_contract(self, factory, store):
        c = _contract()
        manifest = Manifest("other", "full", _at(18), rows=0, schema_hash=c.schema_hash)
        receipt = store.deliver(c, object(), manifest)
        assert not receipt.accepted
        assert store.receipts(c.table) == (receipt,)
        self._fold(store, c)

    @pytest.mark.parametrize("remaining,accepted", [(3, True), (2, False)])
    def test_a_complete_export_respects_the_exact_shrink_fraction(self, factory, store, remaining, accepted):
        c = _contract(max_shrink_fraction=0.7)
        first = self._deliver(factory, store, c, [_row(i) for i in range(10)])
        receipt = self._deliver(factory, store, c, [_row(i) for i in range(remaining)], day=19)
        assert receipt.accepted is accepted
        assert len(factory.read_table(store, c.table)) == (remaining if accepted else 10)
        assert store.load_ledger(c) == (receipt.ledger if accepted else first.ledger)

    def test_empty_named_plant_exports_preserve_other_plants_and_unknown_history(self, factory, store):
        c = Contract(
            "plants", ("id",), {"plant": WINDOW}, "plant", (Column("id", "int"), Column("plant", "str"))
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

    def test_push_and_pull_publish_the_same_rows_ledger_and_receipt(self, factory, store):
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
                pulled = pull.deliver(c, factory.write_source(rows, c.schema), manifest)
                assert pushed == pulled
                assert self._rows(factory, store, c.table) == self._rows(factory, pull, c.table)
                assert store.load_ledger(c) == pull.load_ledger(c)
                self._fold(pull, c)
        finally:
            close = getattr(pull, "close", None)
            if close is not None:
                close()

    def test_pulling_changed_rows_by_updated_at_keeps_unreported_deletions(self, factory, store):
        c = Contract(
            "updates",
            ("id",),
            {"full": FULL, "since": SINCE},
            schema=(Column("id", "int"), Column("updated_at", "datetime"), Column("value", "str")),
        )
        source = [
            {"id": 1, "updated_at": _at(17), "value": "old"},
            {"id": 2, "updated_at": _at(18), "value": "initial"},
        ]
        for day, shape in ((18, "full"), (19, "since")):
            request = next_extraction(store.load_ledger(c), shape, _at(day))
            rows = (
                source
                if shape == "full"
                else [r for r in source if request.since <= r["updated_at"] <= request.until]
            )
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
