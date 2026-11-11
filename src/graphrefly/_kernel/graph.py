"""The GraphReFly kernel: nodes, waves and requests over the D797 kernel rules.

One ``Graph`` is one single-thread domain (R-graph-domain). It is not thread-safe;
the facade binds it to its owner thread. The kernel catches ``Exception`` only:
a node function's throw is that node's ERROR (R-failure), a sink's or cleanup's
throw goes to the host reporter, and any other ``BaseException`` propagates to the
caller of the running host call after the kernel resets its in-wave state.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from inspect import isasyncgenfunction, isawaitable, iscoroutinefunction
from typing import Any, Final, Literal

from graphrefly._kernel.ctx import Ctx
from graphrefly._kernel.messages import (
    COMPLETE,
    COMPLETE_MESSAGE,
    DATA,
    ERROR,
    INVALIDATE,
    INVALIDATE_MESSAGE,
    PENDING_MESSAGE,
    RESOLVED,
    RESOLVED_MESSAGE,
    START_MESSAGE,
    Array,
    KernelError,
    Message,
    check_message,
    is_terminal,
    split,
)
from graphrefly._kernel.signal import Signal
from graphrefly._sentinel import SENTINEL

type Policy = Literal["auto", "manual"]
type NodeFn = Callable[[Ctx], object]
type Setup = Callable[[Ctx], object]
type SinkCallback = Callable[[tuple[Message, ...]], object]
type Reporter = Callable[[Report], object]

_NO_VALUE: Final = object()

# What an input record saw in the current wave, accumulated as its arrays arrive, so
# that a settlement needs no rescan in the common case.
_SAW_VALUE: Final = 1
_SAW_INVALIDATE: Final = 2
_SAW_TERMINAL: Final = 4
_SAW_ERROR: Final = 8
_SAW: Final = {
    DATA: _SAW_VALUE,
    RESOLVED: _SAW_VALUE,
    INVALIDATE: _SAW_INVALIDATE,
    COMPLETE: _SAW_TERMINAL,
    ERROR: _SAW_TERMINAL | _SAW_ERROR,
}
_MARKING: Final = frozenset(_SAW)


@dataclass(frozen=True, slots=True)
class Report:
    """A failure with no caller to return to (R-failure): a sink, cleanup or queued request."""

    source: str
    error: Exception


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """One array delivered to one sink, in delivery order."""

    to: str
    sender: str
    msgs: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class Seed:
    """What a node holds as if it had produced it, given at construction (R-subscription).

    ``values`` are the last array's DATA values, ``ended`` its terminal, ``state`` the
    private state of its first lifetime, ``deps`` the dependencies that lifetime starts
    on (back to the declared ones when it closes), and ``note`` an opaque value that only
    the seeded instance's first run receives, as ``ctx.seed``.
    """

    values: Sequence[object] = ()
    ended: Message | None = None
    state: object = None
    deps: Sequence[str] | None = None
    note: object = None


@dataclass(frozen=True, slots=True)
class Definition:
    """A node's current definition (R-node): declared dependencies, the current instance's
    function, setup and recipe, and the node's policies. Reading it changes nothing."""

    deps: tuple[str, ...]
    fn: NodeFn | None
    setup: Setup | None
    recipe: object
    complete: Policy
    error: Policy
    invalidate: Policy
    quiet: bool


@dataclass(frozen=True, slots=True)
class Described:
    """One node of the topology as data (R-node): its current dependencies."""

    id: str
    kind: Literal["state", "node"]
    deps: tuple[str, ...]
    quiet: bool


@dataclass(frozen=True, slots=True)
class NodeStatus:
    """A node's status, derived from what it holds and never stored (R-node)."""

    cache: Literal["empty", "stale", "ready"]
    output: Literal["open", "completed", "errored"]
    lifecycle: Literal["inactive", "active", "closing"]


@dataclass(frozen=True, slots=True)
class NodeState:
    """What a node holds right now; reading it changes nothing."""

    active: bool
    has: bool
    value: object
    values: tuple[object, ...]
    ended: Message | None


class _Input:
    """A node's record of one dependency: latest value and terminal, this wave's arrays."""

    __slots__ = ("ended", "frames", "has", "invalidated", "saw", "value")

    def __init__(
        self,
        has: bool = False,
        value: object = None,
        ended: Message | None = None,
        frames: list[Array] | None = None,
    ) -> None:
        self.has = has
        self.value = value
        self.ended = ended
        self.frames: list[Array] = [] if frames is None else frames
        self.invalidated = False
        self.saw = _saw_of(self.frames)


