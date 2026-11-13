"""Local-name admission and module inventories for graphrefly:R-naming@1."""

from __future__ import annotations

from typing import Any

import pytest

from graphrefly import (
    Graph,
    GraphReflyRestoreError,
    GraphReflyValueError,
    Recipe,
    restore_graph,
    wire_bridge,
    wire_edge_group_in,
)
from graphrefly._kernel import Graph as KernelGraph
from graphrefly._kernel import KernelError

NAME_ERROR = "name must not contain '::'"
BOUNDARY_NAME_ERROR = "name must not start or end with ':'"
INVALID_NAMES = [
    (name, NAME_ERROR) for name in ["a::b", "::a", "a::", "module/part::child", "#a::b"]
] + [(name, BOUNDARY_NAME_ERROR) for name in [":a", "a:", ":"]]


@pytest.mark.parametrize("name, message", INVALID_NAMES)
@pytest.mark.parametrize("kind", ["state", "node", "derived", "effect", "producer", "bridge"])
def test_local_names_reject_mount_delimiter_before_registration(
    name: str, message: str, kind: str
) -> None:
    with Graph() as graph:
        with pytest.raises(GraphReflyValueError) as error:
            if kind == "state":
                graph.state(1, name=name)
            elif kind == "producer":
                graph.producer(lambda: 1, name=name)
            elif kind == "bridge":
                wire_bridge(graph, session_id="s1", name=name)
            else:
                getattr(graph, kind)([], lambda *args: None, name=name)
        assert str(error.value) == message
        assert graph.describe() == []
        assert graph.state(1, name="local/module").name == "local/module"
        assert graph.state(1, name="a:b").name == "a:b"


@pytest.mark.parametrize("kind", ["state", "node"])
@pytest.mark.parametrize("name, message", INVALID_NAMES)
def test_kernel_registration_also_rejects_reserved_delimiter(
    kind: str, name: str, message: str
) -> None:
    graph = KernelGraph()
    with pytest.raises(KernelError) as error:
        if kind == "state":
            graph.state(name, 1)
        else:
            graph.node(name, [], lambda ctx: None)
    assert str(error.value) == message
    assert graph.describe() == []


@pytest.mark.parametrize("name, message", INVALID_NAMES)
def test_generated_edge_names_are_preflighted_before_any_module_node(
    name: str, message: str
) -> None:
    with Graph() as graph:
        frames = graph.state(name="frames")
        before = graph.describe()
        with pytest.raises(GraphReflyValueError) as error:
            wire_edge_group_in(graph, group="g", frames=frames, edges=["good", name])
        assert str(error.value) == message
        assert graph.describe() == before


@pytest.mark.parametrize("name, message", INVALID_NAMES)
def test_restore_rejects_reserved_name_before_any_recipe_factory(name: str, message: str) -> None:
    with Graph() as graph:
        graph.node([], lambda ctx: None, name="first", recipe=Recipe("r"))
        graph.state(1, name="last")
        checkpoint = graph.checkpoint()
    checkpoint["nodes"][-1]["id"] = name
    calls: list[Any] = []

    def factory(config: Any, at: Any) -> Any:
        calls.append(at.id)
        return lambda ctx: None

    with pytest.raises(GraphReflyRestoreError) as error:
        restore_graph(checkpoint, registry={"r": factory})
    assert str(error.value) == message
    assert calls == []


@pytest.mark.parametrize("name, message", INVALID_NAMES)
def test_kernel_bridge_preflights_prefix_before_creating_child_nodes(
    name: str, message: str
) -> None:
    from graphrefly._wire.envelope import wire_bridge as kernel_wire_bridge

    graph = KernelGraph()
    with pytest.raises(KernelError) as error:
        kernel_wire_bridge(graph, name, session_id="s1")
    assert str(error.value) == message
    assert graph.describe() == []


# Every exported factory whose return value owns a newly generated node is exercised
# with its real defaults. Prerequisite nodes are named explicitly and excluded.
class _ColdRunner:
    def spawn(self, job: Any) -> None:
        raise AssertionError("naming inspection must not launch an async job")


class _ColdHttp:
    async def request(self, request: Any) -> Any:
        raise AssertionError("naming inspection must not send HTTP")

    async def stream(self, request: Any) -> Any:
        raise AssertionError("naming inspection must not open a stream")
        yield  # pragma: no cover


def _module_factory(graph: Graph, name: str) -> Any:
    import graphrefly as api

    runner = _ColdRunner()

    async def value(*args: Any) -> int:
        return 1

    async def values() -> Any:
        yield 1

    if name == "wire_edge_group_out":
        source = graph.state(b"x", name="input")
        return lambda: api.wire_edge_group_out(graph, group="group", edges={"edge": source})
    if name == "wire_edge_group_in":
        frames = graph.state(name="input")
        return lambda: api.wire_edge_group_in(
            graph, group="group", frames=frames, edges=["edge", "issues"]
        )
    if name == "frame_source":
        return lambda: api.frame_source(graph, lambda deliver: lambda: None)
    if name == "wire_bridge":
        return lambda: api.wire_bridge(graph, session_id="s1")
    if name == "wire_bridge_ack_driver":
        bridge = api.wire_bridge(graph, session_id="s1", name="transport")
        clock = graph.state(0, name="clock")
        return lambda: api.wire_bridge_ack_driver(graph, bridge, clock=clock, timeout_ms=10)
    if name == "async_node":
        source = graph.state(1, name="input")
        return lambda: api.async_node(graph, [source], runner, value)
    if name == "from_awaitable":
        return lambda: api.from_awaitable(graph, runner, value)
    if name == "from_async_iter":
        return lambda: api.from_async_iter(graph, runner, values)
    if name == "from_http":
        return lambda: api.from_http(
            graph, "https://example.test", driver=_ColdHttp(), runner=runner
        )
    if name == "from_sse":
        return lambda: api.from_sse(
            graph, "https://example.test", stream_driver=_ColdHttp(), runner=runner
        )
    raise AssertionError(f"unscanned node factory: {name}")


