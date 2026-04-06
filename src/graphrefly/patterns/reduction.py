"""Reduction primitives (roadmap §8.1).

Composable building blocks for taking heterogeneous massive inputs and producing
prioritized, auditable, human-actionable output. Each primitive is either a Graph
factory or a Node factory, built on top of core + extra primitives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

from graphrefly.core.node import NodeImpl, node
from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import derived, state
from graphrefly.extra.tier1 import merge
from graphrefly.graph.graph import GRAPH_META_SEGMENT, Graph

# ---------------------------------------------------------------------------
# Shared helpers (mirrors orchestration.py)
# ---------------------------------------------------------------------------

type StepRef = str | NodeImpl[Any]


def _resolve_dep(graph: Graph, dep: StepRef) -> tuple[NodeImpl[Any], str | None]:
    """Resolve a StepRef to a (node, path) pair."""
    if isinstance(dep, str):
        return graph.resolve(dep), dep
    path = _find_registered_node_path(graph, dep)
    if path is None:
        msg = (
            "reduction dep node must already be registered in the graph "
            "so explicit edges can be recorded; pass a string path or "
            "register the node first"
        )
        raise ValueError(msg)
    return dep, path


def _find_registered_node_path(graph: Graph, target: NodeImpl[Any]) -> str | None:
    """Find the registered path for a node in the graph."""
    described = graph.describe()
    meta_seg = f"::{GRAPH_META_SEGMENT}::"
    nodes: dict[str, Any] = described.get("nodes") or {}
    for path in sorted(nodes.keys()):
        if meta_seg in path:
            continue
        try:
            if graph.resolve(path) is target:
                return path
        except Exception:  # noqa: BLE001
            pass
    return None


def _register_step(
    graph: Graph,
    step_name: str,
    step: NodeImpl[Any],
    dep_paths: list[str],
) -> None:
    """Register step and record edges explicitly."""
    graph.add(step_name, step)
    for dep_path in dep_paths:
        graph.connect(dep_path, step_name)


def _base_meta(kind: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge reduction metadata."""
    out: dict[str, Any] = {"reduction": True, "reduction_type": kind}
    if meta:
        out.update(meta)
    return out


# ---------------------------------------------------------------------------
# stratify
# ---------------------------------------------------------------------------


class StratifyRule:
    """A single routing rule for :func:`stratify`.

    Args:
        name: Branch name (used as node name under ``branch/<name>``).
        classify: Classifier returning ``True`` if the value belongs to this branch.
        ops: Optional operator chain applied after classification.
    """

    __slots__ = ("classify", "name", "ops")

    def __init__(
        self,
        name: str,
        classify: Callable[[Any], bool],
        ops: Callable[[NodeImpl[Any]], NodeImpl[Any]] | None = None,
    ) -> None:
        self.name = name
        self.classify = classify
        self.ops = ops


def stratify(
    name: str,
    source: NodeImpl[Any],
    rules: Sequence[StratifyRule],
    **graph_opts: Any,
) -> Graph:
    """Route input to different reduction branches based on classifier functions.

    Each branch gets an independent operator chain. Rules are reactive — update
    the ``"rules"`` state node to rewrite classification at runtime. Rule updates
    affect **future items only** (streaming classification, not retroactive).

    Branch nodes are structural — created at construction time and persist for
    the graph's lifetime. If a rule name is removed from the rules array, the
    corresponding branch silently drops items (classifier not found). To tear
    down a dead branch, call ``graph.remove("branch/<name>")``.

    Args:
        name: Graph name.
        source: Input node.
        rules: Initial routing rules.
        **graph_opts: Passed to :class:`Graph`.

    Returns:
        Graph with ``"source"``, ``"rules"``, and ``"branch/<name>"`` nodes.
    """
    g = Graph(name, **graph_opts)
    g.add("source", source)

    rules_node = state(list(rules), meta=_base_meta("stratify_rules"))
    g.add("rules", rules_node)

    for rule in rules:
        _add_branch(g, source, rules_node, rule)

    return g


