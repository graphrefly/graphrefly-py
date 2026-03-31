"""Orchestration patterns (roadmap §4.1).

Domain-layer helpers that build workflow shapes on top of core + extra primitives.
Export under ``graphrefly.patterns.orchestration`` to avoid collisions with Phase 2
operator names like ``gate`` and ``for_each``.
"""

from __future__ import annotations

from typing import Any, TypedDict

from graphrefly.core.node import Node, NodeActions, node
from graphrefly.core.protocol import MessageType
from graphrefly.graph.graph import GRAPH_META_SEGMENT, PATH_SEP, Graph

type StepRef = str | Node[Any]


class BranchResult(TypedDict):
    branch: str
    value: Any


def _resolve_dep(graph: Graph, dep: StepRef) -> tuple[Node[Any], str | None]:
    if isinstance(dep, str):
        return graph.resolve(dep), dep
    path = _find_registered_node_path(graph, dep)
    if path is None:
        msg = (
            "orchestration dep node must already be registered in the graph so "
            "explicit edges can be recorded; pass a string path or register the node first"
        )
        raise ValueError(msg)
    return dep, path


def _find_registered_node_path(graph: Graph, target: Node[Any]) -> str | None:
    described = graph.describe()
    meta_segment = f"{PATH_SEP}{GRAPH_META_SEGMENT}{PATH_SEP}"
    for path in sorted(str(k) for k in described["nodes"]):
        if meta_segment in path:
            continue
        try:
            if graph.resolve(path) is target:
                return path
        except KeyError:
            continue
    return None


def _register_step(
    graph: Graph,
    step_name: str,
    step: Node[Any],
    dep_paths: list[str],
) -> None:
    graph.add(step_name, step)
    for dep_path in dep_paths:
        graph.connect(dep_path, step_name)


def _base_meta(kind: str, meta: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"orchestration": True, "orchestration_type": kind}
    if meta:
        out.update(meta)
    return out


def pipeline(name: str, *, opts: dict[str, Any] | None = None) -> Graph:
    """Create an orchestration graph container."""
    return Graph(name, opts)


def task(
    graph: Graph,
    name: str,
    run: Any,
    *,
    deps: list[StepRef] | None = None,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> Node[Any]:
    """Register a workflow task node."""
    resolved = [_resolve_dep(graph, dep) for dep in (deps or [])]
    dep_nodes = [n for n, _ in resolved]
    dep_paths = [p for _n, p in resolved if p is not None]
    step = node(
        dep_nodes,
        run,
        name=name,
        describe_kind="derived",
        meta=_base_meta("task", meta),
        **node_opts,
    )
    _register_step(graph, name, step, dep_paths)
    return step


def branch(
    graph: Graph,
    name: str,
    source: StepRef,
    predicate: Any,
    *,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> Node[BranchResult]:
    """Register a branch step that tags each value as ``then`` or ``else``."""
    source_node, source_path = _resolve_dep(graph, source)

    def compute(deps: list[Any], _actions: NodeActions) -> BranchResult:
        value = deps[0]
        return {
            "branch": "then" if bool(predicate(value)) else "else",
            "value": value,
        }

    step = node(
        [source_node],
        compute,
        name=name,
        describe_kind="derived",
        meta=_base_meta("branch", meta),
        **node_opts,
    )
    _register_step(graph, name, step, [source_path] if source_path else [])
    return step


def gate(
    graph: Graph,
    name: str,
    source: StepRef,
    control: StepRef,
    *,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> Node[Any]:
    """Register a value-level gate step controlled by a boolean signal."""
    source_node, source_path = _resolve_dep(graph, source)
    control_node, control_path = _resolve_dep(graph, control)

    def compute(_deps: list[Any], actions: NodeActions) -> Any:
        if not bool(control_node.get()):
            actions.down([(MessageType.RESOLVED,)])
            return None
        return source_node.get()

    step = node(
        [source_node, control_node],
        compute,
        name=name,
        describe_kind="operator",
        meta=_base_meta("gate", meta),
        **node_opts,
    )
    paths = [p for p in (source_path, control_path) if p is not None]
    _register_step(graph, name, step, paths)
    return step


def approval(
    graph: Graph,
    name: str,
    source: StepRef,
    approver: StepRef,
    *,
    is_approved: Any | None = None,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> Node[Any]:
    """Register an approval gate (human/LLM/system signal controls value flow)."""
    source_node, source_path = _resolve_dep(graph, source)
    approver_node, approver_path = _resolve_dep(graph, approver)
    approved_fn = is_approved if is_approved is not None else (lambda value: bool(value))

    def compute(_deps: list[Any], actions: NodeActions) -> Any:
        if not bool(approved_fn(approver_node.get())):
            actions.down([(MessageType.RESOLVED,)])
            return None
        return source_node.get()

    step = node(
        [source_node, approver_node],
        compute,
        name=name,
        describe_kind="operator",
        meta=_base_meta("approval", meta),
        **node_opts,
    )
    paths = [p for p in (source_path, approver_path) if p is not None]
    _register_step(graph, name, step, paths)
    return step


__all__ = [
    "BranchResult",
    "StepRef",
    "approval",
    "branch",
    "gate",
    "pipeline",
    "task",
]
