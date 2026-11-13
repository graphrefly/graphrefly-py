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
from typing import Any, Final

from graphrefly._kernel import COMPLETE, FATAL, Ctx, Dep, Graph, Message
from graphrefly._kernel.graph import BOUNDARY_NAME_ERROR, NAME_ERROR, NodeFn, Reporter, Seed, Setup

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


def _check_saved(node_id: str, saved: Mapping[str, Any]) -> None:
    """Attribute non-JSON contents to the same node and reason on capture and restore."""

    _check(node_id, saved.get("values"), "its retained values are not JSON")
    _check(node_id, saved.get("state"), "its private state is not JSON")
    _check(node_id, saved.get("ended"), "its ERROR payload is not JSON")
    inputs = saved.get("inputs")
    for received in inputs if isinstance(inputs, list) else []:
        if not isinstance(received, dict):
            continue  # restore's received-record admission reports the structural refusal
        _check(node_id, received.get("values"), "its received values are not JSON")
        _check(node_id, received.get("ended"), "its ERROR payload is not JSON")


def _ended(message: Message | None) -> list[object] | None:
    return None if message is None else list(message)


def _received(graph: Graph, deps: Sequence[str]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for dep_id in deps:
        held = graph.get(dep_id)
        entry: dict[str, object] = {"has": held.has, "values": list(held.values)}
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
        _check(node_id, list(held.values), "its retained values are not JSON")
        _check(node_id, _ended(held.ended), "its ERROR payload is not JSON")
        state = graph.private_state(node_id)
        if state is not None:
            _check(node_id, state, "its private state is not JSON")
        if described.kind == "state":
            saved = {"values": list(held.values), "ended": _ended(held.ended)}
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
        _check(node_id, definition.recipe, "its recipe config is not JSON")
        if node_saved is not None:
            _check_saved(node_id, node_saved)
            if state is not None:
                node_saved["state"] = state
        entry["saved"] = node_saved  # explicit null marks a cold node, not a truncated lifetime
        nodes.append(_json(entry, f"node {node_id}"))
    return {"format": FORMAT, "nodes": nodes}


# ---- restore -------------------------------------------------------------------


class _Unchanged(Dep):
    """A dependency that brings nothing new to a restored node's first run, or only its end."""

    __slots__ = ("_end",)

    def __init__(self, dep: Dep, end: Message | None) -> None:
        super().__init__(dep.id, dep._input, dep._manual_invalidate, dep._ctx)
        self._end = end

    @property
    def waves(self) -> list[list[object]]:
        self._ctx._check_deps()
        return []

    @property
    def sent_data(self) -> bool:
        self._ctx._check_deps()
        return False

    @property
    def terminal(self) -> Message | None:
        self._ctx._check_deps()
        return self._end


def _same_value(dep: Dep, was: Mapping[str, Any]) -> bool:
    if dep.has != was["has"]:
        return False
    if not dep.has:
        return True
    waves = dep.waves
    return bool((waves[-1] if waves else []) == was["values"])


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
            end_changed = (None if terminal is None else list(terminal)) != was["ended"]
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
                    raise _not_restorable(
                        str(node["id"]), f"depends on {dep_id}, which is not in the checkpoint"
                    )
                if dep_id in done:
                    continue
                if dep_id in path:
                    raise _not_restorable(str(dep_id), "depends on itself")
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


@dataclass(frozen=True, slots=True)
class _Admitted:
    nodes: list[dict[str, Any]]


def _terminal_valid(value: object) -> bool:
    return value is None or (
        isinstance(value, list)
        and (
            value == [COMPLETE]
            or (len(value) == 2 and value[0] == "ERROR" and not isinstance(value[1], bool))
        )
    )


def _deps_valid(value: object) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(dep, str) for dep in value)
        and len(set(value)) == len(value)
    )


def _complete_input(record: object) -> bool:
    return (
        isinstance(record, dict)
        and type(record.get("has")) is bool
        and isinstance(record.get("values"), list)
        and bool(record["values"]) == record["has"]
        and "ended" in record
        and _terminal_valid(record["ended"])
    )