def _add_branch(
    graph: Graph,
    source: NodeImpl[Any],
    rules_node: NodeImpl[Any],
    rule: StratifyRule,
) -> None:
    """Add a stratify branch to the graph.

    Protocol: DIRTY is buffered until DATA arrives. If the classifier matches,
    emit [DIRTY, DATA]. If not, emit [DIRTY, RESOLVED] so downstream exits
    dirty status cleanly (spec §1.3.1). Source RESOLVED forwards as RESOLVED.
    """
    branch_name = f"branch/{rule.name}"
    pending_dirty = [False]

    def on_message(msg: Any, dep_index: int, actions: Any) -> bool:
        # Only intercept source messages (dep 0)
        if dep_index != 0:
            return False

        t = msg[0]
        if t is MessageType.DATA:
            value = msg[1]
            current_rules: list[StratifyRule] = rules_node.get() or []
            current_rule = next((r for r in current_rules if r.name == rule.name), None)
            if current_rule is not None and current_rule.classify(value):
                # Match: emit (actions.emit handles DIRTY+DATA wrapping)
                pending_dirty[0] = False
                actions.emit(value)
            else:
                # No match: emit DIRTY + RESOLVED so downstream exits dirty
                if pending_dirty[0]:
                    pending_dirty[0] = False
                    actions.down([(MessageType.DIRTY,), (MessageType.RESOLVED,)])
            return True
        if t is MessageType.DIRTY:
            pending_dirty[0] = True
            return True
        if t is MessageType.RESOLVED:
            # Source unchanged — forward RESOLVED (with buffered DIRTY if any)
            if pending_dirty[0]:
                pending_dirty[0] = False
                actions.down([(MessageType.DIRTY,), (MessageType.RESOLVED,)])
            else:
                actions.down([(MessageType.RESOLVED,)])
            return True
        if t in (MessageType.COMPLETE, MessageType.ERROR):
            pending_dirty[0] = False
            actions.down([msg])
            return True
        return False

    filter_node = node(
        [source, rules_node],
        lambda _d, _a: None,
        on_message=on_message,
        describe_kind="operator",
        meta=_base_meta("stratify_branch", {"branch": rule.name}),
    )

    graph.add(branch_name, filter_node)
    graph.connect("source", branch_name)

    # If the rule has an ops chain, apply it and connect the edge
    if rule.ops is not None:
        transformed = rule.ops(filter_node)
        transformed_name = f"branch/{rule.name}/out"
        graph.add(transformed_name, transformed)
        graph.connect(branch_name, transformed_name)


# ---------------------------------------------------------------------------
# funnel
# ---------------------------------------------------------------------------


class FunnelStage:
    """A named stage for :func:`funnel`.

    Args:
        name: Stage name (mounted as subgraph).
        build: Builder receiving a sub-graph; must add ``"input"`` and ``"output"`` nodes.
    """

    __slots__ = ("build", "name")

    def __init__(self, name: str, build: Callable[[Graph], None]) -> None:
        self.name = name
        self.build = build


def funnel(
    name: str,
    sources: Sequence[NodeImpl[Any]],
    stages: Sequence[FunnelStage],
    **graph_opts: Any,
) -> Graph:
    """Multi-source merge with sequential reduction stages.

    Sources are merged into a single stream. Each stage is a named subgraph.
    Stages connect linearly: ``merged → stage[0].input → stage[0].output → ...``

    Args:
        name: Graph name.
        sources: Input nodes to merge.
        stages: Sequential reduction stages.
        **graph_opts: Passed to :class:`Graph`.

    Returns:
        Graph with ``"merged"`` and mounted stage subgraphs.
    """
    if len(sources) == 0:
        msg = "funnel requires at least one source"
        raise ValueError(msg)
    if len(stages) == 0:
        msg = "funnel requires at least one stage"
        raise ValueError(msg)

    g = Graph(name, **graph_opts)

    merged = sources[0] if len(sources) == 1 else merge(*sources)
    g.add("merged", merged)

    prev_output_path = "merged"
    for stage in stages:
        sub = Graph(stage.name)
        stage.build(sub)

        # Validate input/output
        try:
            sub.resolve("input")
        except Exception:
            msg = f'funnel stage "{stage.name}" must define an "input" node'
            raise ValueError(msg) from None
        try:
            sub.resolve("output")
        except Exception:
            msg = f'funnel stage "{stage.name}" must define an "output" node'
            raise ValueError(msg) from None

        g.mount(stage.name, sub)

        # Bridge: subscribe prev output → forward messages to stage input.
        # Forwards DIRTY, DATA, RESOLVED, COMPLETE, ERROR to preserve
        # two-phase protocol across stages.
        # TODO(8.2): replace with graph-visible bridge nodes once the
        # core bridge() helper lands.
        prev_node = g.resolve(prev_output_path)
        stage_input = g.resolve(f"{stage.name}::input")

        def _make_bridge(target: NodeImpl[Any]) -> Callable[[Any], None]:
            def _bridge(msgs: Any) -> None:
                for m in msgs:
                    t = m[0]
                    if t is MessageType.DATA:
                        target.down([(MessageType.DATA, m[1])])
                    elif t is MessageType.DIRTY:
                        target.down([(MessageType.DIRTY,)])
                    elif t is MessageType.RESOLVED:
                        target.down([(MessageType.RESOLVED,)])
                    elif t in (MessageType.COMPLETE, MessageType.ERROR):
                        target.down([m])

            return _bridge

        prev_node.subscribe(_make_bridge(stage_input))
        prev_output_path = f"{stage.name}::output"

    return g


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------


