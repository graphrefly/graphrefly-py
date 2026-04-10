"""Shared internal utilities for the patterns layer.

These are private helpers used across multiple pattern modules. They are NOT
part of the public API.

General-purpose reactive utilities (``keepalive``, ``reactive_counter``) live
in ``extra.sources`` and are re-exported here for convenience.

.. note:: Internal module — do not import from user code.
"""

from __future__ import annotations

from typing import Any

# Re-export general-purpose utilities from extra (canonical home).
from graphrefly.extra.sources import keepalive, reactive_counter


def domain_meta(
    domain: str,
    kind: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a domain metadata dict for pattern-layer nodes.

    Each domain (orchestration, messaging, reduction, ai, cqrs,
    domain_template) follows the same shape::

        { "<domain>": True, "<domain>_type": "<kind>", ...extra }
    """
    out: dict[str, Any] = {domain: True, f"{domain}_type": kind}
    if extra is not None:
        out.update(extra)
    return out


def tracking_key(item: Any) -> str:
    """Stable tracking key for an item with retry/reingestion decoration.

    Uses ``related_to[0]`` if present (carries the original key forward
    through retries and reingestions). Falls back to ``summary`` for
    first-time items.
    """
    related = (
        item.get("related_to") if isinstance(item, dict) else getattr(item, "related_to", None)
    )
    if related:
        first = related[0] if isinstance(related, (list, tuple)) else None
        if first is not None:
            return str(first)
    if isinstance(item, dict):
        summary = item.get("summary", str(item))
    else:
        summary = getattr(item, "summary", str(item))
    return str(summary)


__all__ = [
    "domain_meta",
    "keepalive",
    "reactive_counter",
    "tracking_key",
]
