"""Meta companion stores — graphrefly-ts ``src/core/meta.ts``."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

from graphrefly.core.guard import access_hint_for_guard
from graphrefly.core.node import NodeImpl  # noqa: TC001 — runtime type for describe_node

__all__ = ["describe_node", "meta_snapshot"]


class _MetaNode(Protocol):
    """Parent node: read-only ``meta`` mapping (e.g. ``MappingProxyType``)."""

    @property
    def meta(self) -> Mapping[str, Any]: ...

    def get(self) -> Any: ...


def meta_snapshot(node: _MetaNode) -> dict[str, Any]:
    """Merge current meta field values into a plain dict for describe-style JSON.

    Values come from each companion node's ``get()`` (last settled cache). If a meta
    field is dirty (``DIRTY`` received, ``DATA`` pending), the snapshot holds the
    previous value — check ``node.meta[key].status`` when freshness matters.
    Keys whose companion ``get()`` raises are omitted so describe tooling keeps other
    fields intact.

    Args:
        node: Any object with a ``meta`` mapping of ``str -> Node`` and a ``get()`` method.

    Returns:
        A plain ``dict`` mapping each meta key to its last settled value.

    Example:
        ```python
        from graphrefly import state
        from graphrefly.core.meta import meta_snapshot
        x = state(0, meta={"version": 1})
        snap = meta_snapshot(x)
        # snap == {"version": 1}
        ```
    """
    out: dict[str, Any] = {}
    for key, child in node.meta.items():
        with suppress(Exception):
            out[key] = child.get()
    return out


def _infer_describe_type(n: NodeImpl[Any]) -> str:
    """Best-effort ``type`` for GRAPHREFLY-SPEC describe node shape (§3.6, Appendix B)."""
    if n._describe_kind is not None:
        return n._describe_kind
    if not n._has_deps:
        return "state" if n._fn is None else "producer"
    if n._fn is None:
        return "derived"
    if n._manual_emit_used:
        return "operator"
    return "derived"


def describe_node(n: NodeImpl[Any]) -> dict[str, Any]:
    """Build a single-node slice of ``Graph.describe()`` JSON (structure + ``meta`` snapshot).

    The ``meta`` field uses :func:`meta_snapshot` (plain values), matching the
    spec's describe shape (GRAPHREFLY-SPEC Appendix B). ``type`` is inferred from
    factory configuration, optional ``describe_kind`` in node options, and the last
    ``_manual_emit_used`` hint (operator vs derived). Sugar constructors
    (``effect``, ``producer``, ``derived``) set ``describe_kind`` automatically.

    Args:
        n: A :class:`~graphrefly.core.node.NodeImpl` to describe.

    Returns:
        A ``dict`` with keys ``type``, ``status``, ``deps``, ``meta``, and optionally
        ``name`` and ``value``.

    Example:
        ```python
        from graphrefly import state
        from graphrefly.core.meta import describe_node
        x = state(42, name="x")
        d = describe_node(x)
        assert d["type"] == "state"
        assert d["value"] == 42
        ```
    """
    out: dict[str, Any] = {
        "type": _infer_describe_type(n),
        "status": n.status,
        "deps": [d.name or "" for d in n._deps],
        "meta": meta_snapshot(n),
    }
    if n.name is not None:
        out["name"] = n.name
    with suppress(Exception):
        out["value"] = n.get()
    g = n._guard
    if g is not None:
        meta = dict(out.get("meta") or {})
        if "access" not in meta:
            meta["access"] = access_hint_for_guard(g)
        out["meta"] = meta
    return out
