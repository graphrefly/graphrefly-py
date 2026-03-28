"""Actor, guard, policy (roadmap 1.5) — precedence C, scoped describe D."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from graphrefly import (
    Graph,
    GuardDenied,
    Messages,
    MessageType,
    derived,
    node,
    policy,
    state,
    system_actor,
)
from graphrefly.core.guard import compose_guards

if TYPE_CHECKING:
    from graphrefly.core.node import NodeActions


def test_policy_deny_overrides_allow_for_same_action() -> None:
    """C: any matching deny makes the decision False even if an allow also matches."""
    g = policy(
        lambda allow, deny: [
            allow("write", where=lambda a: True),
            deny("write", where=lambda a: a.get("type") == "llm"),
        ]
    )
    assert g({"type": "human", "id": "1"}, "write") is True
    assert g({"type": "llm", "id": "bot"}, "write") is False


def test_policy_no_matching_allow_denies() -> None:
    g = policy(lambda allow, deny: [deny("write", where=lambda a: a.get("type") == "llm")])
    assert g({"type": "human", "id": "1"}, "write") is False


def test_policy_allow_observe_open() -> None:
    g = policy(lambda allow, deny: [allow("observe")])
    assert g({"type": "any", "id": ""}, "observe") is True
    assert g({"type": "any", "id": ""}, "write") is False


def test_graph_set_guard_denied_raises() -> None:
    g = Graph("g")
    secret = state(
        0,
        guard=lambda a, act: act != "write" or a.get("type") == "human",
    )
    g.add("secret", secret)
    g.set("secret", 1, actor={"type": "human", "id": "u"})
    assert secret.get() == 1
    with pytest.raises(GuardDenied) as ei:
        g.set("secret", 2, actor={"type": "llm", "id": "a"})
    e = ei.value
    assert e.action == "write"
    assert e.node == "secret"
    assert e.actor["type"] == "llm"


def test_graph_signal_guard_denied() -> None:
    g = Graph("g")
    n = state(0, guard=lambda a, act: act != "signal" or a.get("type") == "wallet")
    g.add("n", n)
    with pytest.raises(GuardDenied) as ei:
        g.signal([(MessageType.PAUSE,)], actor=system_actor())
    assert ei.value.action == "signal"


def _observe_block_llm(a: object, act: str) -> bool:
    ad = a  # type: ignore[assignment]
    return act != "observe" or ad.get("type") != "llm"  # type: ignore[union-attr]


def test_describe_scoped_filters_nodes_edges_subgraphs() -> None:
    """D: hidden nodes removed; edges touching hidden endpoints removed; empty mounts pruned."""
    root = Graph("root")
    child = Graph("child")
    root.mount("sub", child)
    inner = state(1, guard=_observe_block_llm)
    child.add("inner", inner)

    full = root.describe()
    assert "sub::inner" in full["nodes"]

    scoped_llm = root.describe(actor={"type": "llm", "id": "x"})
    assert "sub::inner" not in scoped_llm["nodes"]
    assert "sub" not in scoped_llm["subgraphs"]

    scoped_human = root.describe(actor={"type": "human", "id": "x"})
    assert "sub::inner" in scoped_human["nodes"]
    assert "sub" in scoped_human["subgraphs"]

    g2 = Graph("g2")
    a = state(1)
    b = state(2, guard=_observe_block_llm)
    d = derived([a, b], lambda xs, _: xs[0] + xs[1])
    g2.add("a", a)
    g2.add("b", b)
    g2.add("d", d)
    g2.connect("a", "d")
    g2.connect("b", "d")
    edges_llm = g2.describe(actor={"type": "llm", "id": ""})["edges"]
    ends = {(e["from"], e["to"]) for e in edges_llm}
    assert ("a", "d") in ends
    assert ("b", "d") not in ends


def test_observe_subscribe_respects_guard() -> None:
    g = Graph("g")
    n = state(0, guard=lambda a, act: act != "observe" or a.get("type") == "human")
    g.add("x", n)
    got: list[Messages] = []

    def sink(m: Messages) -> None:
        got.append(m)

    unsub = g.observe("x", actor={"type": "human", "id": "u"}).subscribe(sink)
    g.set("x", 1)
    assert got
    unsub()

    with pytest.raises(GuardDenied):
        g.observe("x", actor={"type": "llm", "id": "b"}).subscribe(sink)


def test_last_mutation_recorded_on_external_write() -> None:
    n = state(0)
    n.down([(MessageType.DATA, 5)], actor={"type": "wallet", "id": "0xabc"})
    lm = n.last_mutation
    assert lm is not None
    assert lm["actor"]["type"] == "wallet"
    assert lm["actor"]["id"] == "0xabc"
    assert "timestamp_ns" in lm


def test_graph_remove_teardown_is_internal_after_registry_pop() -> None:
    """Removing a name pops the registry first; TEARDOWN must not fail on guards."""
    g = Graph("g")
    n = state(0, guard=lambda a, act: False)
    g.add("x", n)
    g.remove("x")
    with pytest.raises(KeyError):
        g.node("x")


def test_compose_guards_and() -> None:
    g1 = policy(lambda a, d: [a("write", where=lambda x: x.get("role") == "admin")])
    g2 = policy(lambda a, d: [a("write", where=lambda x: x.get("type") == "human")])
    g = compose_guards(g1, g2)
    assert g({"type": "human", "id": "", "role": "admin"}, "write") is True
    assert g({"type": "human", "id": "", "role": "user"}, "write") is False


# --- on_message handler (spec §2.6) ---


def test_on_message_intercepts_dep_message() -> None:
    """on_message returning True suppresses normal handling for subsequent messages."""
    src = state(0)
    intercepted: list[tuple] = []

    def handler(msg: tuple, index: int, actions: NodeActions) -> bool:
        intercepted.append(msg)
        return True  # consume

    downstream = node([src], lambda vs, _: vs[0] * 2, on_message=handler)
    collected: list[Messages] = []
    downstream.subscribe(lambda m: collected.append(m))

    # Clear the initial connect emission
    collected.clear()
    intercepted.clear()

    src.down([(MessageType.DATA, 5)])
    # on_message consumed every dep message, so the fn never re-ran — no DATA(10)
    assert len(intercepted) > 0
    data_msgs = [m for batch in collected for m in batch if m[0] is MessageType.DATA]
    assert not data_msgs


def test_on_message_receives_dep_index() -> None:
    a = state(1)
    b = state(2)
    indices: list[int] = []

    def handler(msg: tuple, index: int, actions: NodeActions) -> bool:
        if msg[0] is MessageType.DATA:
            indices.append(index)
        return False  # don't consume

    c = node([a, b], lambda vs, _: vs[0] + vs[1], on_message=handler)
    c.subscribe(lambda m: None)

    a.down([(MessageType.DATA, 10)])
    b.down([(MessageType.DATA, 20)])
    assert 0 in indices
    assert 1 in indices


def test_on_message_error_propagates() -> None:
    src = state(0)

    def handler(msg: tuple, index: int, actions: NodeActions) -> bool:
        raise RuntimeError("boom")

    downstream = node([src], lambda vs, _: vs[0], on_message=handler)
    errors: list[Exception] = []

    def sink(msgs: Messages) -> None:
        for m in msgs:
            if m[0] is MessageType.ERROR:
                errors.append(m[1])

    downstream.subscribe(sink)
    src.down([(MessageType.DATA, 1)])
    assert len(errors) == 1
    assert str(errors[0]) == "boom"


# --- subscribe observe guard (direct node test) ---


def test_node_subscribe_observe_guard() -> None:
    """Direct node.subscribe with actor= respects guard observe check."""
    n = state(0, guard=lambda a, act: act != "observe" or a.get("type") == "human")
    got: list[Messages] = []
    unsub = n.subscribe(lambda m: got.append(m), actor={"type": "human", "id": "u"})
    n.down([(MessageType.DATA, 1)])
    assert got
    unsub()

    with pytest.raises(GuardDenied):
        n.subscribe(lambda m: None, actor={"type": "llm", "id": "b"})


# --- up() guard check ---


def test_up_guard_check() -> None:
    """up() on an external call checks the guard; internal call bypasses."""
    # Allow observe (so subscribe works) but deny write
    def deny_write(a: object, act: str) -> bool:
        return act == "observe"

    src = state(0)
    child = node([src], lambda vs, _: vs[0], guard=deny_write)
    child.subscribe(lambda m: None)

    # External up should be denied (default guard_action is "write")
    with pytest.raises(GuardDenied):
        child.up([(MessageType.DATA, 99)], actor={"type": "human", "id": "u"})

    # Internal up should succeed (no raise)
    child.up([(MessageType.DATA, 99)], internal=True)


# --- destroy() succeeds even when guards deny signal ---


def test_destroy_bypasses_guards() -> None:
    """Graph.destroy() uses internal=True so guards cannot block teardown."""
    g = Graph("g")
    n = state(0, guard=lambda a, act: False)  # deny everything
    g.add("x", n)

    # signal should be denied
    with pytest.raises(GuardDenied):
        g.signal([(MessageType.PAUSE,)], actor={"type": "system", "id": ""})

    # destroy should succeed (internal bypass)
    g.destroy()
    with pytest.raises(KeyError):
        g.node("x")


# --- allows_observe / has_guard ---


def test_allows_observe_and_has_guard() -> None:
    n1 = state(0)
    assert n1.has_guard() is False
    assert n1.allows_observe() is True

    n2 = state(0, guard=lambda a, act: act != "observe" or a.get("type") == "human")
    assert n2.has_guard() is True
    assert n2.allows_observe({"type": "human", "id": ""}) is True
    assert n2.allows_observe({"type": "llm", "id": ""}) is False


# --- meta guard inheritance ---


def test_meta_inherits_guard() -> None:
    """Meta companion nodes inherit their parent's guard."""
    def guard_fn(a: object, act: str) -> bool:
        return a.get("type") == "human"  # type: ignore[union-attr]

    n = state(0, guard=guard_fn, meta={"label": "hi"})
    meta_node = n.meta["label"]
    assert meta_node.has_guard() is True
    # Human can write to meta
    meta_node.down([(MessageType.DATA, "new")], actor={"type": "human", "id": "u"})
    assert meta_node.get() == "new"
    # LLM cannot
    with pytest.raises(GuardDenied):
        meta_node.down([(MessageType.DATA, "bad")], actor={"type": "llm", "id": "b"})