MODULE_SCOPES = {
    "wire_edge_group_out": "group/out",
    "wire_edge_group_in": "group/in",
    "frame_source": "frameSource",
    "wire_bridge": "wireBridge",
    "wire_bridge_ack_driver": "s1/ackDriver",
    "async_node": "asyncNode",
    "from_awaitable": "fromAwaitable",
    "from_async_iter": "fromAsyncIter",
    "from_http": "fromHttp",
    "from_sse": "fromSse",
}


def _check_generated_names(ids: list[str]) -> None:
    import re

    for node_id in ids:
        parts = node_id.split("/")
        assert all(re.fullmatch(r"[a-z][a-zA-Z0-9]*", part) for part in parts), node_id
        words = [
            set(re.findall(r"[a-z]+|[A-Z][a-z]*", part.lower() if part.islower() else part))
            for part in parts
        ]
        for before, after in zip(words, words[1:], strict=False):
            assert not ({word.lower() for word in before} & {word.lower() for word in after}), (
                node_id
            )


@pytest.mark.parametrize("name, scope", MODULE_SCOPES.items())
def test_every_public_module_default_has_readable_collision_safe_ids(
    name: str, scope: str
) -> None:
    with Graph() as graph:
        build = _module_factory(graph, name)
        before = {row["id"] for row in graph.describe()}
        suffixes = None
        for index in range(3):
            build()
            created = {row["id"] for row in graph.describe()} - before
            _check_generated_names(sorted(created))
            expected_scope = scope if index == 0 else f"{scope}{index}"
            assert expected_scope in created
            assert all(
                node == expected_scope or node.startswith(f"{expected_scope}/")
                for node in created
            )
            current = {node.removeprefix(expected_scope) for node in created}
            if suffixes is None:
                suffixes = current
            assert current == suffixes
            before.update(created)


def test_public_node_factory_scan_is_complete() -> None:
    import inspect

    import graphrefly as api

    actual = {
        name
        for name in api.__all__
        if inspect.isfunction(value := getattr(api, name))
        and any(
            word in str(inspect.signature(value).return_annotation)
            for word in ("Node[", "WireBridge", "WireEdgeGroupIn")
        )
    }
    assert actual == set(MODULE_SCOPES)


def test_default_module_prefix_skips_occupied_descendant_and_numbered_names() -> None:
    from graphrefly import frame_source

    with Graph() as graph:
        graph.state(1, name="frameSource/existing")
        graph.state(2, name="frameSource1")
        source = frame_source(graph, lambda deliver: lambda: None)
        assert source.name == "frameSource2"


def test_inbound_edge_named_issues_stays_distinct_from_diagnostics() -> None:
    with Graph() as graph:
        frames = graph.state(name="frames")
        incoming = wire_edge_group_in(
            graph, group="g", frames=frames, edges=["issues"], name="bridge"
        )
        assert incoming.inbound["issues"].name == "bridge/edges/issues"
        assert incoming.issues.name == "bridge/issues"
        assert graph.find("bridge/edges/issues") is incoming.inbound["issues"]
        assert graph.find("bridge/issues") is incoming.issues


def test_user_labels_are_not_recased_or_normalized() -> None:
    with Graph() as graph:
        bridge = wire_bridge(graph, session_id="s::1", name="User.Label-name")
        assert bridge.status.name == "User.Label-name/status"
        assert graph.find("User.Label-name.status") is None
        assert graph.state(1, name="custom-label.value").name == "custom-label.value"


@pytest.mark.parametrize("scope, message", INVALID_NAMES)
@pytest.mark.parametrize("module", ["in", "out", "ack"])
def test_wire_identity_is_validated_when_used_as_a_default_name_prefix(
    scope: str, message: str, module: str
) -> None:
    from graphrefly import wire_bridge_ack_driver, wire_edge_group_out

    with Graph() as graph:
        source = graph.state(b"x", name="source")
        frames = graph.state(name="frames")
        clock = graph.state(0, name="clock")
        bridge = wire_bridge(graph, session_id=scope, name="transport")
        before = graph.describe()

        def build(name: str | None = None) -> Any:
            if module == "in":
                return wire_edge_group_in(graph, group=scope, frames=frames, edges=["x"], name=name)
            if module == "out":
                return wire_edge_group_out(graph, group=scope, edges={"x": source}, name=name)
            return wire_bridge_ack_driver(graph, bridge, clock=clock, timeout_ms=10, name=name)

        with pytest.raises(GraphReflyValueError) as error:
            build()
        assert str(error.value) == message
        assert graph.describe() == before
        build("custom")  # The same wire payload identity is legal with an explicit local name.
