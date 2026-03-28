"""Meta companion stores — graphrefly-ts ``src/core/meta.ts``."""

from __future__ import annotations

from typing import Any, Protocol


class _MetaNode(Protocol):
    meta: dict[str, Any]

    def get(self) -> Any: ...


# Re-export for callers that only need the snapshot helper
__all__ = ["meta_snapshot"]


def meta_snapshot(node: _MetaNode) -> dict[str, Any]:
    """Merge current meta field values (for describe-style JSON)."""
    return {key: child.get() for key, child in node.meta.items()}
