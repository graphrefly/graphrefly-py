"""Node primitive and meta — parity with graphrefly-ts ``src/__tests__/core/node.test.ts``."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from graphrefly.core import (
    MessageType,
    NodeActions,
    batch,
    describe_node,
    emit_with_batch,
    meta_snapshot,
    node,
)
from graphrefly.core.node import NodeImpl


def test_node_factory_overloads_and_get_status() -> None:
    """``node(deps?, fn?, opts?)`` and ``.get()`` / ``.status`` on source and derived."""
    src = node(initial=7)
    assert src.get() == 7
    assert src.status == "settled"

    d = node([src], lambda deps, _: deps[0] * 2)
    assert d.get() is None
    unsub = d.subscribe(lambda _m: None)
    assert d.get() == 14
    assert d.status in ("settled", "resolved")
    src.down([(MessageType.DATA, 10)])
    assert d.get() == 20
    unsub()


def test_derived_compute_not_run_until_subscribe() -> None:
    """Upstream is not wired until the first ``subscribe`` (lazy connect)."""
    a = node(initial=0)
    runs = 0

    def fn(deps: list, _a: object) -> int:
        nonlocal runs
        runs += 1
        return deps[0] + 1

    b = node([a], fn)
    assert runs == 0
    a.down([(MessageType.DIRTY,), (MessageType.DATA, 5)])
    assert runs == 0
    assert b.get() is None
    unsub = b.subscribe(lambda _m: None)
    assert runs >= 1
    assert b.get() == 6
    unsub()


def test_two_sinks_both_receive() -> None:
    """Multiple subscribers: output path uses a set of sinks; each receives."""
    s = node(initial=1)
    a: list[MessageType] = []
    b: list[MessageType] = []

    def sink_a(msgs: list) -> None:
        for m in msgs:
            a.append(m[0])

    def sink_b(msgs: list) -> None:
        for m in msgs:
            b.append(m[0])

    ua = s.subscribe(sink_a)
    ub = s.subscribe(sink_b)
    s.down([(MessageType.DIRTY,), (MessageType.DATA, 2)])
    ua()
    ub()

    assert a == [MessageType.DIRTY, MessageType.DATA]
    assert b == [MessageType.DIRTY, MessageType.DATA]


def test_two_phase_dirty_before_data_on_derived() -> None:
    """Phase 1 DIRTY, then phase 2 DATA (may be one or two sink batches)."""
    src = node(initial=0)
    derived = node([src], lambda deps, _: deps[0] + 1)
    batches: list[list[MessageType]] = []

    def sink(msgs: list) -> None:
        batches.append([m[0] for m in msgs])

    unsub = derived.subscribe(sink)
    src.down([(MessageType.DIRTY,), (MessageType.DATA, 3)])
    unsub()

    flat = [t for batch in batches for t in batch]
    assert flat[0] == MessageType.DIRTY
    assert MessageType.DATA in flat
    assert flat.index(MessageType.DIRTY) < flat.index(MessageType.DATA)


def test_node_unsubscribe_disconnects_upstream() -> None:
    """``Node.unsubscribe()`` drops upstream wiring; updates no longer propagate."""
    src = node(initial=0)
    seen: list[MessageType] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            seen.append(m[0])

    derived = node([src], lambda deps, _: deps[0] + 1)
    derived.subscribe(sink)
    src.down([(MessageType.DATA, 1)])
    assert MessageType.DATA in seen
    seen.clear()
    derived.unsubscribe()
    src.down([(MessageType.DATA, 99)])
    assert seen == []


def test_up_forwards_without_error_when_subscribed() -> None:
    """``up()`` forwards to dependencies (no-op on sources); must not raise."""
    src = node(initial=0)
    mid = node([src], lambda deps, _: deps[0])
    unsub = mid.subscribe(lambda _m: None)
    mid.up([(MessageType.RESUME,)])
    unsub()


def test_lifecycle_pause_resume_propagate_through_multi_hop_chain() -> None:
    """PAUSE / RESUME from upstream reach downstream sinks (multi-hop ``node`` chain)."""
    src = node(initial=1)
    # Identity derived (not a no-fn wire): passthrough wires do not mirror ``initial``,
    # so the first recompute would see ``None`` before any ``down`` from the source.
    hop = node([src], lambda d, _: d[0])
    leaf = node([hop], lambda deps, _: deps[0] * 2)
    batches: list[list[MessageType]] = []

    def sink(msgs: list) -> None:
        batches.append([m[0] for m in msgs])

    unsub = leaf.subscribe(sink)
    src.down([(MessageType.PAUSE, "lock-a")])
    src.down([(MessageType.RESUME, "lock-a")])
    unsub()

    flat = [t for b in batches for t in b]
    assert MessageType.PAUSE in flat
    assert MessageType.RESUME in flat
    assert flat.index(MessageType.PAUSE) < flat.index(MessageType.RESUME)


def test_lifecycle_invalidate_propagates_and_clears_caches_along_chain() -> None:
    """INVALIDATE clears each node's cache (spec §1.2) and propagates through a multi-hop chain."""
    src = node(initial=42)
    hop = node([src], lambda d, _: d[0])
    leaf = node([hop], lambda deps, _: deps[0] + 1)
    saw_inv: list[bool] = []

    def sink(msgs: list) -> None:
        saw_inv.append(any(m[0] == MessageType.INVALIDATE for m in msgs))

    unsub = leaf.subscribe(sink)
    assert leaf.get() == 43
    src.down([(MessageType.INVALIDATE,)])
    unsub()

    assert any(saw_inv)
    assert src.get() is None
    assert hop.get() is None
    assert leaf.get() is None
    assert src.status == "dirty"