def _admit(checkpoint: Checkpoint) -> _Admitted:
    """The single restore admission boundary, before any graph or factory exists."""

    if not isinstance(checkpoint, dict) or checkpoint.get("format") != FORMAT:
        raise CheckpointError(f"restore: not a {FORMAT} checkpoint")
    nodes = checkpoint.get("nodes")
    if not isinstance(nodes, list):
        raise CheckpointError("restore: the checkpoint has no nodes")
    ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            raise CheckpointError("restore: every node needs a string id")
        node_id = node["id"]
        if "::" in node_id:
            raise CheckpointError(NAME_ERROR)
        if node_id.startswith(":") or node_id.endswith(":"):
            raise CheckpointError(BOUNDARY_NAME_ERROR)
        if node_id in ids:
            raise _not_restorable(node_id, "duplicate node id")
        ids.add(node_id)
        if node.get("kind") not in ("state", "node"):
            raise _not_restorable(node_id, "invalid node kind")
        if not _deps_valid(node.get("deps")):
            raise _not_restorable(node_id, "invalid dependencies")
        if node.get("local"):
            raise _not_restorable(node_id, "local-only, built or replaced without a recipe")
        if node["kind"] == "state":
            if node["deps"]:
                raise _not_restorable(node_id, "a state cannot have dependencies")
        else:
            recipe_value = node.get("recipe")
            if not isinstance(recipe_value, dict) or not isinstance(recipe_value.get("ref"), str):
                raise _not_restorable(node_id, "missing recipe")
            if "config" not in recipe_value:
                raise _not_restorable(node_id, "missing recipe config")
            _check(node_id, recipe_value, "its recipe config is not JSON")
            policies = node.get("policies")
            if (
                not isinstance(policies, dict)
                or any(
                    policies.get(key) not in ("auto", "manual")
                    for key in ("complete", "error", "invalidate")
                )
                or type(policies.get("quiet")) is not bool
            ):
                raise _not_restorable(node_id, "missing or invalid policies")
        if "saved" not in node:
            raise _not_restorable(
                node_id,
                "missing received records" if node["kind"] == "node" else "missing saved snapshot",
            )
        saved = node["saved"]
        if saved is None and node["kind"] == "node":
            continue  # Explicitly cold; it has never received inputs or held private state.
        if not isinstance(saved, dict):
            raise _not_restorable(node_id, "invalid saved snapshot")
        if node["kind"] == "state" and any(key in saved for key in ("deps", "inputs", "state")):
            raise _not_restorable(node_id, "invalid state snapshot")
        _check_saved(node_id, saved)
        if "values" not in saved or not isinstance(saved["values"], list):
            raise _not_restorable(node_id, "missing or invalid saved values")
        if "ended" not in saved or not _terminal_valid(saved["ended"]):
            raise _not_restorable(node_id, "missing or invalid saved terminal")
        if node["kind"] == "node":
            deps = saved["deps"] if "deps" in saved else node["deps"]
            if not _deps_valid(deps):
                raise _not_restorable(node_id, "invalid lifetime dependencies")
            inputs = saved.get("inputs")
            if (
                not isinstance(inputs, list)
                or len(inputs) != len(deps)
                or not all(_complete_input(record) for record in inputs)
            ):
                raise _not_restorable(node_id, "missing received records")
    # Check declared and lifetime topology before constructors can mutate a graph.
    for node in nodes:
        saved = node["saved"]
        if isinstance(saved, dict) and "deps" in saved:
            for dep in saved["deps"]:
                if dep not in ids:
                    raise _not_restorable(
                        node["id"], f"depends on {dep}, which is not in the checkpoint"
                    )
    detached = _json(checkpoint, "the checkpoint", "restore")
    ordered = _build_order(detached["nodes"])
    combined = [
        {
            **node,
            "deps": list(
                dict.fromkeys(
                    [
                        *node["deps"],
                        *(node["saved"].get("deps", []) if isinstance(node["saved"], dict) else []),
                    ]
                )
            ),
        }
        for node in ordered
    ]
    _build_order(combined)
    return _Admitted(ordered)


def restore(
    checkpoint: Checkpoint,
    registry: Mapping[str, Factory],
    *,
    report: Reporter | None = None,
    into: Graph | None = None,
) -> Graph:
    """Admit a detached checkpoint, then construct a cold graph without function runs."""

    return _restore_admitted(_admit(checkpoint), registry, report=report, into=into)


def _restore_admitted(
    admitted: _Admitted,
    registry: Mapping[str, Factory],
    *,
    report: Reporter | None = None,
    into: Graph | None = None,
) -> Graph:
    if into is not None and into.describe():
        raise CheckpointError("restore: the graph to restore into is not empty")
    graph = into if into is not None else Graph(report=report)

    def make(ref: str, config: object, node_id: str) -> Built:
        factory = registry.get(ref)
        if factory is None:
            raise _not_restorable(node_id, "missing recipe")
        built = factory(config, RecipeContext(graph, node_id, make))
        return built if isinstance(built, Built) else Built(built)

    for node in admitted.nodes:
        try:
            _restore_node(graph, node, make)
        except FATAL:
            raise
        except Exception as error:
            reason = str(error)
            if reason.startswith("not restorable: "):
                raise CheckpointError(reason) from error
            raise _not_restorable(node["id"], reason) from error
    return graph


def _restore_node(
    graph: Graph, node: dict[str, Any], make: Callable[[str, object, str], Built]
) -> None:
    node_id: str = node["id"]
    saved: dict[str, Any] | None = node["saved"]
    if node["kind"] == "state":
        graph.state(node_id)
        assert saved is not None  # admission requires a state snapshot
        values: list[object] = saved["values"]
        if values:
            # Its last array's values come back as one array, with no subscriber to see it.
            def body() -> None:
                for value in values:
                    graph.set(node_id, value)

            graph.batch(body)
        ended = _message(saved["ended"])
        if ended is not None:
            if ended[0] == COMPLETE:
                graph.complete(node_id)
            else:
                graph.error(node_id, ended[1])
        return
    recipe_value = node["recipe"]
    built = make(recipe_value["ref"], recipe_value["config"], node_id)
    policies = node["policies"]
    seed: Seed | None = None
    if saved is not None:
        ended = _message(saved["ended"])
        note: dict[str, object] | None = None
        if ended is None:  # an ended node never runs again, so it gets no note
            note = {"inputs": saved["inputs"]}
            if "deps" in saved:
                note["deps"] = saved["deps"]
        seed = Seed(
            values=saved["values"],
            ended=ended,
            state=saved.get("state"),
            deps=saved.get("deps"),
            note=note,
        )
    graph.node(
        node_id,
        node["deps"],
        _restorable(built.fn) if saved is not None else built.fn,
        complete=policies["complete"],
        error=policies["error"],
        invalidate=policies["invalidate"],
        quiet=policies["quiet"],
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