class _Lifetime:
    """One activation of a computed node (R-lifetime): its signal, setup and cleanup."""

    __slots__ = ("cleanup", "open", "setup_done", "signal")

    def __init__(self) -> None:
        self.open = True
        self.signal: Signal | None = None  # created on first access
        self.setup_done = False
        self.cleanup: Callable[[], object] | None = None


class _Sink:
    __slots__ = ("callback", "id")

    def __init__(self, sink_id: str, callback: SinkCallback | None) -> None:
        self.id = sink_id
        self.callback = callback


class _Edge:
    """A dependent's hold on a node: ``node`` reads it as its dependency ``index``."""

    __slots__ = ("index", "node")

    def __init__(self, node: _Node, index: int) -> None:
        self.node = node
        self.index = index


class _Node:
    __slots__ = (
        "active",
        "cache",
        "complete_policy",
        "declared_deps",
        "deps",
        "ended",
        "error_policy",
        "fn",
        "has",
        "id",
        "inputs",
        "invalidate_policy",
        "is_state",
        "lifetime",
        "pending",
        "pending_deps",
        "private_state",
        "quiet",
        "recipe",
        "seed_deps",
        "seed_note",
        "setup",
        "subs",
        "values",
    )

    def __init__(
        self,
        node_id: str,
        deps: list[str],
        fn: NodeFn | None,
        complete_policy: Policy,
        error_policy: Policy,
        invalidate_policy: Policy,
        values: list[object],
        quiet: bool = False,
        setup: Setup | None = None,
        recipe: object = None,
        seed: Seed | None = None,
    ) -> None:
        self.id = node_id
        self.is_state = fn is None
        self.deps = deps
        self.declared_deps = deps
        self.fn = fn
        self.complete_policy = complete_policy
        self.error_policy = error_policy
        self.invalidate_policy = invalidate_policy
        # A quiet node consumes its input waves without passing the pending mark on;
        # what it sends is a later wave of its own (R-wave).
        self.quiet = quiet
        # Runs once per lifetime before the function's first run; belongs to the instance.
        self.setup = setup
        # An opaque description of how the instance was built; stored, never read (R-node).
        self.recipe = recipe
        self.seed_deps: list[str] | None = (
            None if seed is None or seed.deps is None else list(seed.deps)
        )
        self.seed_note: object = _NO_VALUE if seed is None else seed.note
        self.subs: list[_Sink | _Edge] = []
        self.active = False
        self.lifetime: _Lifetime | None = None
        # R-subscription: the DATA values of the last array that had any.
        self.values = values
        self.has = bool(values)
        self.cache: object = values[-1] if values else None
        self.ended: Message | None = None if seed is None else seed.ended
        self.private_state: object = None if seed is None else seed.state
        self.pending = False
        self.pending_deps: set[int] = set()
        self.inputs = [_Input() for _ in deps]


class _Deferred:
    """A function given to set, computed when the write runs in request order."""

    __slots__ = ("fn",)

    def __init__(self, fn: Callable[[Any], object]) -> None:
        self.fn = fn


class _Staged:
    """What a batch body staged for one node: its accumulated messages."""

    __slots__ = ("lifetime", "msgs")

    def __init__(self, lifetime: _Lifetime | None) -> None:
        self.lifetime = lifetime
        self.msgs: Array = []


def _snapshot_of(node: _Node) -> Array:
    """What a new subscriber gets after START: the retained values, then the terminal."""

    snapshot: Array = [(DATA, value) for value in node.values]
    if node.ended is not None:
        snapshot.append(node.ended)
    return snapshot


def _saw_of(frames: Iterable[Array]) -> int:
    saw = 0
    for frame in frames:
        for message in frame:
            saw |= _SAW.get(message[0], 0)
    return saw


def _settles(frame: Array) -> bool:
    """Whether an array marks its sender's cone pending and settles a mark (R-wave)."""

    for message in frame:  # noqa: SIM110 - a loop is faster than any() here
        if message[0] in _MARKING:
            return True
    return False


def _resolve(node: _Node, msgs: Array) -> Array:
    """Compute staged set functions in order, each from the value before it."""

    current: object = node.cache if node.has else SENTINEL
    out: Array = []
    for message in msgs:
        kind = message[0]
        if kind == INVALIDATE:
            current = SENTINEL
        if kind != DATA:
            out.append(message)
            continue
        payload = message[1]
        if type(payload) is not _Deferred:
            current = payload
            out.append(message)
            continue
        value = payload.fn(current)
        if value is current:
            continue  # it returned the value it was given: nothing is written
        out.append(check_message((DATA, value)))
        current = value
    return out


def _clear(inputs: list[_Input]) -> None:
    for record in inputs:
        record.frames = []
        record.invalidated = False
        record.saw = 0


