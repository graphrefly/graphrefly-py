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
from typing import Final, Literal

from graphrefly._kernel.ctx import Ctx
from graphrefly._kernel.messages import (
    COMPLETE,
    COMPLETE_MESSAGE,
    DATA,
    ERROR,
    INVALIDATE,
    INVALIDATE_MESSAGE,
    PENDING,
    PENDING_MESSAGE,
    RESOLVED_MESSAGE,
    START_MESSAGE,
    Array,
    KernelError,
    Message,
    check_message,
    is_terminal,
    is_value,
    marks,
    split,
)

type Policy = Literal["auto", "manual"]
type NodeFn = Callable[[Ctx], object]
type SinkCallback = Callable[[tuple[Message, ...]], object]
type Reporter = Callable[[Report], object]

_NO_VALUE: Final = object()


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
class NodeState:
    """What a node holds right now; reading it changes nothing."""

    active: bool
    has: bool
    value: object
    values: tuple[object, ...]
    ended: Message | None


class _Input:
    """A node's record of one dependency: latest value and terminal, this wave's arrays."""

    __slots__ = ("ended", "frames", "has", "invalidated", "value")

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


class _Lifetime:
    """One activation of a computed node (R-lifetime)."""

    __slots__ = ("open",)

    def __init__(self) -> None:
        self.open = True


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
    ) -> None:
        self.id = node_id
        self.is_state = fn is None
        self.deps = deps
        self.declared_deps = deps
        self.fn = fn
        self.complete_policy = complete_policy
        self.error_policy = error_policy
        self.invalidate_policy = invalidate_policy
        self.subs: list[_Sink | _Edge] = []
        self.active = False
        self.lifetime: _Lifetime | None = None
        # R-subscription: the DATA values of the last array that had any.
        self.values = values
        self.has = bool(values)
        self.cache: object = values[-1] if values else None
        self.ended: Message | None = None
        self.private_state: object = None
        self.pending = False
        self.pending_deps: set[int] = set()
        self.inputs = [_Input() for _ in deps]


def _snapshot_of(node: _Node) -> Array:
    """What a new subscriber gets after START: the retained values, then the terminal."""

    snapshot: Array = [(DATA, value) for value in node.values]
    if node.ended is not None:
        snapshot.append(node.ended)
    return snapshot


def _has_mark(frames: Iterable[Array]) -> bool:
    return any(marks(message) for frame in frames for message in frame)


def _settles(frame: Array) -> bool:
    return any(marks(message) for message in frame)


