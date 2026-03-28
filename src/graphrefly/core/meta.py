"""Meta companion stores — graphrefly-ts ``src/core/meta.ts``."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from graphrefly.core.node import NodeImpl

__all__ = ["describe_node", "meta_snapshot"]


class _MetaNode(Protocol):
    """Parent node: read-only ``meta`` mapping (e.g. ``MappingProxyType``)."""

    @property
    def meta(self) -> Mapping[str, Any]: ...

    def get(self) -> Any: ...


def meta_snapshot(node: _MetaNode) -> dict[str, Any]:
    """Merge current meta field values (for describe-style JSON).

    Values come from each companion node's ``get()`` (last settled cache). If a meta
    field is dirty (DIRTY received, DATA pending), the snapshot still holds the
    previous value — check ``node.meta[key].status`` when freshness matters.

    Keys whose companion ``get()`` raises are omitted so describe tooling keeps other
    fields.
    """
    out: dict[str, Any] = {}
    for key, child in node.meta.items():
        with suppress(Exception):
            out[key] = child.get()
    return out


def _infer_describe_type(n: NodeImpl[Any]) -> str:
    """Best-effort ``type`` for GRAPHREFLY-SPEC describe node shape (§3.6, Appendix B)."""
    if not n._has_deps:
        return "state" if n._fn is None else "producer"
    if n._fn is None:
        return "derived"
    if n._manual_emit_used:
        return "operator"
    return "derived"


def describe_node(n: NodeImpl[Any]) -> dict[str, Any]:
    """Single-node slice of ``Graph.describe()`` JSON (structure + ``meta`` snapshot).

    Intended for Phase 1 ``graph.describe()`` to merge per-node entries. The ``meta``
    field uses :func:`meta_snapshot` (plain values), matching the spec's describe
    examples.

    ``type`` is inferred from configuration and last-run hints (``_manual_emit_used``).
    Nodes whose function returns ``None`` without using ``down()``/``emit()`` may still
    be reported as ``"derived"`` until sugar constructors supply explicit kinds
    (roadmap 0.6).
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
    return out
