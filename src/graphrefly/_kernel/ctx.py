"""The function's view of one invocation: ``ctx`` and ``ctx.deps[i]`` (R-invocation)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from graphrefly._kernel.messages import (
    DATA,
    Array,
    KernelError,
    Message,
    is_terminal,
    is_value,
    prepare_output,
)
from graphrefly._kernel.signal import Signal

if TYPE_CHECKING:
    from graphrefly._kernel.graph import Graph, _Input, _Lifetime, _Node

# The input record's "a DATA arrived" bit, one of graph.py's _SAW bits; it lives here
# because graph.py imports this module.
SAW_DATA: Final = 16
DEPS_SCOPE_ERROR: Final = "ctx.deps is only available during setup or function invocation"


class Dep:
    """One dependency as this invocation sees it."""

    __slots__ = ("_ctx", "_id", "_input", "_manual_invalidate")

    def __init__(self, dep_id: str, record: _Input, manual_invalidate: bool, ctx: Ctx) -> None:
        self._ctx = ctx
        self._id = dep_id
        self._input = record
        self._manual_invalidate = manual_invalidate

    @property
    def id(self) -> str:
        """The dependency's graph-registered identity during its invocation."""

        self._ctx._check_deps()
        return self._id

    @property
    def waves(self) -> list[list[object]]:
        """The value arrays this dependency delivered to this invocation.

        Each array is its DATA payloads in order; a RESOLVED array is ``[]``.
        Arrays without a value (an INVALIDATE, a terminal alone) are not listed.
        """

        self._ctx._check_deps()
        return [
            [message[1] for message in frame if message[0] == DATA]
            for frame in self._input.frames
            if any(is_value(message) for message in frame)
        ]

    @property
    def sent_data(self) -> bool:
        """Whether this dependency sent a DATA to this invocation: ``any(self.waves)``,
        read from the input record without building the arrays."""

        self._ctx._check_deps()
        return bool(self._input.saw & SAW_DATA)

    @property
    def terminal(self) -> Message | None:
        """The COMPLETE or ERROR this dependency sent in this invocation, if any."""

        self._ctx._check_deps()
        for frame in self._input.frames:
            for message in frame:
                if is_terminal(message):
                    return message
        return None

    @property
    def has(self) -> bool:
        """Whether a value has been received from this dependency and not invalidated."""

        self._ctx._check_deps()
        return self._input.has

    @property
    def value(self) -> object:
        """The latest value received from this dependency, or ``None`` without one."""

        self._ctx._check_deps()
        return self._input.value

    @property
    def invalidated(self) -> bool:
        """Whether an INVALIDATE arrived on this input (manual INVALIDATE policy only)."""

        self._ctx._check_deps()
        return self._manual_invalidate and self._input.invalidated


class Ctx:
    """What a node function receives: its inputs, private state and output."""

    __slots__ = ("_deps", "_graph", "_invoking", "_lifetime", "_node", "_out", "_seed")

    def __init__(self, graph: Graph, node: _Node, lifetime: _Lifetime) -> None:
        self._graph = graph
        self._node = node
        self._lifetime = lifetime
        self._out: list[Array] = []
        self._invoking = True
        self._deps: list[Dep] | None = None
        self._seed: object = None

    @property
    def deps(self) -> Sequence[Dep]:
        """Each declared dependency of this lifetime, in order."""

        self._check_deps()
        if self._deps is None:
            node = self._node
            manual = node.invalidate_policy == "manual"
            self._deps = [
                Dep(dep_id, record, manual, self)
                for dep_id, record in zip(node.deps, node.inputs, strict=True)
            ]
        return self._deps

    def _check_deps(self) -> None:
        if not self._invoking:
            raise KernelError(DEPS_SCOPE_ERROR)

    @property
    def state(self) -> object:
        """This lifetime's private state (``None`` until set).

        Read through a ctx whose lifetime has gone, it is ``None``, and a write through
        it is dropped, so an old ctx never reaches a later lifetime's state.
        """

        node = self._node
        return node.private_state if node.lifetime is self._lifetime else None

    @state.setter
    def state(self, value: object) -> None:
        node = self._node
        if node.lifetime is self._lifetime:
            node.private_state = value

    @property
    def signal(self) -> Signal:
        """The lifetime's abort signal: aborted when this lifetime closes."""

        lifetime = self._lifetime
        if lifetime.signal is None:
            lifetime.signal = Signal()
            if not lifetime.open:
                lifetime.signal._abort(lambda error: None)
        return lifetime.signal

    @property
    def seed(self) -> object:
        """The seed's note on a seeded instance's first run; ``None`` on every other run."""

        return self._seed

    def rewire(self, deps: Sequence[str]) -> None:
        """Change this node's own dependencies for its current lifetime (R-request-order).

        A request: it runs after the current wave and is dropped once this lifetime has
        closed. Kept dependencies keep their records; removed ones are released without
        a wave; added ones are acquired, and their snapshots, if any, are one input wave.
        The declared dependencies come back when the lifetime closes. A list with a
        duplicate, the node itself or a node that depends on it is refused.
        """

        if not self._lifetime.open:
            return
        self._graph._ask_rewire(self._node, self._lifetime, list(deps))

    def down(self, array: Sequence[Message]) -> None:
        """Send one array: this invocation's output, or a later wave once it returned.

        The array is checked before anything is applied (R-failure); called after
        the lifetime closed, it is dropped (R-lifetime).
        """

        frames = prepare_output(array)
        if self._invoking:
            self._out.extend(frames)
            return
        self._graph._later(self._node, self._lifetime, frames)
