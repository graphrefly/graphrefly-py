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