def test_invalidate_clears_dep_memo_so_identical_data_triggers_recompute() -> None:
    """After INVALIDATE, a derived node must not skip ``fn`` solely via dep identity memo."""
    src = node(initial=7)
    runs = 0

    def fn(deps: list, _a: object) -> int:
        nonlocal runs
        runs += 1
        return deps[0] + 1

    d = node([src], fn)
    unsub = d.subscribe(lambda _m: None)
    assert runs == 1
    assert d.get() == 8
    src.down([(MessageType.INVALIDATE,)])
    src.down([(MessageType.DIRTY,), (MessageType.DATA, 7)])
    unsub()

    assert runs == 2
    assert d.get() == 8


def test_invalidate_after_complete_reaches_sinks_and_clears_cache() -> None:
    """INVALIDATE passes the terminal gate like TEARDOWN (optimizations §9 / §9b)."""
    src = node(initial=1)
    types: list[MessageType] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            types.append(m[0])

    unsub = src.subscribe(sink)
    src.down([(MessageType.COMPLETE,)])
    src.down([(MessageType.INVALIDATE,)])
    unsub()

    assert MessageType.INVALIDATE in types
    assert src.get() is None


def test_invalidate_runs_fn_cleanup_once() -> None:
    """INVALIDATE invokes a registered ``fn`` cleanup callable (then clears cache)."""
    src = node(initial=0)
    cleanups = 0

    def fn(_deps: list, _a: object):
        def cleanup() -> None:
            nonlocal cleanups
            cleanups += 1

        return cleanup

    n = node([src], fn)
    unsub = n.subscribe(lambda _m: None)
    assert cleanups == 0
    n.down([(MessageType.INVALIDATE,)])
    assert cleanups == 1
    n.down([(MessageType.INVALIDATE,)])
    assert cleanups == 1
    unsub()


def test_reset_on_teardown_clears_cached_value() -> None:
    n = node(initial=42, reset_on_teardown=True)
    unsub = n.subscribe(lambda _m: None)
    assert n.get() == 42
    n.down([(MessageType.TEARDOWN,)])
    assert n.get() is None
    unsub()


def test_error_payload_is_exception_instance() -> None:
    source = node(initial=0)
    err = RuntimeError("boom")

    def _fail(_d: list, _a: object) -> None:
        raise err

    broken = node([source], _fail)
    payloads: list[object] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            if m[0] == MessageType.ERROR:
                payloads.append(m[1])

    unsub = broken.subscribe(sink)
    source.down([(MessageType.DATA, 1)])
    unsub()

    assert len(payloads) == 1
    assert payloads[0] is err


