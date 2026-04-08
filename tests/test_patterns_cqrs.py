"""Tests for CQRS patterns (roadmap §4.5)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from graphrefly.core.guard import GuardDenied
from graphrefly.patterns.cqrs import (
    CqrsEvent,
    CqrsGraph,
    EventStoreAdapter,
    EventStoreCursor,
    LoadEventsResult,
    MemoryEventStore,
    cqrs,
)


def _snap_entries(node_val: object) -> tuple[CqrsEvent, ...]:
    """Extract entries from a log snapshot (now a plain tuple)."""
    if isinstance(node_val, tuple):
        return node_val
    return ()


# -- Factory -----------------------------------------------------------------


def test_cqrs_returns_cqrs_graph() -> None:
    app = cqrs("test")
    assert isinstance(app, CqrsGraph)
    app.destroy()


# -- Events ------------------------------------------------------------------


def test_event_registers_observable_stream() -> None:
    app = cqrs("test")
    evt_node = app.event("order_placed")
    assert evt_node is not None
    entries = _snap_entries(evt_node.get())
    assert entries == ()
    app.destroy()


def test_event_is_idempotent() -> None:
    app = cqrs("test")
    a = app.event("order_placed")
    b = app.event("order_placed")
    assert a is b
    app.destroy()


def test_event_guard_denies_external_write() -> None:
    app = cqrs("test")
    app.event("order_placed")
    evt_node = app.resolve("order_placed")
    with pytest.raises(GuardDenied):
        evt_node.down([("DATA", "bad")], actor={"type": "human", "id": "h1"})
    app.destroy()


# -- Commands ----------------------------------------------------------------


def test_command_registers_write_only_node() -> None:
    app = cqrs("test")
    app.command("place_order", lambda _p, actions: actions.emit("order_placed", {"id": "1"}))
    desc = app.describe(detail="standard")
    assert "place_order" in desc["nodes"]
    assert desc["nodes"]["place_order"]["meta"]["cqrs_type"] == "command"
    app.destroy()


def test_command_guard_denies_observe() -> None:
    app = cqrs("test")
    app.command("place_order", lambda _p, _a: None)
    cmd_node = app.resolve("place_order")
    with pytest.raises(GuardDenied):
        cmd_node.subscribe(lambda _msgs: None, actor={"type": "human", "id": "h1"})
    app.destroy()


# -- Dispatch ----------------------------------------------------------------


def test_dispatch_runs_handler_and_appends_events() -> None:
    app = cqrs("test")
    app.event("order_placed")
    app.command(
        "place_order",
        lambda payload, actions: actions.emit("order_placed", {"order_id": payload["id"]}),
    )
    app.dispatch("place_order", {"id": "order-1"})

    entries = _snap_entries(app.event("order_placed").get())
    assert len(entries) == 1
    assert entries[0].type == "order_placed"
    assert entries[0].payload == {"order_id": "order-1"}
    app.destroy()


def test_dispatch_auto_registers_events() -> None:
    app = cqrs("test")
    app.command("place_order", lambda _p, actions: actions.emit("order_placed", {"id": "1"}))
    app.dispatch("place_order", {})
    desc = app.describe(detail="standard")
    assert "order_placed" in desc["nodes"]
    assert desc["nodes"]["order_placed"]["meta"]["cqrs_type"] == "event"
    app.destroy()


def test_dispatch_throws_for_unknown_command() -> None:
    app = cqrs("test")
    with pytest.raises(ValueError, match="Unknown command"):
        app.dispatch("nonexistent", {})
    app.destroy()


def test_dispatch_sets_command_node_value() -> None:
    app = cqrs("test")
    app.command("place_order", lambda _p, _a: None)
    app.dispatch("place_order", {"id": "42"})
    assert app.get("place_order") == {"id": "42"}
    app.destroy()


def test_events_carry_timestamp_ns_and_seq() -> None:
    app = cqrs("test")
    app.event("order_placed")
    app.command("place_order", lambda payload, actions: actions.emit("order_placed", payload))
    app.dispatch("place_order", {"id": "1"})
    entries = _snap_entries(app.event("order_placed").get())
    evt = entries[0]
    assert evt.timestamp_ns > 0
    assert evt.seq == 1
    app.destroy()


def test_events_carry_v0_identity_when_event_log_is_versioned() -> None:
    app = cqrs("test")
    app.event("order_placed")
    app._event_logs["order_placed"].log.entries._apply_versioning(0)
    app.command("place_order", lambda payload, actions: actions.emit("order_placed", payload))
    app.dispatch("place_order", {"id": "1"})
    entries = _snap_entries(app.event("order_placed").get())
    evt = entries[0]
    assert evt.v0 is not None
    assert isinstance(evt.v0["id"], str)
    app.destroy()


def test_seq_increments_monotonically() -> None:
    app = cqrs("test")
    app.event("a")
    app.event("b")
    app.command("cmd", lambda _p, a: [a.emit("a", 1), a.emit("b", 2)])
    app.dispatch("cmd", {})
    entries_a = _snap_entries(app.event("a").get())
    entries_b = _snap_entries(app.event("b").get())
    assert entries_a[0].seq == 1
    assert entries_b[0].seq == 2
    app.destroy()


# -- Command handler error ---------------------------------------------------


def test_dispatch_sets_meta_error_on_handler_throw() -> None:
    err = RuntimeError("boom")

    def bad_handler(_p: object, _a: object) -> None:
        raise err

    app = cqrs("test")
    app.command("bad", bad_handler)
    with pytest.raises(RuntimeError, match="boom"):
        app.dispatch("bad", {})
    cmd_node = app.resolve("bad")
    assert cmd_node.meta["error"].get() is err
    app.destroy()


def test_dispatch_clears_meta_error_on_success() -> None:
    should_throw = [True]

    def maybe_handler(_p: object, actions: object) -> None:
        if should_throw[0]:
            raise RuntimeError("fail")

    app = cqrs("test")
    app.command("maybe", maybe_handler)
    with pytest.raises(RuntimeError, match="fail"):
        app.dispatch("maybe", {})
    cmd_node = app.resolve("maybe")
    assert isinstance(cmd_node.meta["error"].get(), RuntimeError)
    should_throw[0] = False
    app.dispatch("maybe", {})
    assert cmd_node.meta["error"].get() is None
    app.destroy()


# -- Projections -------------------------------------------------------------


def test_projection_derives_read_model() -> None:
    app = cqrs("test")
    app.event("order_placed")
    app.projection("order_count", ["order_placed"], lambda _s, events: len(events), 0)
    app.command("place_order", lambda payload, actions: actions.emit("order_placed", payload))

    assert app.get("order_count") == 0
    app.dispatch("place_order", {"id": "1"})
    assert app.get("order_count") == 1
    app.dispatch("place_order", {"id": "2"})
    assert app.get("order_count") == 2
    app.destroy()


def test_projection_from_multiple_event_streams() -> None:
    app = cqrs("test")
    app.event("order_placed")
    app.event("order_cancelled")

    def reducer(_state: dict[str, int], events: list[CqrsEvent]) -> dict[str, int]:
        return {
            "placed": sum(1 for e in events if e.type == "order_placed"),
            "cancelled": sum(1 for e in events if e.type == "order_cancelled"),
        }

    app.projection(
        "summary", ["order_placed", "order_cancelled"], reducer, {"placed": 0, "cancelled": 0}
    )
    app.command("place_order", lambda _p, a: a.emit("order_placed", {}))
    app.command("cancel_order", lambda _p, a: a.emit("order_cancelled", {}))

    app.dispatch("place_order", {})
    app.dispatch("place_order", {})
    app.dispatch("cancel_order", {})

    summary = app.get("summary")
    assert summary["placed"] == 2
    assert summary["cancelled"] == 1
    app.destroy()


def test_projection_guard_denies_write() -> None:
    app = cqrs("test")
    app.event("order_placed")
    app.projection("order_count", ["order_placed"], lambda _s, e: len(e), 0)
    proj_node = app.resolve("order_count")
    with pytest.raises(GuardDenied):
        proj_node.down([("DATA", 999)], actor={"type": "human", "id": "h1"})
    app.destroy()


# -- Sagas -------------------------------------------------------------------


def test_saga_runs_handler_on_events() -> None:
    app = cqrs("test")
    app.event("order_placed")

    saga_log: list[CqrsEvent] = []
    app.saga("notify_shipping", ["order_placed"], lambda evt: saga_log.append(evt))

    app.command("place_order", lambda payload, actions: actions.emit("order_placed", payload))
    app.dispatch("place_order", {"id": "1"})

    assert len(saga_log) >= 1
    assert saga_log[0].type == "order_placed"
    app.destroy()


def test_saga_sets_meta_error_and_clears_on_success() -> None:
    app = cqrs("test")
    app.event("order_placed")
    should_throw = [True]

    def saga_handler(_e: CqrsEvent) -> None:
        if should_throw[0]:
            msg = "saga boom"
            raise RuntimeError(msg)

    app.saga("side_fx", ["order_placed"], saga_handler)
    app.command("place_order", lambda payload, actions: actions.emit("order_placed", payload))
    app.dispatch("place_order", {"id": "1"})
    saga_node = app.resolve("side_fx")
    assert isinstance(saga_node.meta["error"].get(), RuntimeError)
    should_throw[0] = False
    app.dispatch("place_order", {"id": "2"})
    assert saga_node.meta["error"].get() is None
    app.destroy()


def test_saga_only_processes_new_events() -> None:
    app = cqrs("test")
    app.event("order_placed")

    saga_log: list[CqrsEvent] = []
    app.saga("notify_shipping", ["order_placed"], lambda evt: saga_log.append(evt))

    app.command("place_order", lambda payload, actions: actions.emit("order_placed", payload))
    app.dispatch("place_order", {"id": "1"})
    count_after_first = len(saga_log)

    app.dispatch("place_order", {"id": "2"})
    new_events = saga_log[count_after_first:]
    assert len(new_events) == 1
    assert new_events[0].payload == {"id": "2"}
    app.destroy()


# -- describe() --------------------------------------------------------------


def test_describe_distinguishes_cqrs_roles() -> None:
    app = cqrs("test")
    app.event("order_placed")
    app.command("place_order", lambda _p, _a: None)
    app.projection("order_count", ["order_placed"], lambda _s, e: len(e), 0)
    app.saga("notify_shipping", ["order_placed"], lambda _e: None)

    desc = app.describe(detail="standard")
    assert desc["nodes"]["place_order"]["meta"]["cqrs_type"] == "command"
    assert desc["nodes"]["order_placed"]["meta"]["cqrs_type"] == "event"
    assert desc["nodes"]["order_count"]["meta"]["cqrs_type"] == "projection"
    assert desc["nodes"]["notify_shipping"]["meta"]["cqrs_type"] == "saga"
    app.destroy()


def test_describe_shows_edges() -> None:
    app = cqrs("test")
    app.event("order_placed")
    app.projection("order_count", ["order_placed"], lambda _s, e: len(e), 0)
    app.saga("notify_shipping", ["order_placed"], lambda _e: None)

    desc = app.describe()
    edge_pairs = [f"{e['from']}->{e['to']}" for e in desc["edges"]]
    assert "order_placed->order_count" in edge_pairs
    assert "order_placed->notify_shipping" in edge_pairs
    app.destroy()


# -- Event store -------------------------------------------------------------


def test_dispatch_calls_sync_persist() -> None:
    """Adapters with sync ``persist`` are invoked on the dispatch path."""

    class SyncStore(EventStoreAdapter):
        def __init__(self) -> None:
            self.called: list[CqrsEvent] = []

        def persist(self, event: CqrsEvent) -> None:
            self.called.append(event)

        async def load_events(
            self, event_type: str, cursor: dict[str, Any] | None = None
        ) -> LoadEventsResult:
            return LoadEventsResult()

    store = SyncStore()
    app = cqrs("test")
    app.use_event_store(store)
    app.command("c", lambda _p, a: a.emit("e", {}))
    app.dispatch("c", {})
    entries = _snap_entries(app.event("e").get())
    assert len(entries) == 1
    assert len(store.called) == 1
    assert store.called[0].type == "e"
    app.destroy()


def test_use_event_store_persists_events() -> None:
    store = MemoryEventStore()
    app = cqrs("test")
    app.use_event_store(store)
    app.event("order_placed")
    app.command("place_order", lambda payload, actions: actions.emit("order_placed", payload))

    app.dispatch("place_order", {"id": "1"})
    app.dispatch("place_order", {"id": "2"})

    result = asyncio.run(store.load_events("order_placed"))
    assert len(result.events) == 2
    assert result.events[0].payload == {"id": "1"}
    app.destroy()


def test_rebuild_projection() -> None:
    store = MemoryEventStore()
    app = cqrs("test")
    app.use_event_store(store)
    app.event("order_placed")
    app.command("place_order", lambda payload, actions: actions.emit("order_placed", payload))

    app.dispatch("place_order", {"id": "1"})
    app.dispatch("place_order", {"id": "2"})

    rebuilt = asyncio.run(
        app.rebuild_projection(["order_placed"], lambda _s, events: len(events), 0)
    )
    assert rebuilt == 2
    app.destroy()


def test_rebuild_projection_throws_without_store() -> None:
    app = cqrs("test")
    with pytest.raises(RuntimeError, match="No event store"):
        asyncio.run(app.rebuild_projection(["y"], lambda _s, e: len(e), 0))
    app.destroy()


# -- MemoryEventStore --------------------------------------------------------


def test_memory_event_store_since_filter() -> None:
    store = MemoryEventStore()
    t1 = 1_000_000_000_000
    t2 = 2_000_000_000_000
    store.persist(CqrsEvent(type="a", payload=1, timestamp_ns=t1, seq=1))
    store.persist(CqrsEvent(type="a", payload=2, timestamp_ns=t2, seq=2))

    all_result = asyncio.run(store.load_events("a"))
    assert len(all_result.events) == 2

    cursor = EventStoreCursor(timestamp_ns=t1, seq=1)
    recent_result = asyncio.run(store.load_events("a", cursor=cursor))
    assert len(recent_result.events) == 1
    assert recent_result.events[0].payload == 2


def test_memory_event_store_clear() -> None:
    store = MemoryEventStore()
    store.persist(CqrsEvent(type="a", payload=1, timestamp_ns=0, seq=1))
    store.clear()
    assert asyncio.run(store.load_events("a")).events == []