def feedback(
    graph: Graph,
    condition: str,
    reentry: str,
    *,
    max_iterations: int = 10,
    meta: dict[str, Any] | None = None,
) -> Graph:
    """Introduce a bounded reactive cycle into an existing graph.

    When ``condition`` emits non-null DATA, routes it back to the ``reentry``
    state node. Bounded by ``max_iterations``. The counter node
    (``__feedback_<condition>``) is the source of truth — reset it to 0 to
    allow more iterations.

    To remove the feedback cycle, call
    ``graph.remove("__feedback_<condition>")``.

    Args:
        graph: Existing graph to augment.
        condition: Path to a node whose DATA triggers feedback.
        reentry: Path to a state node that receives the feedback value.
        max_iterations: Maximum feedback iterations (default 10).
        meta: Optional metadata.

    Returns:
        The same graph (mutated with feedback nodes added).
    """
    counter_name = f"__feedback_{condition}"
    counter = state(0, meta=_base_meta("feedback_counter", {"max_iterations": max_iterations}))
    graph.add(counter_name, counter)

    cond_node = graph.resolve(condition)
    reentry_node = graph.resolve(reentry)

    def _on_condition(msgs: Any) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                current_count = int(counter.get() or 0)
                if current_count >= max_iterations:
                    continue
                cond_value = msg[1]
                if cond_value is None:
                    continue
                counter.down([(MessageType.DATA, current_count + 1)])
                reentry_node.down([(MessageType.DATA, cond_value)])

    cond_node.subscribe(_on_condition)

    return graph


# ---------------------------------------------------------------------------
# budget_gate
# ---------------------------------------------------------------------------


class BudgetConstraint:
    """A reactive constraint for :func:`budget_gate`.

    Args:
        node: Constraint node whose value is checked.
        check: Returns ``True`` when the constraint is satisfied (budget available).
    """

    __slots__ = ("check", "node")

    def __init__(self, node: NodeImpl[Any], check: Callable[[Any], bool]) -> None:
        self.node = node
        self.check = check


