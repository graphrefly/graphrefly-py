"""Checkpoint and restore over the Python kernel (R-checkpoint, graphrefly:D802).

``capture(graph)`` saves an ordinary kernel graph at a wave boundary and keeps no
record of its own: it reads the topology, each node's definition and recipe, what
each node holds, its private state, what it received from each dependency and the
dependencies of a lifetime a ``ctx.rewire`` changed. ``restore(checkpoint, registry)``
builds a new graph with no subscribers, each node after its declared dependencies,
and runs no recipe function: a state gets its saved values as one array and then its
terminal; a computed node gets them as a kernel seed whose note holds what it had
received. At a restored node's first activation its setup runs, as at every
activation, and its function runs once only when a dependency no longer holds what it
had received, with only the changed dependencies as input.

A recipe is ``{"ref": name, "config": json}``. A factory registered under ``name`` is
called as ``factory(config, RecipeContext(graph, id, make))`` and returns the node
function, or a :class:`Built` with a function and a setup. The layout below is this
package's (R-checkpoint leaves layout to each package); it is plain JSON.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import wraps
from typing import Any, Final, cast

from graphrefly._kernel import COMPLETE, Ctx, Dep, Graph, KernelError, Message
from graphrefly._kernel.graph import NodeFn, Reporter, Seed, Setup

FORMAT: Final = "graphrefly-py/checkpoint@1"

type Checkpoint = dict[str, Any]


class CheckpointError(Exception):
    """A capture or restore that R-checkpoint refuses."""


@dataclass(frozen=True, slots=True)
class Built:
    """What a recipe factory builds: the node's function and, optionally, its setup."""

    fn: NodeFn
    setup: Setup | None = None


@dataclass(frozen=True, slots=True)
class RecipeContext:
    """What a factory is given besides its configuration: the graph being built, the
    node's id, and ``make(ref, config, id)`` to build another registered recipe."""

    graph: Graph
    id: str
    make: Callable[[str, object, str], Built]


type Factory = Callable[[Any, RecipeContext], NodeFn | Built]


def recipe(ref: str, config: object = None) -> dict[str, object]:
    """The opaque recipe a restorable node carries: a factory reference and a JSON config."""

    return {"ref": ref, "config": config}


# ---- capture -------------------------------------------------------------------


def _json(value: object, where: str, op: str = "capture") -> Any:
    """A JSON copy of ``value``: the checkpoint is a snapshot at the wave boundary, so it
    shares no object with the live graph, nor a restored graph with its checkpoint."""

    try:
        text = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise CheckpointError(f"{op}: {where} is not JSON") from None
    copy = json.loads(text)
    if copy != value:
        raise CheckpointError(f"{op}: {where} is not JSON")
    return copy


def _not_restorable(node_id: str, reason: str) -> CheckpointError:
    """The one form of a node-level refusal: ``not restorable: <id>: <reason>``."""

    return CheckpointError(f"not restorable: {node_id}: {reason}")


def _check(node_id: str, value: object, reason: str) -> None:
    try:
        _json(value, node_id)
    except CheckpointError:
        raise _not_restorable(node_id, reason) from None


def _ended(message: Message | None) -> list[object] | None:
    return None if message is None else list(message)


