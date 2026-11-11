"""The function's view of one invocation: ``ctx`` and ``ctx.deps[i]`` (R-invocation)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from graphrefly._kernel.messages import DATA, Array, Message, is_terminal, is_value, prepare_output

if TYPE_CHECKING:
    from graphrefly._kernel.graph import Graph, _Input, _Lifetime, _Node


class Dep:
    """One dependency as this invocation sees it."""

    __slots__ = ("_input", "_manual_invalidate", "id")

    def __init__(self, dep_id: str, record: _Input, manual_invalidate: bool) -> None:
        self.id = dep_id
        self._input = record
        self._manual_invalidate = manual_invalidate

    @property
    def waves(self) -> list[list[object]]:
        """The value arrays this dependency delivered to this invocation.

        Each array is its DATA payloads in order; a RESOLVED array is ``[]``.
        Arrays without a value (an INVALIDATE, a terminal alone) are not listed.
        """

        return [
            [message[1] for message in frame if message[0] == DATA]
            for frame in self._input.frames
            if any(is_value(message) for message in frame)
        ]

    @property
    def terminal(self) -> Message | None:
        """The COMPLETE or ERROR this dependency sent in this invocation, if any."""

        for frame in self._input.frames:
            for message in frame:
                if is_terminal(message):
                    return message
        return None

    @property
    def has(self) -> bool:
        """Whether a value has been received from this dependency and not invalidated."""

        return self._input.has

    @property
    def value(self) -> object:
        """The latest value received from this dependency, or ``None`` without one."""

        return self._input.value

    @property
    def invalidated(self) -> bool:
        """Whether an INVALIDATE arrived on this input (manual INVALIDATE policy only)."""

        return self._manual_invalidate and self._input.invalidated


class Ctx:
    """What a node function receives: its inputs, private state and output."""

    __slots__ = ("_deps", "_graph", "_invoking", "_lifetime", "_node", "_out")

    def __init__(self, graph: Graph, node: _Node, lifetime: _Lifetime) -> None:
        self._graph = graph
        self._node = node
        self._lifetime = lifetime
        self._out: list[Array] = []
        self._invoking = True
        self._deps: list[Dep] | None = None

    @property
    def deps(self) -> Sequence[Dep]:
        """Each declared dependency of this lifetime, in order."""

        if self._deps is None:
            node = self._node
            manual = node.invalidate_policy == "manual"
            self._deps = [
                Dep(dep_id, record, manual)
                for dep_id, record in zip(node.deps, node.inputs, strict=True)
            ]
        return self._deps

    @property
    def state(self) -> object:
        """This lifetime's private state (``None`` until set)."""

        return self._node.private_state

    @state.setter
    def state(self, value: object) -> None:
        self._node.private_state = value

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