def test_source_node_emits_to_subscribers() -> None:
    s = node(initial=0)
    seen: list[list[MessageType]] = []

    def sink(msgs: list) -> None:
        seen.append([m[0] for m in msgs])

    unsub = s.subscribe(sink)
    s.down([(MessageType.DIRTY,), (MessageType.DATA, 1)])
    unsub()

    assert s.get() == 1
    assert s.status == "settled"
    assert seen == [[MessageType.DIRTY], [MessageType.DATA]]


def test_derived_emits_resolved_when_equals_unchanged() -> None:
    source = node(initial=1)
    derived = node(
        [source],
        lambda deps, _: "positive" if deps[0] > 0 else "other",
        equals=lambda a, b: a == b,
    )
    seen: list[list[MessageType]] = []

    def sink(msgs: list) -> None:
        seen.append([m[0] for m in msgs])

    unsub = derived.subscribe(sink)
    source.down([(MessageType.DATA, 2)])
    unsub()

    assert derived.get() == "positive"
    assert MessageType.RESOLVED in [t for batch in seen for t in batch]


def test_diamond_settles_once() -> None:
    a = node(initial=0)
    b = node([a], lambda d, _: d[0] + 1)
    c = node([a], lambda d, _: d[0] + 2)
    d_runs = 0

    def fn(deps: list, _a: object) -> int:
        nonlocal d_runs
        d_runs += 1
        return deps[0] + deps[1]

    d = node([b, c], fn)
    unsub = d.subscribe(lambda _m: None)
    before = d_runs
    a.down([(MessageType.DIRTY,), (MessageType.DATA, 5)])
    after = d_runs
    unsub()

    assert after - before == 1
    assert d.get() == 13


def test_fn_throw_error_downstream() -> None:
    source = node(initial=0)

    def _run(_d: list, _a: object) -> None:
        raise RuntimeError("boom")

    broken = node([source], _run)
    seen: list[MessageType] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            seen.append(m[0])

    unsub = broken.subscribe(sink)
    source.down([(MessageType.DATA, 1)])
    assert broken.status == "errored"
    assert MessageType.ERROR in seen
    unsub()


def test_resubscribable_after_terminal() -> None:
    n = node(initial=1, resubscribable=True)
    seen: list[MessageType] = []

    def sink1(msgs: list) -> None:
        for m in msgs:
            seen.append(m[0])

    unsub1 = n.subscribe(sink1)
    n.down([(MessageType.COMPLETE,)])
    unsub1()

    unsub2 = n.subscribe(lambda _m: None)
    n.down([(MessageType.DATA, 2)])
    unsub2()

    assert MessageType.COMPLETE in seen
    assert n.get() == 2


def test_passthrough_unknown_type() -> None:
    CUSTOM = "CUSTOM"
    source = node(initial=0)
    passthrough = node([source])
    seen: list[object] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            seen.append(m[0])

    unsub = passthrough.subscribe(sink)
    source.down([(CUSTOM, 1)])
    unsub()

    assert CUSTOM in seen


def test_wire_deps_no_fn() -> None:
    source = node(initial=1)
    wire = node([source], name="wire")
    seen: list[MessageType] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            seen.append(m[0])

    unsub = wire.subscribe(sink)
    source.down([(MessageType.DATA, 2)])
    unsub()

    assert wire.name == "wire"
    assert MessageType.DATA in seen


def test_complete_when_deps_complete_default() -> None:
    a = node(initial=1)
    b = node(initial=2)
    derived = node([a, b], lambda d, _: d[0] + d[1])
    seen: list[MessageType] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            seen.append(m[0])

    unsub = derived.subscribe(sink)
    a.down([(MessageType.COMPLETE,)])
    assert MessageType.COMPLETE not in seen
    b.down([(MessageType.COMPLETE,)])
    unsub()

    assert MessageType.COMPLETE in seen


def test_complete_when_deps_complete_false() -> None:
    a = node(initial=1)
    b = node(initial=2)
    derived = node(
        [a, b],
        lambda d, _: d[0] + d[1],
        complete_when_deps_complete=False,
    )
    seen: list[MessageType] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            seen.append(m[0])

    unsub = derived.subscribe(sink)
    a.down([(MessageType.COMPLETE,)])
    b.down([(MessageType.COMPLETE,)])
    unsub()

    assert MessageType.COMPLETE not in seen
    assert derived.status != "completed"