class Graph:
    """One kernel graph: create nodes, subscribe sinks and feed state nodes."""

    def __init__(self, *, report: Reporter | None = None, trace: bool = False) -> None:
        self._nodes: dict[str, _Node] = {}
        self._report_to = report
        self.trace: list[TraceEntry] | None = [] if trace else None
        self._busy = False
        self._queue: deque[Callable[[], None]] = deque()
        # While a batch body runs: what it staged per node, and the requests it made.
        self._staging: dict[_Node, _Staged] | None = None
        self._batch_requests: list[Callable[[], None]] | None = None

    # ---- Construction -----------------------------------------------------

    def state(self, node_id: str, initial: object = _NO_VALUE) -> str:
        """Create a state node, with a value or empty (R-sentinel)."""

        values: list[object] = [] if initial is _NO_VALUE else [initial]
        if values:
            check_message((DATA, initial))
        self._add(_Node(node_id, [], None, "auto", "auto", "auto", values))
        return node_id

    def node(
        self,
        node_id: str,
        deps: Sequence[str],
        fn: NodeFn,
        *,
        complete: Policy = "auto",
        error: Policy = "auto",
        invalidate: Policy = "auto",
        quiet: bool = False,
        setup: Setup | None = None,
        recipe: object = None,
        seed: Seed | None = None,
    ) -> str:
        """Create a computed node over declared dependencies (R-node)."""

        dep_ids = list(deps)
        for dep_id in dep_ids:
            self._node_of(dep_id)
        if len(set(dep_ids)) != len(dep_ids):
            raise KernelError(f"{node_id} needs distinct dependencies")
        for name, policy in (("complete", complete), ("error", error), ("invalidate", invalidate)):
            if policy not in ("auto", "manual"):
                raise KernelError(f"{name} policy must be 'auto' or 'manual', not {policy!r}")
        values: list[object] = []
        if seed is not None:
            values = list(seed.values)
            for value in values:
                check_message((DATA, value))
            if seed.ended is not None and not is_terminal(check_message(seed.ended)):
                raise KernelError(f"{node_id}: a seed's ended must be COMPLETE or ERROR")
        node = _Node(node_id, dep_ids, fn, complete, error, invalidate, values, bool(quiet))
        node.setup = setup
        node.recipe = recipe
        if seed is not None:
            node.ended = seed.ended
            node.private_state = seed.state
            node.seed_deps = None if seed.deps is None else list(seed.deps)
            node.seed_note = seed.note
        self._add(node)
        return node_id

    def _add(self, node: _Node) -> None:
        if node.id in self._nodes:
            raise KernelError(f"duplicate node {node.id}")
        self._nodes[node.id] = node

    def _node_of(self, node_id: str) -> _Node:
        node = self._nodes.get(node_id)
        if node is None:
            raise KernelError(f"unknown node {node_id}")
        return node

    # ---- Requests (R-request-order) ---------------------------------------
    #
    # A request that arrives while the kernel is running one (from a sink, from a
    # function reaching the graph, from a late ctx.down) waits and runs afterwards,
    # in arrival order, so a wave never grows after parts of it settled.

    def _request(self, op: Callable[[], None]) -> None:
        if self._batch_requests is not None:
            self._batch_requests.append(op)  # the batch's commit has not run yet
            return
        if self._busy:
            self._queue.append(op)
            return
        failure: Exception | None = None
        self._busy = True
        try:
            try:
                op()
            except Exception as error:
                failure = error
            while self._queue:
                queued = self._queue.popleft()
                try:
                    queued()
                except Exception as error:
                    self._report("queued request", error)
        except BaseException:
            self._abandon()
            raise
        finally:
            self._busy = False
        if failure is not None:
            raise failure

    def _abandon(self) -> None:
        """Reset in-wave state after a fatal BaseException; delivered arrays stay delivered."""

        self._queue.clear()
        self._staging = None
        self._batch_requests = None
        for node in self._nodes.values():
            node.pending = False
            node.pending_deps.clear()
            _clear(node.inputs)

    def _report(self, source: str, error: Exception) -> None:
        if self._report_to is None:
            return
        with suppress(Exception):  # the reporter's own failure has nowhere to go
            self._report_to(Report(source, error))

    # ---- Lifetimes and activation (R-lifetime) ----------------------------

    def _activate(self, node: _Node) -> None:
        node.active = True
        if node.is_state or node.ended is not None:
            return  # an ended node gives its snapshot but never runs again
        node.lifetime = _Lifetime()
        if node.seed_deps is not None:
            # A seeded first lifetime starts on the dependencies a rewire had given it.
            for dep_id in node.seed_deps:
                self._node_of(dep_id)
            node.deps = node.seed_deps
            node.inputs = [_Input() for _ in node.deps]
            node.seed_deps = None
        for index in range(len(node.deps)):
            self._acquire(node, index)
        self._down(node, self._first_run(node))

    def _first_run(self, node: _Node) -> list[Array]:
        """A lifetime's first run sees each dependency's snapshot as its input (R-terminals)."""

        dep_error = next(
            (r.ended for r in node.inputs if r.ended is not None and r.ended[0] == ERROR),
            None,
        )
        if dep_error is not None and node.error_policy == "auto":
            out: list[Array] = [[dep_error]]
        else:
            out = self._invoke(node)
        _clear(node.inputs)
        all_ended = bool(node.deps) and all(r.ended is not None for r in node.inputs)
        if node.complete_policy == "auto" and all_ended:
            out = [*out, [COMPLETE_MESSAGE]]
        return out

    def _acquire(self, node: _Node, index: int) -> None:
        dep = self._node_of(node.deps[index])
        if not dep.active:
            self._activate(dep)
        dep.subs.append(_Edge(node, index))
        snapshot = _snapshot_of(dep)
        node.inputs[index] = _Input(dep.has, dep.cache, dep.ended, [snapshot] if snapshot else [])

    def _release(self, dep: _Node, sub: _Sink | _Edge) -> None:
        try:
            dep.subs.remove(sub)
        except ValueError:
            return
        if not dep.subs and dep.active:
            self._deactivate(dep)

    def _release_deps(self, node: _Node) -> None:
        for index, dep_id in enumerate(node.deps):
            dep = self._node_of(dep_id)
            for sub in dep.subs:
                if isinstance(sub, _Edge) and sub.node is node and sub.index == index:
                    self._release(dep, sub)
                    break

    def _close_lifetime(self, node: _Node) -> None:
        """Close in the rule's order: abort the signal, release the dependencies, run the
        cleanup (which may still read the private state), then drop the private state."""

        lifetime = node.lifetime
        if lifetime is None or not lifetime.open:
            return
        lifetime.open = False
        if lifetime.signal is not None:
            lifetime.signal._abort(lambda error: self._report(f"signal {node.id}", error))
        self._release_deps(node)
        node.deps = node.declared_deps
        node.inputs = [_Input() for _ in node.deps]
        cleanup, lifetime.cleanup = lifetime.cleanup, None
        if cleanup is not None:
            try:
                cleanup()
            except Exception as error:
                self._report(f"cleanup {node.id}", error)  # not the node's ERROR
        node.private_state = None
        node.lifetime = None

    # ---- Topology: replace and rewire (R-request-order, R-node) -------------

    def _check_deps(self, op: str, node: _Node, deps: list[str]) -> None:
        for dep_id in deps:
            self._node_of(dep_id)
        if len(set(deps)) != len(deps):
            raise KernelError(f"{op}: {node.id} needs distinct dependencies")
        for dep_id in deps:
            if dep_id == node.id or self._depends_on(self._node_of(dep_id), node.id):
                raise KernelError(f"{op}: {node.id} would depend on itself through {dep_id}")

    def _depends_on(self, start: _Node, target: str) -> bool:
        """Whether ``start`` reaches ``target`` through current or declared dependencies:
        a closing lifetime puts the declared ones back, so either could close a cycle."""

        seen: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            for dep_id in (*node.deps, *node.declared_deps):
                if dep_id == target:
                    return True
                if dep_id not in seen:
                    seen.add(dep_id)
                    stack.append(self._node_of(dep_id))
        return False

    def replace(
        self,
        node_id: str,
        deps: Sequence[str],
        fn: NodeFn,
        *,
        setup: Setup | None = None,
        recipe: object = None,
    ) -> None:
        """Give a computed node a new instance under the same id and subscribers.

        The current instance closes; the new one starts as on a first activation with
        these dependencies, function, setup and recipe; the node's policies stay. Its
        sinks receive START and the new snapshot, its dependents the snapshot as input.
        A request: during a wave it waits until the wave ends.
        """

        node = self._node_of(node_id)
        if node.is_state:
            raise KernelError(f"{node_id} is not a computed node")
        dep_ids = list(deps)
        self._check_deps("replace", node, dep_ids)
        self._request(lambda: self._replace_node(node, dep_ids, fn, setup, recipe))

    def _replace_node(
        self,
        node: _Node,
        deps: list[str],
        fn: NodeFn,
        setup: Setup | None,
        recipe: object,
    ) -> None:
        old = node.lifetime  # None when inactive, or when the old instance has ended
        if old is not None:
            old.open = False
            if old.signal is not None:
                old.signal._abort(lambda error: self._report(f"signal {node.id}", error))
        else:
            node.deps = []  # an ended instance has released its dependencies already
        self._exchange(node, deps)
        if old is not None and old.cleanup is not None:
            cleanup, old.cleanup = old.cleanup, None
            try:
                cleanup()
            except Exception as error:
                self._report(f"cleanup {node.id}", error)
        node.fn = fn
        node.setup = setup
        node.recipe = recipe
        node.seed_note = _NO_VALUE
        node.seed_deps = None
        node.declared_deps = deps
        node.lifetime = None
        node.values = []
        node.has = False
        node.cache = None
        node.ended = None
        node.private_state = None
        node.pending = False
        node.pending_deps.clear()
        if not node.active:
            return  # the next subscription activates the new instance

        node.lifetime = _Lifetime()
        for index, dep_id in enumerate(deps):
            dep = self._node_of(dep_id)
            snapshot = _snapshot_of(dep)
            node.inputs[index] = _Input(
                dep.has, dep.cache, dep.ended, [snapshot] if snapshot else []
            )
        self._apply(node, self._first_run(node))  # the retained value only

        snapshot = _snapshot_of(node)
        edges: list[_Edge] = []
        for sub in list(node.subs):
            if type(sub) is _Edge:
                sub.node.inputs[sub.index] = _Input()
                edges.append(sub)
            else:
                assert isinstance(sub, _Sink)
                self._deliver_to_sink(sub, node.id, (START_MESSAGE, *snapshot))
        if snapshot:
            # One wave from the node: its cone is marked first, then each dependent
            # receives the snapshot as its input.
            if _settles(snapshot):
                node.pending = True
                self._mark_pending(node)
                node.pending = False
            for edge in edges:
                self._receive(edge.node, edge.index, [snapshot])
        if node.ended is not None:
            self._close_lifetime(node)

    def _ask_rewire(self, node: _Node, lifetime: _Lifetime, deps: list[str]) -> None:
        if len(set(node.deps)) != len(node.deps):
            raise KernelError(f"rewire: {node.id} needs distinct dependencies")
        self._check_deps("rewire", node, deps)

        def op() -> None:
            if lifetime.open and node.lifetime is lifetime:
                added = self._exchange(node, deps)
                if any(node.inputs[index].frames for index in added):
                    self._settle(node)

        self._request(op)

    def _exchange(self, node: _Node, deps: list[str]) -> list[int]:
        """Give ``node`` the dependency list ``deps``; return the added positions.

        An inactive node only changes its list. For an active one a dependency in both
        lists keeps its connection and record; an added one is acquired before any is
        released, so a shared upstream stays active; then a removed one is released,
        without a wave.
        """

        if not node.active:
            node.deps = deps
            node.inputs = [_Input() for _ in deps]
            return []
        held: dict[str, tuple[_Input, _Edge | None]] = {}
        for index, dep_id in enumerate(node.deps):
            edge = next(
                (
                    sub
                    for sub in self._node_of(dep_id).subs
                    if type(sub) is _Edge and sub.node is node and sub.index == index
                ),
                None,
            )
            held[dep_id] = (node.inputs[index], edge)
        added: list[int] = []
        inputs: list[_Input] = []
        for index, dep_id in enumerate(deps):
            kept = held.pop(dep_id, None)
            if kept is None:
                added.append(index)
                inputs.append(_Input())
                continue
            record, edge = kept
            if edge is not None:
                edge.index = index
            inputs.append(record)
        node.inputs = inputs
        node.deps = deps
        for index in added:
            self._acquire(node, index)
        for dep_id, (_record, edge) in held.items():
            if edge is not None:
                self._release(self._node_of(dep_id), edge)
        return added

    def _deactivate(self, node: _Node) -> None:
        node.active = False
        if node.is_state:
            return  # a state keeps its values across deactivation
        self._close_lifetime(node)
        if node.ended is None:
            node.has = False
            node.cache = None
            node.values = []

    # ---- Invocation (R-invocation, R-failure) -----------------------------

    def _invoke(self, node: _Node) -> list[Array]:
        lifetime = node.lifetime
        assert lifetime is not None and node.fn is not None
        ctx = Ctx(self, node, lifetime)
        if node.seed_note is not _NO_VALUE:
            ctx._seed = node.seed_note  # the seeded instance's first run only
            node.seed_note = _NO_VALUE
        try:
            if not lifetime.setup_done:
                # The lifetime's setup runs first, once, in this invocation: what it sends
                # is part of this run's output, and a throw ends the node (R-lifetime).
                lifetime.setup_done = True
                if node.setup is not None:
                    cleanup = node.setup(ctx)
                    if callable(cleanup):
                        lifetime.cleanup = cleanup
            node.fn(ctx)
        except Exception as error:
            return [[(ERROR, error)]]  # what it sent before the throw is dropped
        finally:
            ctx._invoking = False
        return ctx._out

    def _later(self, node: _Node, lifetime: _Lifetime, frames: list[Array]) -> None:
        """Output sent after an invocation returned: a new wave from ``node`` (R-wave)."""

        if not frames:
            return
        if self._staging is not None and lifetime.open:
            self._stage(node, lifetime, [m for frame in frames for m in frame])
            return

        def op() -> None:
            if lifetime.open and node.lifetime is lifetime:
                self._run_wave([(node, frames)])

        self._request(op)

    # ---- Waves (R-wave, R-invalidation, R-terminals) -----------------------

    def _mark_pending(self, node: _Node) -> None:
        for sub in node.subs:
            if isinstance(sub, _Sink):
                continue
            dependent = sub.node
            if dependent.ended is not None:
                continue
            dependent.pending_deps.add(sub.index)
            if not dependent.pending:
                dependent.pending = True
                if not dependent.quiet:
                    self._mark_pending(dependent)

    def _deliver_to_sink(self, sink: _Sink, sender: str, array: tuple[Message, ...]) -> None:
        if self.trace is not None:
            self.trace.append(TraceEntry(sink.id, sender, array))
        if sink.callback is None:
            return
        try:
            sink.callback(array)
        except Exception as error:
            self._report(f"sink {sink.id}", error)  # not the node's ERROR

    def _down(self, node: _Node, frames: list[Array]) -> None:
        self._deliver(node, self._apply(node, frames))

    def _apply(self, node: _Node, frames: list[Array]) -> list[Array]:
        """Write a node's own output into what it holds; return what remains to send."""

        out: list[Array] = []
        for frame in frames:
            if node.ended is not None:
                break  # nothing after a terminal
            kept: Array = []
            values: list[object] = []
            for message in frame:
                kind = message[0]
                if kind == DATA:
                    node.has = True
                    node.cache = message[1]
                    values.append(message[1])
                elif kind == INVALIDATE:
                    if not node.has:
                        continue  # effective only while a value is retained
                    node.has = False
                    node.cache = None
                    node.values = []
                elif kind in (COMPLETE, ERROR):
                    node.ended = message
                kept.append(message)
            if values:
                node.values = values
            if kept:
                out.append(kept)
        return out

    def _deliver(self, node: _Node, out: list[Array]) -> None:
        marked = False
        for frame in out:
            if _settles(frame):
                marked = True
                break
        if node.pending and not marked:
            out = [*out, [RESOLVED_MESSAGE]]  # a pending node that sent nothing
            marked = True
        if not out:
            return
        if marked and not node.pending:
            # A source, later output, or a node answering a wave it was not marked in
            # marks its own cone pending first.
            node.pending = True
            self._mark_pending(node)
        node.pending = False
        sent: list[tuple[Message, ...]] | None = None
        for sub in list(node.subs):
            if type(sub) is _Edge:
                self._receive(sub.node, sub.index, out)  # dependents never see PENDING
            else:
                assert isinstance(sub, _Sink)
                if sent is None:
                    sent = [
                        (PENDING_MESSAGE, *frame) if _settles(frame) else tuple(frame)
                        for frame in out
                    ]
                for array in sent:
                    self._deliver_to_sink(sub, node.id, array)
        if node.ended is not None:
            self._close_lifetime(node)  # an ended node holds nothing upstream

    def _receive(self, node: _Node, index: int, frames: list[Array]) -> None:
        if node.ended is not None:
            return
        record = node.inputs[index]
        saw = 0
        for frame in frames:
            for message in frame:
                kind = message[0]
                if kind == DATA:
                    record.has = True
                    record.value = message[1]
                    saw |= _SAW_VALUE
                else:
                    bit = _SAW.get(kind, 0)
                    saw |= bit
                    if kind == INVALIDATE:
                        record.has = False
                        record.value = None
                    elif bit & _SAW_TERMINAL:
                        record.ended = message
            record.frames.append(frame)
        record.saw |= saw
        if saw:
            node.pending_deps.discard(index)
        if not node.pending_deps:
            self._settle(node)

    def _settle(self, node: _Node) -> None:
        inputs = node.inputs
        saw = 0
        has_frames = False
        for record in inputs:
            if record.frames:
                has_frames = True
                saw |= record.saw

        if not has_frames:
            return

        # Auto ERROR: a dependency's ERROR ends the node without running its function;
        # an INVALIDATE before it in the wave takes effect first (R-terminals).
        if saw & _SAW_ERROR and node.error_policy == "auto":
            dep_error = next(
                message
                for record in inputs
                for frame in record.frames
                for message in frame
                if message[0] == ERROR
            )
            invalidated_first = any(
                frame and frame[0][0] == INVALIDATE for record in inputs for frame in record.frames
            )
            _clear(inputs)
            end: list[Array] = [[dep_error]]
            if invalidated_first:
                end = [[INVALIDATE_MESSAGE], [dep_error]]
            if node.quiet:
                node.pending = False
                self._later_from(node, end)  # the INVALIDATE goes with the ERROR
            else:
                self._down(node, end)
            return

        lead: list[Array] = []
        invalidated = False
        if saw & _SAW_INVALIDATE:
            # What came before an input's last INVALIDATE is gone (R-invalidation).
            for record in inputs:
                at = -1
                for position, frame in enumerate(record.frames):
                    if frame and frame[0][0] == INVALIDATE:
                        at = position
                if at >= 0:
                    record.frames = record.frames[at + 1 :]
                    record.invalidated = True
                    invalidated = True
                record.saw = _saw_of(record.frames)
            saw = 0
            for record in inputs:
                saw |= record.saw
            if invalidated and node.invalidate_policy == "auto":
                lead = self._apply(node, [[INVALIDATE_MESSAGE]])

        runs = bool(saw & _SAW_VALUE) or (invalidated and node.invalidate_policy == "manual")
        if not runs and saw & _SAW_TERMINAL:
            runs = any(
                is_terminal(message)
                and (node.complete_policy if message[0] == COMPLETE else node.error_policy)
                == "manual"
                for record in inputs
                for frame in record.frames
                for message in frame
            )
        out = self._invoke(node) if runs else []
        _clear(inputs)

        # Auto COMPLETE once every dependency has ended; dependencies only newly end in a
        # wave that carries a terminal.
        postlude = (
            bool(saw & _SAW_TERMINAL)
            and node.complete_policy == "auto"
            and all(record.ended is not None for record in inputs)
            and not any(is_terminal(message) for frame in out for message in frame)
        )
        if node.quiet:
            # What a quiet node sends is a later wave; only an auto INVALIDATE goes on now.
            if lead:
                self._deliver(node, lead)
            node.pending = False
            self._later_from(node, out)
            if postlude:
                self._later_from(node, [[COMPLETE_MESSAGE]])
            return
        if postlude:
            out = [*out, [COMPLETE_MESSAGE]]
        self._deliver(node, [*lead, *self._apply(node, out)] if lead else self._apply(node, out))

    def _later_from(self, node: _Node, frames: list[Array]) -> None:
        lifetime = node.lifetime
        if lifetime is not None:
            self._later(node, lifetime, frames)

    def _run_wave(self, entries: list[tuple[_Node, list[Array]]]) -> None:
        live = [(node, frames) for node, frames in entries if node.ended is None]
        applied = [(node, self._apply(node, frames)) for node, frames in live]
        for node, out in applied:
            if not node.pending and any(_settles(frame) for frame in out):
                node.pending = True
                self._mark_pending(node)
        for node, out in applied:
            self._deliver(node, out)

    # ---- Host input and subscriptions --------------------------------------

    def subscribe(self, sink_id: str, node_id: str, callback: SinkCallback | None = None) -> None:
        """Subscribe a sink; its first array is START and the node's snapshot (R-subscription)."""

        node = self._node_of(node_id)

        def op() -> None:
            if any(isinstance(s, _Sink) and s.id == sink_id for s in node.subs):
                return
            if not node.active:
                self._activate(node)
            sink = _Sink(sink_id, callback)
            node.subs.append(sink)
            self._deliver_to_sink(sink, node.id, (START_MESSAGE, *_snapshot_of(node)))

        self._request(op)

    def unsubscribe(self, sink_id: str, node_id: str) -> None:
        """Remove a sink; the last subscriber's leaving deactivates the node (R-lifetime)."""

        node = self._node_of(node_id)

        def op() -> None:
            for sub in node.subs:
                if isinstance(sub, _Sink) and sub.id == sink_id:
                    self._release(node, sub)
                    return

        self._request(op)

    def set(self, node_id: str, value: object) -> None:
        """Write a value to a state node; given a callable, compute it when the write runs.

        The callable gets the state's value when the write takes its place in request
        order (in a batch, from the writes staged before it; ``SENTINEL`` when there is
        none) and returns the value to write; returning the very value it was given
        writes nothing. A callable is written as a value only wrapped in another one
        (R-request-order).
        """

        if callable(value):
            self._host_input(node_id, (DATA, _Deferred(value)))
        else:
            self._host_input(node_id, check_message((DATA, value)))

    def invalidate(self, node_id: str) -> None:
        """Invalidate a state node's value (R-invalidation)."""

        self._host_input(node_id, INVALIDATE_MESSAGE)

    def complete(self, node_id: str) -> None:
        """End a state node with COMPLETE (R-terminals)."""

        self._host_input(node_id, COMPLETE_MESSAGE)

    def error(self, node_id: str, diagnostic: object) -> None:
        """End a state node with ERROR (R-terminals)."""

        self._host_input(node_id, check_message((ERROR, diagnostic)))

    def _host_input(self, node_id: str, message: Message) -> None:
        node = self._node_of(node_id)
        if not node.is_state:
            raise KernelError(f"{node_id} is not a state node")
        if node.ended is not None:
            raise KernelError(f"{node_id} has ended")
        if self._staging is not None:
            self._stage(node, None, [message])
            return

        def op() -> None:
            if node.ended is not None:
                raise KernelError(f"{node_id} has ended")  # ended by an earlier request
            msgs = _resolve(node, [message])
            if msgs:
                self._run_wave([(node, split(msgs))])

        self._request(op)

    # ---- Batch (R-request-order) --------------------------------------------

    def _stage(self, node: _Node, lifetime: _Lifetime | None, msgs: Array) -> None:
        """Add to a node's accumulated batch array; a submission that would make it invalid
        is rejected and leaves the earlier staging as it was."""

        assert self._staging is not None
        entry = self._staging.get(node)
        accumulated = [*(entry.msgs if entry is not None else []), *msgs]
        try:
            split(accumulated)
        except KernelError as error:
            raise KernelError(f"{node.id}: {error} (batch)") from None
        if entry is None:
            entry = self._staging[node] = _Staged(lifetime)
        entry.msgs = accumulated

    def batch[R](self, body: Callable[[], R]) -> R:
        """Run ``body`` at once and commit its host inputs as one wave (R-request-order).

        Nothing is delivered while the body runs. Its topology requests run after the
        commit; a body that throws commits nothing and drops its requests. The commit
        is itself a request, so during a wave it waits until the wave ends. A batch
        inside a batch body joins the outer one. An async function is refused before
        it runs; a body that returns an awaitable commits its synchronous part, and the
        host reporter is told.
        """

        if iscoroutinefunction(body) or isasyncgenfunction(body):
            raise KernelError("batch body is an async function; await first, then batch the writes")
        if self._staging is not None:
            return self._report_awaitable(body())
        self._staging = {}
        self._batch_requests = []
        try:
            result = body()
            staged = self._staging
            requests = self._batch_requests
        finally:
            self._staging = None
            self._batch_requests = None
        self._report_awaitable(result)

        def commit() -> None:
            # The body's requests run after this wave and before anything it requests;
            # they stay queued also when a set function below fails the commit.
            self._queue.extendleft(reversed(requests))
            resolved = [(node, entry, _resolve(node, entry.msgs)) for node, entry in staged.items()]
            entries: list[tuple[_Node, list[Array]]] = []
            for node, entry, msgs in resolved:
                lifetime = entry.lifetime
                if lifetime is not None and not (lifetime.open and node.lifetime is lifetime):
                    continue  # staged by a lifetime that has closed
                if msgs:
                    entries.append((node, split(msgs)))
            self._run_wave(entries)

        self._request(commit)
        return result

    def _report_awaitable[R](self, result: R) -> R:
        if isawaitable(result):
            self._report(
                "batch",
                KernelError(
                    "batch body returned an awaitable; writes after its first await are"
                    " separate waves"
                ),
            )
        return result

    # ---- Reads ---------------------------------------------------------------

    def status(self, node_id: str) -> NodeStatus:
        """The node's status, derived from what it holds (R-node); reading changes nothing."""

        node = self._node_of(node_id)
        ended = node.ended
        lifetime = node.lifetime
        return NodeStatus(
            "empty" if not node.has else "stale" if node.pending else "ready",
            "open" if ended is None else "completed" if ended[0] == COMPLETE else "errored",
            "closing"
            if lifetime is not None and not lifetime.open
            else "active"
            if node.active
            else "inactive",
        )

    def definition(self, node_id: str) -> Definition:
        """The node's current definition (R-node); reading changes nothing."""

        node = self._node_of(node_id)
        return Definition(
            tuple(node.declared_deps),
            node.fn,
            node.setup,
            node.recipe,
            node.complete_policy,
            node.error_policy,
            node.invalidate_policy,
            node.quiet,
        )

    def describe(self) -> list[Described]:
        """Every node in creation order with its current dependencies (R-node)."""

        return [
            Described(node.id, "state" if node.is_state else "node", tuple(node.deps), node.quiet)
            for node in self._nodes.values()
        ]

    def get(self, node_id: str) -> NodeState:
        """What the node retains as of now; reading never computes or activates."""

        node = self._node_of(node_id)
        return NodeState(node.active, node.has, node.cache, tuple(node.values), node.ended)
