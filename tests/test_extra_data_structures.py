"""Roadmap §3.2 — reactive data structures."""

from __future__ import annotations

import time

from graphrefly.core.protocol import MessageType
from graphrefly.extra.data_structures import (
    log_slice,
    pubsub,
    reactive_index,
    reactive_list,
    reactive_log,
    reactive_map,
)


def test_reactive_map_set_and_data() -> None:
    m = reactive_map()
    m.set("a", 1)
    assert m.entries.get() == {"a": 1}
    m.delete("a")
    assert m.entries.get() == {}


def test_reactive_map_ttl_and_prune() -> None:
    m = reactive_map(default_ttl=0.05)
    m.set("k", "v")
    assert m.entries.get() == {"k": "v"}
    time.sleep(0.08)
    # data still shows stale snapshot until prune
    m.prune_expired()
    assert m.entries.get() == {}


def test_reactive_map_max_size_lru() -> None:
    m = reactive_map(max_size=2)
    m.set("a", 1)
    m.set("b", 2)
    m.set("c", 3)
    assert m.entries.get() == {"b": 2, "c": 3}


def test_reactive_map_immutable_snapshot() -> None:
    """data.get() should be a MappingProxyType (immutable)."""
    m = reactive_map()
    m.set("x", 1)
    snap = m.entries.get()
    # Should not support mutation
    try:
        snap["y"] = 2  # type: ignore[index]
        raise AssertionError("Expected TypeError on immutable mapping")
    except TypeError:
        pass


def test_reactive_map_identity_equals() -> None:
    """Each mutation produces a new MappingProxyType identity (identity equality)."""
    m = reactive_map()
    m.set("a", 1)
    v1 = m.entries.get()
    m.set("a", 1)  # same value — mutation still produces new snapshot
    v2 = m.entries.get()
    # Each push creates a new snapshot object — identity differs
    assert v1 is not v2
    # But content is the same
    assert dict(v1) == dict(v2)


def test_reactive_map_name() -> None:
    """name param is accepted (for describe() integration)."""
    m = reactive_map(name="my-cache")
    m.set("k", 1)
    assert m.entries.get() == {"k": 1}


def test_reactive_log_append_tail() -> None:
    lg = reactive_log()
    assert lg.entries.get() == ()
    lg.append(1)
    lg.append(2)
    assert lg.entries.get() == (1, 2)
    tail = lg.tail(1)
    assert tail.get() == (2,)


def test_log_slice() -> None:
    lg = reactive_log([0, 1, 2, 3])
    sl = log_slice(lg, 1, 3)
    assert sl.get() == (1, 2)


def test_reactive_index_order() -> None:
    idx = reactive_index()
    idx.upsert("p1", 10, "a")
    idx.upsert("p2", 5, "b")
    assert dict(idx.by_primary.get()) == {"p1": "a", "p2": "b"}
    ordered = idx.ordered.get()
    assert [r.primary for r in ordered] == ["p2", "p1"]
    idx.delete("p2")
    assert list(idx.by_primary.get().keys()) == ["p1"]


def test_reactive_list_ops() -> None:
    lst = reactive_list()
    lst.append(1)
    lst.insert(0, 0)
    assert lst.items.get() == (0, 1)
    assert lst.pop() == 1
    assert lst.items.get() == (0,)


def test_pubsub_lazy_topic() -> None:
    hub = pubsub()
    t = hub.topic("x")
    seen: list[object] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA:
                seen.append(m[1])

    unsub = t.subscribe(sink)
    hub.publish("x", 42)
    assert seen == [42]
    unsub()


def test_pubsub_two_phase() -> None:
    """publish should emit DIRTY then DATA (two-phase protocol)."""
    hub = pubsub()
    t = hub.topic("y")
    messages: list[object] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            messages.append(m[0])

    t.subscribe(sink)
    messages.clear()
    hub.publish("y", 99)
    assert MessageType.DIRTY in messages
    assert MessageType.DATA in messages


def test_reactive_map_get_refreshes_lru() -> None:
    """get() should refresh LRU order so accessed keys survive eviction."""
    m = reactive_map(max_size=2)
    m.set("a", 1)
    m.set("b", 2)
    # Access "a" via get() — should move it to end of LRU
    assert m.get("a") == 1
    # Setting "c" should evict "b" (oldest untouched), not "a"
    m.set("c", 3)
    snap = m.entries.get()
    assert "a" in snap
    assert "c" in snap
    assert "b" not in snap