def test_many_deps_bitmask() -> None:
    sources = [node(initial=i) for i in range(40)]

    def sum_fn(deps: list, _a: object) -> int:
        return sum(deps)

    combined = node(sources, sum_fn)
    unsub = combined.subscribe(lambda _m: None)
    sources[35].down([(MessageType.DIRTY,), (MessageType.DATA, 100)])
    assert combined.get() == 780 - 35 + 100
    unsub()


def test_producer_form() -> None:
    def producer(_deps: list, actions: object) -> None:
        assert hasattr(actions, "down")
        actions.down([(MessageType.DATA, 42)])  # type: ignore[attr-defined]

    p = node(producer, name="producer-like")
    values: list[int] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            if m[0] == MessageType.DATA:
                values.append(m[1])

    unsub = p.subscribe(sink)
    unsub()

    assert p.name == "producer-like"
    assert values == [42]


def test_meta_snapshot_and_subscribe() -> None:
    src = node(initial=1)
    n = node(
        [src],
        lambda d, _: d[0] * 2,
        meta={"err": None},
        name="with-meta",
    )
    assert n.meta["err"].get() is None
    assert meta_snapshot(n) == {"err": None}

    seen: list[list[MessageType]] = []

    def sink(msgs: list) -> None:
        seen.append([m[0] for m in msgs])

    unsub = n.meta["err"].subscribe(sink)
    n.meta["err"].down([(MessageType.DIRTY,), (MessageType.DATA, "bad")])
    unsub()

    assert seen == [[MessageType.DIRTY], [MessageType.DATA]]
    assert meta_snapshot(n)["err"] == "bad"


def test_meta_standalone_without_parent_subscribe() -> None:
    n = node(initial=0, meta={"tag": "a"})
    assert n.meta["tag"].get() == "a"
    n.meta["tag"].down([(MessageType.DATA, "b")])
    assert n.meta["tag"].get() == "b"
    assert meta_snapshot(n) == {"tag": "b"}


def test_teardown_after_complete_runs_lifecycle_and_reaches_sinks_b3() -> None:
    """TEARDOWN after COMPLETE runs lifecycle and reaches sinks (decision B3)."""
    src = node(initial=1)
    n = node([src], lambda deps, _: deps[0], meta={"m": 0})
    meta_saw_teardown = False

    def meta_sink(msgs: list) -> None:
        nonlocal meta_saw_teardown
        if any(m[0] == MessageType.TEARDOWN for m in msgs):
            meta_saw_teardown = True

    sink_types: list[MessageType] = []

    def parent_sink(msgs: list) -> None:
        for m in msgs:
            sink_types.append(m[0])

    unsub_meta = n.meta["m"].subscribe(meta_sink)
    unsub = n.subscribe(parent_sink)
    src.down([(MessageType.COMPLETE,)])
    assert n.status == "completed"
    n.down([(MessageType.TEARDOWN,)])
    assert meta_saw_teardown is True
    assert sum(1 for t in sink_types if t == MessageType.TEARDOWN) >= 1
    unsub()
    unsub_meta()


def test_batch_discards_deferred_on_outer_exception() -> None:
    """graphrefly-ts: phase-2 not flushed when outer batch throws."""
    log: list[str] = []

    def sink(msgs: list) -> None:
        for m in msgs:
            log.append(m[0].value)

    with pytest.raises(RuntimeError, match="abort"), batch():
        emit_with_batch(sink, [(MessageType.DATA, 1)])
        raise RuntimeError("abort")

    assert log == []


def test_meta_snapshot_empty_without_meta_option() -> None:
    n = node(initial=1)
    assert meta_snapshot(n) == {}


def test_meta_snapshot_omits_keys_when_child_get_raises() -> None:
    n = node(initial=0, meta={"fine": 1, "bad": 2})
    bad = n.meta["bad"]
    real_get = NodeImpl.get

    def get_wrap(self: NodeImpl[object]) -> object:
        if self is bad:
            raise RuntimeError("no snapshot")
        return real_get(self)

    with patch.object(NodeImpl, "get", get_wrap):
        assert meta_snapshot(n) == {"fine": 1}


