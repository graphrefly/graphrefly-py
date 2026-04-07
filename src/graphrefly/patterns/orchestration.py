"""Orchestration patterns (roadmap §4.1).

Domain-layer helpers that build workflow shapes on top of core + extra primitives.
Export under ``graphrefly.patterns.orchestration`` to avoid collisions with Phase 2
operator names like ``valve`` and ``for_each``.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from graphrefly.core.node import Node, NodeActions, node
from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import derived, state
from graphrefly.graph.graph import GRAPH_META_SEGMENT, PATH_SEP, Graph

if TYPE_CHECKING:
    from collections.abc import Callable

type StepRef = str | Node[Any]


@dataclass(frozen=True, slots=True)
class BranchResult:
    """Result of :func:`branch` — tags each value as ``then`` or ``else``."""

    branch: str
    value: Any


@dataclass(frozen=True, slots=True)
class SensorControls[T]:
    """Result bundle for :func:`sensor`."""

    node: Node[T]
    push: Callable[[T], None]
    error: Callable[[BaseException | Any], None]
    complete: Callable[[], None]


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


def _coerce_loop_iterations(raw: Any) -> int:
    parsed: float
    if isinstance(raw, str):
        trimmed = raw.strip()
        if len(trimmed) == 0:
            parsed = 0.0
        else:
            try:
                parsed = float(trimmed)
            except ValueError:
                parsed = float("nan")
    elif raw is None:
        parsed = 0.0
    else:
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            parsed = float("nan")
    if not math.isfinite(parsed):
        return 1
    count = int(parsed)
    if count < 0:
        return 0
    return count


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
        return BranchResult(
            branch="then" if bool(predicate(value)) else "else",
            value=value,
        )

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


def valve(
    graph: Graph,
    name: str,
    source: StepRef,
    control: StepRef,
    *,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> Node[Any]:
    """Register a value-level valve step controlled by a boolean signal."""
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
        meta=_base_meta("valve", meta),
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


def for_each(
    graph: Graph,
    name: str,
    source: StepRef,
    run: Callable[[Any, NodeActions], None],
    *,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> Node[Any]:
    """Register a workflow side-effect step that remains graph-observable."""
    source_node, source_path = _resolve_dep(graph, source)
    terminated = [False]

    def on_message(msg: Any, index: int, actions: NodeActions) -> bool:
        if terminated[0]:
            return True
        if index != 0:
            actions.down([msg])
            if msg[0] is MessageType.COMPLETE or msg[0] is MessageType.ERROR:
                terminated[0] = True
            return True
        if msg[0] is MessageType.DATA:
            try:
                run(msg[1], actions)
                actions.down([msg])
            except BaseException as err:
                terminated[0] = True
                actions.down([(MessageType.ERROR, err)])
            return True
        actions.down([msg])
        if msg[0] is MessageType.COMPLETE or msg[0] is MessageType.ERROR:
            terminated[0] = True
        return True

    step = node(
        [source_node],
        lambda _deps, _actions: None,
        name=name,
        describe_kind="effect",
        complete_when_deps_complete=False,
        on_message=on_message,
        meta=_base_meta("for_each", meta),
        **node_opts,
    )
    _register_step(graph, name, step, [source_path] if source_path else [])
    return step


def join(
    graph: Graph,
    name: str,
    deps: list[StepRef],
    *,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> Node[Any]:
    """Register a join step that emits the latest tuple of dependency values."""
    resolved = [_resolve_dep(graph, dep) for dep in deps]
    dep_nodes = [n for n, _ in resolved]
    dep_paths = [p for _n, p in resolved if p is not None]
    step = node(
        dep_nodes,
        lambda values, _a: tuple(values),
        name=name,
        describe_kind="derived",
        meta=_base_meta("join", meta),
        **node_opts,
    )
    _register_step(graph, name, step, dep_paths)
    return step


def loop(
    graph: Graph,
    name: str,
    source: StepRef,
    iterate: Callable[[Any, int, NodeActions], Any],
    *,
    iterations: int | StepRef = 1,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> Node[Any]:
    """Register a loop step that applies ``iterate`` N times per source value."""
    source_node, source_path = _resolve_dep(graph, source)
    iter_node: Node[Any] | None = None
    iter_path: str | None = None
    if isinstance(iterations, int):
        static_iterations = iterations
    else:
        static_iterations = None
        iter_node, iter_path = _resolve_dep(graph, iterations)

    def compute(_deps: list[Any], actions: NodeActions) -> Any:
        value = source_node.get()
        raw: Any = (
            static_iterations
            if static_iterations is not None
            else (iter_node.get() if iter_node is not None else 1)
        )
        count = _coerce_loop_iterations(raw)
        for i in range(count):
            value = iterate(value, i, actions)
        return value

    deps_list: list[Node[Any]] = [source_node] + ([iter_node] if iter_node is not None else [])
    step = node(
        deps_list,
        compute,
        name=name,
        describe_kind="derived",
        meta=_base_meta("loop", meta),
        **node_opts,
    )
    _register_step(graph, name, step, [p for p in (source_path, iter_path) if p is not None])
    return step


def sub_pipeline(
    graph: Graph,
    name: str,
    child_or_build: Graph | Callable[[Graph], None] | None = None,
    *,
    opts: dict[str, Any] | None = None,
) -> Graph:
    """Mount and return a child workflow graph."""
    child = child_or_build if isinstance(child_or_build, Graph) else pipeline(name, opts=opts)
    if callable(child_or_build):
        child_or_build(child)
    graph.mount(name, child)
    return child


def sensor(
    graph: Graph,
    name: str,
    *,
    initial: Any | None = None,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> SensorControls[Any]:
    """Register a producer-like sensor source and return imperative controls."""
    src = node(
        [],
        lambda _deps, _actions: None,
        name=name,
        initial=initial,
        describe_kind="producer",
        meta=_base_meta("sensor", meta),
        **node_opts,
    )
    _register_step(graph, name, src, [])
    return SensorControls(
        node=src,
        push=lambda value: src.down([(MessageType.DATA, value)]),
        error=lambda err: src.down([(MessageType.ERROR, err)]),
        complete=lambda: src.down([(MessageType.COMPLETE,)]),
    )


def wait(
    graph: Graph,
    name: str,
    source: StepRef,
    seconds: float,
    *,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> Node[Any]:
    """Register a delayed-forwarding step (value-level wait)."""
    source_node, source_path = _resolve_dep(graph, source)
    timers: set[threading.Timer] = set()
    lock = threading.Lock()
    terminated = [False]
    completed = [False]

    def clear_all() -> None:
        with lock:
            current = list(timers)
            timers.clear()
            terminated[0] = True
        for timer in current:
            timer.cancel()

    def on_message(msg: Any, index: int, actions: NodeActions) -> bool:
        if terminated[0]:
            return True
        if index != 0:
            actions.down([msg])
            if msg[0] is MessageType.COMPLETE or msg[0] is MessageType.ERROR:
                terminated[0] = True
            return True
        if msg[0] is MessageType.DATA:

            def fire() -> None:
                actions.down([msg])
                should_complete = False
                with lock:
                    timers.discard(timer)
                    should_complete = completed[0] and len(timers) == 0
                if should_complete:
                    actions.down([(MessageType.COMPLETE,)])

            timer = threading.Timer(seconds, fire)
            with lock:
                timers.add(timer)
            timer.start()
            return True
        if msg[0] is MessageType.COMPLETE:
            with lock:
                terminated[0] = True
                completed[0] = True
                done_now = len(timers) == 0
            if done_now:
                actions.down([(MessageType.COMPLETE,)])
            return True
        if msg[0] is MessageType.ERROR:
            clear_all()
            actions.down([msg])
            return True
        actions.down([msg])
        return True

    def compute(_deps: list[Any], _actions: NodeActions) -> Any:
        return clear_all

    step = node(
        [source_node],
        compute,
        name=name,
        initial=source_node.get(),
        describe_kind="operator",
        complete_when_deps_complete=False,
        on_message=on_message,
        meta=_base_meta("wait", meta),
        **node_opts,
    )
    _register_step(graph, name, step, [source_path] if source_path else [])
    return step


def on_failure(
    graph: Graph,
    name: str,
    source: StepRef,
    recover: Callable[[BaseException | Any, NodeActions], Any],
    *,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> Node[Any]:
    """Register an error-recovery step for a source."""
    source_node, source_path = _resolve_dep(graph, source)
    terminated = [False]

    def on_message(msg: Any, _index: int, actions: NodeActions) -> bool:
        if terminated[0]:
            return True
        if msg[0] is MessageType.ERROR:
            try:
                actions.emit(recover(msg[1], actions))
            except BaseException as err:
                terminated[0] = True
                actions.down([(MessageType.ERROR, err)])
            return True
        actions.down([msg])
        if msg[0] is MessageType.COMPLETE:
            terminated[0] = True
        return True

    step = node(
        [source_node],
        lambda _deps, _actions: None,
        name=name,
        describe_kind="operator",
        complete_when_deps_complete=False,
        on_message=on_message,
        meta=_base_meta("on_failure", meta),
        **node_opts,
    )
    _register_step(graph, name, step, [source_path] if source_path else [])
    return step


@dataclass(frozen=True, slots=True)
class GateController:
    """Result bundle for :func:`gate` — queue-based human approval primitive.

    Provides reactive state for the pending queue, count, and open/closed
    status, plus imperative controls to approve, reject, modify, open, and
    close the gate.
    """

    node: Node[Any]
    pending: Node[list[Any]]
    count: Node[int]
    is_open: Node[bool]
    approve: Callable[..., None]
    reject: Callable[..., None]
    modify: Callable[..., None]
    open: Callable[[], None]
    close: Callable[[], None]


def gate(
    graph: Graph,
    name: str,
    source: StepRef,
    *,
    max_pending: int | float = float("inf"),
    start_open: bool = False,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> GateController:
    """Register a queue-based human-in-the-loop approval gate.

    Incoming DATA values are enqueued in a pending list when the gate is
    closed. Callers can then :meth:`approve`, :meth:`reject`, or
    :meth:`modify` individual items. Calling :meth:`open` flushes pending
    items and auto-forwards future values; :meth:`close` re-enables manual
    gating.

    Args:
        graph: The orchestration graph to register nodes in.
        name: Base name for the gate nodes.
        source: Upstream step (string path or node).
        max_pending: Maximum pending queue size (must be >= 1).
        start_open: If ``True`` the gate starts in auto-forward mode.
        meta: Extra metadata merged onto the orchestration meta.
        **node_opts: Additional node options.

    Returns:
        A :class:`GateController` with reactive state and imperative controls.

    Raises:
        ValueError: If *max_pending* < 1.
    """
    if max_pending < 1:
        msg = "gate: max_pending must be >= 1"
        raise ValueError(msg)

    source_node, source_path = _resolve_dep(graph, source)

    # Internal mutable state protected by a lock.
    lock = threading.Lock()
    queue: list[Any] = []
    open_flag = [start_open]
    torn = [False]

    # Reactive state nodes ------------------------------------------------
    pending_node: Node[list[Any]] = state(list(queue), name=f"{name}::pending")
    is_open_node: Node[bool] = state(start_open, name=f"{name}::is_open")
    count_node: Node[int] = derived(
        [pending_node],
        lambda deps, _a: len(deps[0]) if isinstance(deps[0], list) else 0,
        name=f"{name}::count",
        initial=0,
    )

    def _sync_pending() -> None:
        """Push internal queue state into reactive pending node."""
        pending_node.down([(MessageType.DATA, list(queue))])

    def _sync_open() -> None:
        """Push internal open state into reactive is_open node."""
        is_open_node.down([(MessageType.DATA, open_flag[0])])

    # Output node with on_message intercept --------------------------------
    def on_message(msg: Any, index: int, actions: NodeActions) -> bool:
        if index != 0:
            actions.down([msg])
            return True

        mtype = msg[0]

        if mtype is MessageType.DATA:
            with lock:
                if torn[0]:
                    return True
                if open_flag[0]:
                    # Auto-forward mode.
                    actions.down([msg])
                    return True
                # Enqueue the value (FIFO-evict oldest if over capacity).
                queue.append(msg[1])
                if len(queue) > max_pending:
                    queue.pop(0)
                _sync_pending()
            actions.down([(MessageType.RESOLVED,)])
            return True

        if mtype in (MessageType.TEARDOWN, MessageType.COMPLETE, MessageType.ERROR):
            with lock:
                torn[0] = True
                queue.clear()
                _sync_pending()
            actions.down([msg])
            return True

        # Forward all other message types unchanged.
        actions.down([msg])
        return True

    output_node = node(
        [source_node],
        lambda _deps, _actions: None,
        name=name,
        describe_kind="operator",
        complete_when_deps_complete=False,
        on_message=on_message,
        meta=_base_meta("gate", meta),
        **node_opts,
    )

    # Register output node and internal state sub-graph.
    _register_step(graph, name, output_node, [source_path] if source_path else [])
    internal = Graph(f"{name}_state")
    internal.add("pending", pending_node)
    internal.add("is_open", is_open_node)
    internal.add("count", count_node)
    internal.connect("pending", "count")
    graph.mount(f"{name}_state", internal)

    # Imperative controls --------------------------------------------------
    def _assert_not_torn(method: str) -> None:
        if torn[0]:
            msg = f"gate: {method}() called after gate was torn down"
            raise RuntimeError(msg)

    def _approve(count: int = 1) -> None:
        with lock:
            _assert_not_torn("approve")
            to_send = queue[:count]
            del queue[:count]
            _sync_pending()
        for item in to_send:
            if torn[0]:
                break
            output_node.down([(MessageType.DATA, item)])

    def _reject(count: int = 1) -> None:
        with lock:
            _assert_not_torn("reject")
            del queue[:count]
            _sync_pending()

    def _modify(fn: Callable[..., Any], count: int = 1) -> None:
        with lock:
            _assert_not_torn("modify")
            pending_snapshot = list(queue)
            to_process = queue[:count]
            del queue[:count]
            _sync_pending()
        for idx, item in enumerate(to_process):
            if torn[0]:
                break
            transformed = fn(item, idx, pending_snapshot)
            output_node.down([(MessageType.DATA, transformed)])

    def _open() -> None:
        with lock:
            _assert_not_torn("open")
            open_flag[0] = True
            to_flush = list(queue)
            queue.clear()
            _sync_pending()
            _sync_open()
        for item in to_flush:
            if torn[0]:
                break
            output_node.down([(MessageType.DATA, item)])

    def _close() -> None:
        with lock:
            _assert_not_torn("close")
            open_flag[0] = False
            _sync_open()

    return GateController(
        node=output_node,
        pending=pending_node,
        count=count_node,
        is_open=is_open_node,
        approve=_approve,
        reject=_reject,
        modify=_modify,
        open=_open,
        close=_close,
    )


__all__ = [
    "BranchResult",
    "GateController",
    "SensorControls",
    "StepRef",
    "approval",
    "branch",
    "for_each",
    "gate",
    "valve",
    "join",
    "loop",
    "on_failure",
    "pipeline",
    "sensor",
    "sub_pipeline",
    "task",
    "wait",
]