def _received(graph: Graph, deps: Sequence[str]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for dep_id in deps:
        held = graph.get(dep_id)
        entry: dict[str, object] = {"has": held.has}
        if held.has:
            entry["values"] = list(held.values)
        entry["ended"] = _ended(held.ended)
        out.append(entry)
    return out


def capture(graph: Graph) -> Checkpoint:
    """Save ``graph`` at a wave boundary; refused inside a wave or a batch body."""

    if not graph.idle():
        raise CheckpointError("capture: a wave or batch is running; capture at a wave boundary")
    nodes: list[dict[str, object]] = []
    for described in graph.describe():
        node_id = described.id
        held = graph.get(node_id)
        if described.kind == "state":
            saved = {"values": list(held.values), "ended": _ended(held.ended)}
            _check(node_id, saved["values"], "a value it holds is not JSON")
            _check(node_id, saved["ended"], "its ERROR payload is not JSON")
            nodes.append(
                _json(
                    {"id": node_id, "kind": "state", "deps": [], "saved": saved}, f"node {node_id}"
                )
            )
            continue
        definition = graph.definition(node_id)
        declared = list(definition.deps)
        if definition.recipe is None:
            nodes.append({"id": node_id, "kind": "node", "deps": declared, "local": True})
            continue
        entry: dict[str, object] = {
            "id": node_id,
            "kind": "node",
            "deps": declared,
            "recipe": definition.recipe,
            "policies": {
                "complete": definition.complete,
                "error": definition.error,
                "invalidate": definition.invalidate,
                "quiet": definition.quiet,
            },
        }
        state = graph.private_state(node_id)
        node_saved: dict[str, object] | None = None
        if isinstance(held.seed, dict):
            # Restored and not activated since: what it holds is its seed, saved again.
            node_saved = {"values": list(held.values), "ended": _ended(held.ended), **held.seed}
        elif held.active or held.ended is not None:
            current = list(described.deps)
            node_saved = {
                "values": list(held.values),
                "ended": _ended(held.ended),
                "inputs": _received(graph, current),
            }
            if current != declared:
                node_saved["deps"] = current  # a ctx.rewire's dependencies come back with it
        _check(node_id, definition.recipe, "its recipe is not JSON")
        if node_saved is not None:
            _check(node_id, node_saved, "a value it holds or received is not JSON")
            if state is not None:
                _check(node_id, state, "its private state is not JSON")
                node_saved["state"] = state
            entry["saved"] = node_saved
        nodes.append(_json(entry, f"node {node_id}"))
    return {"format": FORMAT, "nodes": nodes}


# ---- restore -------------------------------------------------------------------


class _Unchanged(Dep):
    """A dependency that brings nothing new to a restored node's first run, or only its end."""

    __slots__ = ("_end",)

    def __init__(self, dep: Dep, end: Message | None) -> None:
        super().__init__(dep.id, dep._input, dep._manual_invalidate)
        self._end = end

    @property
    def waves(self) -> list[list[object]]:
        return []

    @property
    def sent_data(self) -> bool:
        return False

    @property
    def terminal(self) -> Message | None:
        return self._end


def _same_value(dep: Dep, was: Mapping[str, Any]) -> bool:
    if dep.has != was.get("has"):
        return False
    if not dep.has:
        return True
    waves = dep.waves
    return (waves[-1] if waves else []) == was.get("values")


def _restorable(real: NodeFn) -> NodeFn:
    """The restored node's function: on the seeded instance's first run, compare each
    dependency with what the node had received and run ``real`` only on a change, with
    only the changed dependencies; on every other run it is ``real``."""

    @wraps(real)  # what the host attached to its function stays readable (definition)
    def run(ctx: Ctx) -> object:
        note = ctx.seed
        if not isinstance(note, dict) or "inputs" not in note:
            return real(ctx)
        changed = False
        deps: list[Dep] = []
        for dep, was in zip(ctx.deps, note["inputs"], strict=True):
            value_changed = not _same_value(dep, was)
            terminal = dep.terminal
            end_changed = (None if terminal is None else list(terminal)) != was.get("ended")
            changed = changed or value_changed or end_changed
            deps.append(
                dep if value_changed else _Unchanged(dep, terminal if end_changed else None)
            )
        if not changed:
            return None  # the seed holds the value and the private state
        ctx._deps = deps
        try:
            return real(ctx)
        finally:
            ctx._deps = None

    return run


def _build_order(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each node after its declared dependencies, in checkpoint order otherwise; a stack,
    not recursion, so a long chain needs no deep Python stack."""

    by_id = {node["id"]: node for node in nodes}
    done: set[str] = set()
    order: list[dict[str, Any]] = []
    for first in nodes:
        if first["id"] in done:
            continue
        path: set[str] = {first["id"]}
        stack: list[tuple[dict[str, Any], int]] = [(first, 0)]
        while stack:
            node, index = stack[-1]
            deps = node["deps"]
            if index < len(deps):
                stack[-1] = (node, index + 1)
                dep_id = deps[index]
                if dep_id not in by_id:
                    raise CheckpointError(
                        f"restore: {node['id']} depends on {dep_id}, which is not in the checkpoint"
                    )
                if dep_id in done:
                    continue
                if dep_id in path:
                    raise CheckpointError(f"restore: {dep_id} depends on itself")
                path.add(dep_id)
                stack.append((by_id[dep_id], 0))
                continue
            stack.pop()
            path.discard(node["id"])
            done.add(node["id"])
            order.append(node)
    return order


def _message(saved: object) -> Message | None:
    if saved is None:
        return None
    assert isinstance(saved, list)
    return tuple(saved)


def restore(
    checkpoint: Checkpoint,
    registry: Mapping[str, Factory],
    *,
    report: Reporter | None = None,
    into: Graph | None = None,
) -> Graph:
    """Build a new graph from ``checkpoint``; no subscriber, no recipe function runs.

    ``into`` is an empty graph to build into, for a host that made the graph itself.
    """

    if not isinstance(checkpoint, dict) or checkpoint.get("format") != FORMAT:
        raise CheckpointError(f"restore: not a {FORMAT} checkpoint")
    checkpoint = _json(checkpoint, "the checkpoint", "restore")
    nodes = cast("list[dict[str, Any]]", checkpoint.get("nodes"))
    if not isinstance(nodes, list):
        raise CheckpointError("restore: the checkpoint has no nodes")
    if into is not None and into.describe():
        raise CheckpointError("restore: the graph to restore into is not empty")
    graph = into if into is not None else Graph(report=report)

    def make(ref: str, config: object, node_id: str) -> Built:
        factory = registry.get(ref)
        if factory is None:
            raise CheckpointError(f"restore: no recipe {ref}")
        built = factory(config, RecipeContext(graph, node_id, make))
        return built if isinstance(built, Built) else Built(built)

    for node in nodes:
        if node.get("local"):
            raise _not_restorable(str(node["id"]), "local-only, built or replaced without a recipe")
    for node in nodes:
        saved = node.get("saved")
        if node["kind"] == "state" or not saved or saved.get("ended"):
            continue
        deps = saved.get("deps", node["deps"])
        inputs = saved.get("inputs")
        if not isinstance(inputs, list) or len(inputs) != len(deps):
            raise CheckpointError(
                f"restore: {node['id']} has no record of what it received; "
                "the checkpoint is not in this format"
            )

    try:
        for node in _build_order(nodes):
            _restore_node(graph, node, make)
    except KernelError as error:
        raise CheckpointError(f"restore: {error}") from error
    return graph


def _restore_node(
    graph: Graph, node: dict[str, Any], make: Callable[[str, object, str], Built]
) -> None:
    node_id: str = node["id"]
    saved: dict[str, Any] | None = node.get("saved")
    if node["kind"] == "state":
        graph.state(node_id)
        if saved is None:
            return
        values: list[object] = saved.get("values") or []
        if values:
            # Its last array's values come back as one array, with no subscriber to see it.
            def body() -> None:
                for value in values:
                    graph.set(node_id, value)

            graph.batch(body)
        ended = _message(saved.get("ended"))
        if ended is not None:
            if ended[0] == COMPLETE:
                graph.complete(node_id)
            else:
                graph.error(node_id, ended[1])
        return
    recipe_value = node["recipe"]
    built = make(recipe_value["ref"], recipe_value.get("config"), node_id)
    policies = node.get("policies", {})
    seed: Seed | None = None
    if saved is not None:
        ended = _message(saved.get("ended"))
        note: dict[str, object] | None = None
        if ended is None:  # an ended node never runs again, so it gets no note
            note = {"inputs": saved["inputs"]}
            if "deps" in saved:
                note["deps"] = saved["deps"]
        seed = Seed(
            values=saved.get("values") or [],
            ended=ended,
            state=saved.get("state"),
            deps=saved.get("deps"),
            note=note,
        )
    graph.node(
        node_id,
        node["deps"],
        _restorable(built.fn) if saved is not None else built.fn,
        complete=policies.get("complete", "auto"),
        error=policies.get("error", "auto"),
        invalidate=policies.get("invalidate", "auto"),
        quiet=bool(policies.get("quiet", False)),
        setup=built.setup,
        recipe=recipe_value,
        seed=seed,
    )


__all__ = [
    "FORMAT",
    "Built",
    "Checkpoint",
    "CheckpointError",
    "Factory",
    "RecipeContext",
    "capture",
    "recipe",
    "restore",
]