def budget_gate(
    source: NodeImpl[Any],
    constraints: Sequence[BudgetConstraint],
    *,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> NodeImpl[Any]:
    """Pass-through respecting reactive constraint nodes.

    DATA flows when all constraints are satisfied. When any is exceeded,
    PAUSE is sent upstream and DATA is buffered. On replenish, RESUME and flush.

    Args:
        source: Input node.
        constraints: Reactive constraint checks.
        meta: Optional metadata.
        **node_opts: Passed to :func:`node`.

    Returns:
        Gated node.
    """
    if len(constraints) == 0:
        msg = "budget_gate requires at least one constraint"
        raise ValueError(msg)

    constraint_nodes = [c.node for c in constraints]
    all_deps: list[NodeImpl[Any]] = [source, *constraint_nodes]

    buffer: list[Any] = []
    paused = [False]
    lock_id = object()

    def check_budget() -> bool:
        return all(c.check(c.node.get()) for c in constraints)

    def flush_buffer(actions: Any) -> None:
        while buffer and check_budget():
            item = buffer.pop(0)
            actions.emit(item)

    def on_message(msg: Any, dep_index: int, actions: Any) -> bool:
        t = msg[0]

        # Source messages (dep 0)
        if dep_index == 0:
            if t is MessageType.DATA:
                if check_budget() and not buffer:
                    actions.emit(msg[1])
                else:
                    buffer.append(msg[1])
                    if not paused[0]:
                        paused[0] = True
                        actions.up([(MessageType.PAUSE, lock_id)])
                return True
            if t is MessageType.DIRTY:
                actions.down([(MessageType.DIRTY,)])
                return True
            if t is MessageType.RESOLVED:
                if not buffer:
                    actions.down([(MessageType.RESOLVED,)])
                return True
            if t in (MessageType.COMPLETE, MessageType.ERROR):
                # Force-flush all buffered items regardless of budget (terminal = done)
                for item in list(buffer):
                    actions.emit(item)
                buffer.clear()
                # Release PAUSE lock before forwarding terminal
                if paused[0]:
                    paused[0] = False
                    actions.up([(MessageType.RESUME, lock_id)])
                actions.down([msg])
                return True
            return False

        # Constraint node messages (dep 1+)
        if t in (MessageType.DATA, MessageType.RESOLVED):
            if check_budget() and buffer:
                flush_buffer(actions)
                if not buffer and paused[0]:
                    paused[0] = False
                    actions.up([(MessageType.RESUME, lock_id)])
            elif not check_budget() and not paused[0] and buffer:
                paused[0] = True
                actions.up([(MessageType.PAUSE, lock_id)])
            return True
        if t is MessageType.DIRTY:
            return True
        if t is MessageType.ERROR:
            # Constraint error → forward downstream
            actions.down([msg])
            return True
        # Constraint COMPLETE — locked at last value, no-op.
        # Unknown constraint types → default forwarding.
        return t is MessageType.COMPLETE

    return node(
        all_deps,
        lambda _d, _a: None,
        on_message=on_message,
        describe_kind="operator",
        meta=_base_meta("budget_gate", meta),
        **node_opts,
    )


# ---------------------------------------------------------------------------
# scorer
# ---------------------------------------------------------------------------


class ScoredItem:
    """A scored item with full breakdown.

    Attributes:
        value: Original signal values.
        score: Final weighted score.
        breakdown: Per-signal weighted contributions.
    """

    __slots__ = ("breakdown", "score", "value")

    def __init__(self, value: list[Any], score: float, breakdown: list[float]) -> None:
        self.value = value
        self.score = score
        self.breakdown = breakdown

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ScoredItem):
            return NotImplemented
        return (
            self.value == other.value
            and self.score == other.score
            and self.breakdown == other.breakdown
        )

    def __repr__(self) -> str:
        return f"ScoredItem(value={self.value}, score={self.score}, breakdown={self.breakdown})"


def scorer(
    sources: Sequence[NodeImpl[Any]],
    weights: Sequence[NodeImpl[Any]],
    *,
    score_fns: Sequence[Callable[[Any], float]] | None = None,
    meta: dict[str, Any] | None = None,
    **node_opts: Any,
) -> NodeImpl[Any]:
    """Reactive multi-signal scoring with live weights.

    Each source emits a numeric score dimension. Weights are reactive state nodes.
    Output: :class:`ScoredItem` with sorted scores and full breakdown.

    Missing values (``None``) are coerced to ``0``.

    Args:
        sources: Signal nodes (each emits a numeric score).
        weights: Reactive weight nodes (one per source).
        score_fns: Optional per-signal scoring transforms.
        meta: Optional metadata.
        **node_opts: Passed to :func:`derived`.

    Returns:
        Node emitting :class:`ScoredItem`.
    """
    if len(sources) == 0:
        msg = "scorer requires at least one source"
        raise ValueError(msg)
    if len(sources) != len(weights):
        msg = "scorer requires the same number of sources and weights"
        raise ValueError(msg)

    n = len(sources)
    all_deps: list[NodeImpl[Any]] = [*sources, *weights]

    def compute(vals: list[Any], _actions: Any) -> ScoredItem:
        signals = vals[:n]
        weight_values = vals[n:]

        breakdown: list[float] = []
        total_score = 0.0

        for i in range(n):
            sig = signals[i] if signals[i] is not None else 0
            wt = weight_values[i] if weight_values[i] is not None else 0
            raw = score_fns[i](sig) if score_fns else float(sig)
            weighted = raw * float(wt)
            breakdown.append(weighted)
            total_score += weighted

        return ScoredItem(
            value=[(s if s is not None else 0) for s in signals],
            score=total_score,
            breakdown=breakdown,
        )

    return derived(
        all_deps,
        compute,
        meta=_base_meta("scorer", meta),
        **node_opts,
    )


__all__ = [
    "BudgetConstraint",
    "FunnelStage",
    "ScoredItem",
    "StratifyRule",
    "budget_gate",
    "feedback",
    "funnel",
    "scorer",
    "stratify",
]