def test_meta_snapshot_omits_all_keys_that_throw() -> None:
    n = node(initial=0, meta={"ok": 0, "x": 1, "y": 2})
    xn = n.meta["x"]
    yn = n.meta["y"]
    real_get = NodeImpl.get

    def get_wrap(self: NodeImpl[object]) -> object:
        if self is xn or self is yn:
            raise RuntimeError("bad")
        return real_get(self)

    with patch.object(NodeImpl, "get", get_wrap):
        assert meta_snapshot(n) == {"ok": 0}


def test_parent_teardown_disconnects_when_one_meta_down_throws() -> None:
    src = node(initial=1)
    n = node([src], lambda d, _: d[0] * 2, meta={"flaky": 0, "stable": 1})
    flaky = n.meta["flaky"]
    real_down = NodeImpl.down

    def down_wrap(self: NodeImpl[object], msgs: list) -> None:
        if self is flaky and msgs and msgs[0][0] == MessageType.TEARDOWN:
            raise RuntimeError("meta teardown boom")
        return real_down(self, msgs)

    stable_saw_teardown = False

    def stable_sink(msgs: list) -> None:
        nonlocal stable_saw_teardown
        if any(m[0] == MessageType.TEARDOWN for m in msgs):
            stable_saw_teardown = True

    with patch.object(NodeImpl, "down", down_wrap):
        unsub_stable = n.meta["stable"].subscribe(stable_sink)
        unsub = n.subscribe(lambda _m: None)
        n.down([(MessageType.TEARDOWN,)])
    assert stable_saw_teardown is True
    assert n.status == "disconnected"
    unsub()
    unsub_stable()


def test_parent_teardown_propagates_to_every_meta_child() -> None:
    n = node(initial=0, meta={"a": 1, "b": 2, "c": 3})
    saw: list[str] = []
    unsubs = [
        n.meta[k].subscribe(
            lambda msgs, key=k: (
                saw.append(key) if any(m[0] == MessageType.TEARDOWN for m in msgs) else None
            )
        )
        for k in ("a", "b", "c")
    ]
    n.down([(MessageType.TEARDOWN,)])
    assert sorted(saw) == ["a", "b", "c"]
    for u in unsubs:
        u()


def test_teardown_stops_producer_when_meta_down_throws() -> None:
    producer_runs = 0

    def prod(_deps: list[object], actions: NodeActions) -> None:
        nonlocal producer_runs
        producer_runs += 1
        actions.down([(MessageType.DATA, producer_runs)])

    p = node(prod, meta={"m": 0})
    mchild = p.meta["m"]
    real_down = NodeImpl.down

    def down_wrap(self: NodeImpl[object], msgs: list) -> None:
        if self is mchild and msgs and msgs[0][0] == MessageType.TEARDOWN:
            raise RuntimeError("meta teardown boom")
        return real_down(self, msgs)

    with patch.object(NodeImpl, "down", down_wrap):
        u1 = p.subscribe(lambda _m: None)
        assert producer_runs == 1
        p.down([(MessageType.TEARDOWN,)])
        assert producer_runs == 1
        u1()
        u2 = p.subscribe(lambda _m: None)
        assert producer_runs == 2
        u2()


def test_describe_node_includes_meta_and_spec_fields() -> None:
    n = node(
        initial=0,
        meta={"description": "purpose", "type_hint": "integer"},
        name="retry_limit",
    )
    d = describe_node(n)
    assert d["type"] == "state"
    assert d["status"] == "settled"
    assert d["name"] == "retry_limit"
    assert d["deps"] == []
    assert d["meta"] == {"description": "purpose", "type_hint": "integer"}
    assert d["value"] == 0


def test_describe_node_derived_lists_dep_names() -> None:
    src = node(initial=1, name="input")
    n = node([src], lambda deps, _: deps[0] * 2, name="validate", meta={"description": "ok"})
    snap = describe_node(n)
    assert snap["type"] == "derived"
    assert snap["deps"] == ["input"]
    assert snap["meta"] == {"description": "ok"}


def test_meta_mapping_is_read_only() -> None:
    n = node(initial=0, meta={"k": 1})
    with pytest.raises(TypeError):
        n.meta["new"] = n.meta["k"]  # type: ignore[index]
