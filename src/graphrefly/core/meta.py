"""Meta companion stores — graphrefly-ts ``src/core/meta.ts``."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

from graphrefly.core.dynamic_node import DynamicNodeImpl
from graphrefly.core.guard import access_hint_for_guard
from graphrefly.core.node import NO_VALUE, NodeImpl  # noqa: TC001 — runtime type for describe_node
from graphrefly.core.versioning import is_v1

__all__ = [
    "DescribeDetail",
    "DescribeField",
    "describe_node",
    "meta_snapshot",
    "resolve_describe_fields",
]

# ---------------------------------------------------------------------------
# Progressive disclosure types (Phase 3.3b)
# ---------------------------------------------------------------------------

DescribeDetail = Literal["minimal", "standard", "full"]
DescribeField = str  # "type", "status", "value", "deps", "meta", "v", etc.


def resolve_describe_fields(
    detail: DescribeDetail | None = None,
    fields: list[str] | None = None,
) -> set[str] | None:
    """Resolve which fields to include. fields overrides detail. None = all fields."""
    if fields is not None and len(fields) > 0:
        return set(fields)
    if detail == "standard":
        return {"type", "status", "value", "deps", "meta", "v"}
    if detail == "full":
        return None  # all
    # minimal (default)
    return {"type", "deps"}


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


def _wants_field(include_fields: set[str] | None, field: str) -> bool:
    """Return True if *field* should be included given *include_fields*."""
    if include_fields is None:
        return True
    return field in include_fields


def _wants_opt_in_field(include_fields: set[str] | None, *names: str) -> bool:
    """Return True when one of *names* is included in the resolved field set.

    ``None`` means full mode (all fields) so opt-in fields are included.
    For preset detail levels (``"minimal"``, ``"standard"``), the set won't
    contain these names, so they stay excluded unless explicitly requested via
    ``fields``.
    """
    if include_fields is None:
        return True  # full mode → include everything
    return any(n in include_fields for n in names)


def _wants_meta(include_fields: set[str] | None) -> bool:
    """Return True if any meta content should be included."""
    if include_fields is None:
        return True
    if "meta" in include_fields:
        return True
    return any(f.startswith("meta.") for f in include_fields)


def _filter_meta(full_meta: dict[str, Any], include_fields: set[str]) -> dict[str, Any]:
    """When dotted meta paths are requested (e.g. 'meta.label'), return only those keys."""
    if "meta" in include_fields:
        return full_meta  # whole meta requested
    dotted = {f.split(".", 1)[1] for f in include_fields if f.startswith("meta.")}
    if not dotted:
        return full_meta
    return {k: v for k, v in full_meta.items() if k in dotted}


def describe_node(
    n: NodeImpl[Any] | DynamicNodeImpl[Any],
    include_fields: set[str] | None = None,
) -> dict[str, Any]:
    """Build a single-node slice of ``Graph.describe()`` JSON (structure + ``meta`` snapshot).

    The ``meta`` field uses :func:`meta_snapshot` (plain values), matching the
    spec's describe shape (GRAPHREFLY-SPEC Appendix B). ``type`` is inferred from
    factory configuration, optional ``describe_kind`` in node options, and the last
    ``_manual_emit_used`` hint (operator vs derived). Sugar constructors
    (``effect``, ``producer``, ``derived``) set ``describe_kind`` automatically.

    Args:
        n: A :class:`~graphrefly.core.node.NodeImpl` or
           :class:`~graphrefly.core.dynamic_node.DynamicNodeImpl` to describe.
        include_fields: Optional set of field names to include. ``None`` means all
            fields (full mode). ``type`` and ``deps`` are always included.

    Returns:
        A ``dict`` with keys ``type``, ``deps``, and optionally ``status``, ``meta``,
        ``name``, ``value``, ``v``, ``guard``, and ``last_mutation``.

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
    if isinstance(n, DynamicNodeImpl):
        out: dict[str, Any] = {
            "type": n._describe_kind or "derived",
            "deps": [],
        }
        if _wants_field(include_fields, "status"):
            out["status"] = n.status
        if _wants_meta(include_fields):
            full_meta = meta_snapshot(n)
            g = n._guard
            if g is not None and "access" not in full_meta:
                full_meta["access"] = access_hint_for_guard(g)
            if include_fields is not None:
                out["meta"] = _filter_meta(full_meta, include_fields)
            else:
                out["meta"] = full_meta
        if n.name is not None:
            out["name"] = n.name
        if _wants_field(include_fields, "value"):
            if n._cached is NO_VALUE:
                out["sentinel"] = True
            with suppress(Exception):
                out["value"] = n.get()
        # Guard access hint (standalone opt-in field)
        if _wants_opt_in_field(include_fields, "guard") and n._guard is not None:
            out["guard"] = access_hint_for_guard(n._guard)
        # last_mutation (opt-in)
        if _wants_opt_in_field(include_fields, "lastMutation", "last_mutation"):
            lm = n.last_mutation
            if lm is not None:
                out["last_mutation"] = asdict(lm)
        # Versioning (GRAPHREFLY-SPEC §7)
        if _wants_field(include_fields, "v") and hasattr(n, "v") and n.v is not None:
            out["v"] = _versioning_dict(n.v)
        return out

    out = {
        "type": _infer_describe_type(n),
        "deps": [d.name or "" for d in n._deps],
    }
    if _wants_field(include_fields, "status"):
        out["status"] = n.status
    if _wants_meta(include_fields):
        full_meta = meta_snapshot(n)
        g = n._guard
        if g is not None and "access" not in full_meta:
            full_meta["access"] = access_hint_for_guard(g)
        if include_fields is not None:
            out["meta"] = _filter_meta(full_meta, include_fields)
        else:
            out["meta"] = full_meta
    if n.name is not None:
        out["name"] = n.name
    if _wants_field(include_fields, "value"):
        if n._cached is NO_VALUE:
            out["sentinel"] = True
        with suppress(Exception):
            out["value"] = n.get()
    # Guard access hint (standalone opt-in field)
    if _wants_opt_in_field(include_fields, "guard") and n._guard is not None:
        out["guard"] = access_hint_for_guard(n._guard)
    # last_mutation (opt-in)
    if _wants_opt_in_field(include_fields, "lastMutation", "last_mutation"):
        lm = n.last_mutation
        if lm is not None:
            out["last_mutation"] = asdict(lm)
    # Versioning (GRAPHREFLY-SPEC §7)
    if _wants_field(include_fields, "v") and n.v is not None:
        out["v"] = _versioning_dict(n.v)
    return out


def _versioning_dict(v: Any) -> dict[str, Any]:
    """Convert versioning info to a plain dict for JSON serialization."""
    d: dict[str, Any] = {"id": v.id, "version": v.version}
    if is_v1(v):
        d["cid"] = v.cid
        d["prev"] = v.prev
    return d
