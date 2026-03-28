"""Node primitive and meta — parity with graphrefly-ts ``src/__tests__/core/node.test.ts``."""

from __future__ import annotations

import pytest

from graphrefly.core import (
    MessageType,
    batch,
    emit_with_batch,
    meta_snapshot,
    node,
)


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
