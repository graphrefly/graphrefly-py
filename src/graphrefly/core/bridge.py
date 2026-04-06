"""bridge — graph-visible message forwarding between two nodes.

Replaces ad-hoc ``subscribe()`` bridges that bypass graph topology.
The returned node is an effect that intercepts messages from ``from_node``
and forwards them to ``to_node.down()``. Register it with ``graph.add()`` to
make the bridge visible in ``describe()`` and ``snapshot()``.

**Upstream path:** The bridge node has ``from_node`` as its dep, so anything
downstream of the bridge that calls ``up()`` naturally reaches ``from_node``.
If ``to_node`` is used as a dep by other nodes and those nodes send ``up()``,
the messages reach ``to_node``'s deps (not ``from_node``). For full upstream
relay across the bridge boundary, wire the bridge as a dep of ``to_node``'s
consumers or use ``graph.connect()``.

**ABAC / guards:** ``to_node.down()`` is called without transport options,
so any ABAC guard on ``to_node`` receives ``actor = None``. Upstream
(``up()``) messages propagate through the dep chain the same way — no actor
is injected on either path. Both paths are intentionally unguarded; if
``to_node`` requires a specific actor, provide a guarded wrapper node and
bridge to that instead.

**Default forwarding:** All standard message types are forwarded by
default, including TEARDOWN, PAUSE, RESUME, and INVALIDATE. Use the
``down`` option to restrict which types are forwarded. Callers that need
to exclude TEARDOWN (e.g. inter-stage wiring in ``funnel()``) pass an
explicit ``down`` sequence without TEARDOWN.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

from graphrefly.core.node import NodeImpl, node
from graphrefly.core.protocol import MessageType

DEFAULT_DOWN: frozenset[MessageType] = frozenset(
    {
        MessageType.DATA,
        MessageType.DIRTY,
        MessageType.RESOLVED,
        MessageType.COMPLETE,
        MessageType.ERROR,
        MessageType.TEARDOWN,
        MessageType.PAUSE,
        MessageType.RESUME,
        MessageType.INVALIDATE,
    }
)

# All standard message types the bridge understands. Types outside this set
# are "unknown" and must always be forwarded (spec §1.3.6).
_STANDARD_TYPES: frozenset[MessageType] = frozenset(
    {
        MessageType.DATA,
        MessageType.DIRTY,
        MessageType.RESOLVED,
        MessageType.COMPLETE,
        MessageType.ERROR,
        MessageType.TEARDOWN,
        MessageType.PAUSE,
        MessageType.RESUME,
        MessageType.INVALIDATE,
    }
)


def bridge(
    from_node: NodeImpl[Any],
    to_node: NodeImpl[Any],
    *,
    name: str | None = None,
    down: Sequence[MessageType] | None = None,
) -> NodeImpl[Any]:
    """Create a graph-visible bridge node that forwards messages.

    The bridge is a real node (effect) — it shows up in ``describe()``,
    participates in two-phase push, and cleans up on TEARDOWN. Register
    it via ``graph.add()`` to make it part of the graph topology.

    **Unknown message types** (custom domain signals not in the standard
    protocol set) are always forwarded to ``to_node``, regardless of the
    ``down`` option. This satisfies spec §1.3.6 ("unknown types forward
    unchanged").

    **COMPLETE / ERROR**: when forwarded, the bridge also transitions to
    terminal state so graph-wide completion detection works correctly.

    Args:
        from_node: Source node to observe.
        to_node: Target node to forward messages to via ``to_node.down()``.
        name: Optional node name (for graph registration / describe).
        down: Standard message types to forward. Default: all standard
            types. Unknown (non-standard) types always forward per spec
            §1.3.6 regardless of this option.

    Returns:
        A bridge effect node. Add to a graph with ``graph.add(name, bridge(...))``.

    Example:
        ```python
        from graphrefly import bridge, state, Graph

        a = state(0)
        b = state(0)
        br = bridge(a, b, name="__bridge_a_b")
        g = Graph("test")
        g.add("a", a)
        g.add("b", b)
        g.add("__bridge_a_b", br)
        g.connect("a", "__bridge_a_b")
        ```
    """
    allowed_down = frozenset(down if down is not None else DEFAULT_DOWN)

    def on_message(msg: Any, dep_index: int, actions: Any) -> bool:  # noqa: ARG001
        t = msg[0]

        # Unknown types (custom domain signals) always forward — spec §1.3.6.
        if t not in _STANDARD_TYPES:
            to_node.down([msg])
            return True

        # Known type, not in allowed_down — consume without forwarding.
        if t not in allowed_down:
            return True

        # Forward the message to the target.
        to_node.down([msg])

        # For terminal types, return False so default dispatch also handles
        # terminal state on the bridge itself (bridge completes when from completes).
        return t is not MessageType.COMPLETE and t is not MessageType.ERROR

    return node(
        [from_node],
        None,
        on_message=on_message,
        describe_kind="effect",
        name=name,
    )


__all__ = ["DEFAULT_DOWN", "bridge"]