class Graph:
    """One kernel graph: create nodes, subscribe sinks and feed state nodes."""

    def __init__(self, *, report: Reporter | None = None, trace: bool = False) -> None:
        self._nodes: dict[str, _Node] = {}
        self._report_to = report
        self.trace: list[TraceEntry] | None = [] if trace else None
        self._busy = False
        self._queue: deque[Callable[[], None]] = deque()

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
        self._add(_Node(node_id, dep_ids, fn, complete, error, invalidate, []))
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
        for node in self._nodes.values():
            node.pending = False
            node.pending_deps.clear()
            for record in node.inputs:
                record.frames = []
                record.invalidated = False

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
        for record in node.inputs:
            record.frames = []
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
        lifetime = node.lifetime
        if lifetime is None or not lifetime.open:
            return
        lifetime.open = False
        self._release_deps(node)
        node.deps = node.declared_deps
        node.inputs = [_Input() for _ in node.deps]
        node.private_state = None
        node.lifetime = None

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
        try:
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
        if node.pending and not any(_settles(frame) for frame in out):
            out = [*out, [RESOLVED_MESSAGE]]  # a pending node that sent nothing
        if not out:
            return
        if not node.pending and _has_mark(out):
            node.pending = True
            self._mark_pending(node)
        node.pending = False
        sent = [[PENDING_MESSAGE, *frame] if _settles(frame) else frame for frame in out]
        for sub in list(node.subs):
            if isinstance(sub, _Sink):
                for frame in sent:
                    self._deliver_to_sink(sub, node.id, tuple(frame))
            else:
                self._receive(sub.node, sub.index, sent)
        if node.ended is not None:
            self._close_lifetime(node)  # an ended node holds nothing upstream

    def _receive(self, node: _Node, index: int, frames: list[Array]) -> None:
        if node.ended is not None:
            return
        record = node.inputs[index]
        settled = False
        for frame in frames:
            kept = [message for message in frame if message[0] != PENDING]
            for message in kept:
                kind = message[0]
                if kind == DATA:
                    record.has = True
                    record.value = message[1]
                elif kind == INVALIDATE:
                    record.has = False
                    record.value = None
                elif kind in (COMPLETE, ERROR):
                    record.ended = message
            record.frames.append(kept)
            settled = settled or _settles(kept)
        if settled:
            node.pending_deps.discard(index)
        if not node.pending_deps:
            self._settle(node)

    def _settle(self, node: _Node) -> None:
        inputs = node.inputs
        if not any(record.frames for record in inputs):
            return

        def clear() -> None:
            for record in inputs:
                record.frames = []
                record.invalidated = False

        # Auto ERROR: a dependency's ERROR ends the node without running its function;
        # an INVALIDATE before it in the wave takes effect first (R-terminals).
        dep_error = next(
            (
                message
                for record in inputs
                for frame in record.frames
                for message in frame
                if message[0] == ERROR
            ),
            None,
        )
        if dep_error is not None and node.error_policy == "auto":
            invalidated_first = any(
                frame and frame[0][0] == INVALIDATE for record in inputs for frame in record.frames
            )
            clear()
            end: list[Array] = [[dep_error]]
            if invalidated_first:
                end = [[INVALIDATE_MESSAGE], [dep_error]]
            self._down(node, end)
            return

        # What came before an input's last INVALIDATE is gone (R-invalidation).
        invalidated = False
        for record in inputs:
            at = -1
            for position, frame in enumerate(record.frames):
                if frame and frame[0][0] == INVALIDATE:
                    at = position
            if at >= 0:
                record.frames = record.frames[at + 1 :]
                record.invalidated = True
                invalidated = True
        lead: list[Array] = []
        if invalidated and node.invalidate_policy == "auto":
            lead = self._apply(node, [[INVALIDATE_MESSAGE]])

        rest = [message for record in inputs for frame in record.frames for message in frame]
        manual_terminal = any(
            is_terminal(message)
            and (node.complete_policy if message[0] == COMPLETE else node.error_policy) == "manual"
            for message in rest
        )
        runs = (
            any(is_value(message) for message in rest)
            or manual_terminal
            or (invalidated and node.invalidate_policy == "manual")
        )
        out = self._invoke(node) if runs else []
        clear()

        all_ended = all(record.ended is not None for record in inputs)
        sent_terminal = any(is_terminal(message) for frame in out for message in frame)
        if node.complete_policy == "auto" and all_ended and not sent_terminal:
            out = [*out, [COMPLETE_MESSAGE]]
        self._deliver(node, [*lead, *self._apply(node, out)])

    def _run_wave(self, entries: list[tuple[_Node, list[Array]]]) -> None:
        live = [(node, frames) for node, frames in entries if node.ended is None]
        applied = [(node, self._apply(node, frames)) for node, frames in live]
        for node, out in applied:
            if not node.pending and _has_mark(out):
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
        """Write a value to a state node."""

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

        def op() -> None:
            if node.ended is not None:
                raise KernelError(f"{node_id} has ended")  # ended by an earlier request
            self._run_wave([(node, split([message]))])

        self._request(op)

    # ---- Reads ---------------------------------------------------------------

    def get(self, node_id: str) -> NodeState:
        """What the node retains as of now; reading never computes or activates."""

        node = self._node_of(node_id)
        return NodeState(node.active, node.has, node.cache, tuple(node.values), node.ended)
