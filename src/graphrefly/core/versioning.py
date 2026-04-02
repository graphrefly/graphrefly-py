"""Node versioning — GRAPHREFLY-SPEC §7.

Progressive, optional versioning for node identity and change tracking.

- **V0**: ``id`` + ``version`` — identity & change detection (~16 bytes overhead)
- **V1**: + ``cid`` + ``prev`` — content addressing & linked history (~60 bytes overhead)

**Lifecycle notes:**

- Version advances only on DATA (not RESOLVED, INVALIDATE, or TEARDOWN).
- ``reset_on_teardown`` clears the cached value but does NOT reset versioning state.
  After teardown, ``v.cid`` still reflects the last DATA value, not the cleared cache.
  The invariant ``hash(node.get()) == v.cid`` only holds in ``settled``/``resolved`` status.
- Resubscribable nodes preserve versioning across subscription lifetimes (monotonic counter).
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "V0",
    "V1",
    "NodeVersionInfo",
    "VersioningLevel",
    "HashFn",
    "create_versioning",
    "advance_version",
    "default_hash",
    "canonicalize_for_hash",
    "is_v1",
]

type VersioningLevel = int  # 0 or 1; extensible to 2, 3 later
type HashFn = Callable[[Any], str]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class V0:
    """V0: identity + monotonic version counter."""

    id: str
    version: int = 0


@dataclass
class V1(V0):
    """V1: V0 + content-addressed identifier + previous cid link."""

    cid: str = ""
    prev: str | None = None


# Union alias for type hints.
type NodeVersionInfo = V0 | V1


# ---------------------------------------------------------------------------
# Canonical normalizer
# ---------------------------------------------------------------------------


def canonicalize_for_hash(value: Any) -> Any:
    """Normalize *value* into a JSON-safe canonical form for deterministic hashing.

    - ``None`` → ``None``
    - ``float``: rejects non-finite (``NaN``, ``±Inf``) with ``TypeError``;
      normalizes integer-valued floats to ``int`` (``1.0`` → ``1``).
    - ``int``, ``str``, ``bool``: pass through unchanged.
    - ``list`` / ``tuple``: recursively canonicalize each element, return as ``list``.
    - ``dict``: sort keys, recursively canonicalize values, return as sorted ``dict``.
    - Fallback: return ``None``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            msg = f"Cannot canonicalize non-finite float: {value}"
            raise TypeError(msg)
        if value == int(value):
            iv = int(value)
            if abs(iv) > 2**53 - 1:
                msg = (
                    f"Cannot hash integer outside safe range (|n| > 2^53-1): {value}. "
                    "Cross-language cid parity is not guaranteed for unsafe integers."
                )
                raise TypeError(msg)
            return iv
        return value
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            msg = (
                f"Cannot hash integer outside safe range (|n| > 2^53-1): {value}. "
                "Cross-language cid parity is not guaranteed for unsafe integers."
            )
            raise TypeError(msg)
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return [canonicalize_for_hash(item) for item in value]
    if isinstance(value, dict):
        return {k: canonicalize_for_hash(v) for k, v in sorted(value.items())}
    return None


# ---------------------------------------------------------------------------
# Default hash
# ---------------------------------------------------------------------------


def default_hash(value: Any) -> str:
    """SHA-256 of deterministic JSON, truncated to 16 hex chars (~64-bit).

    Object keys are sorted for determinism. Values are canonicalized first
    via :func:`canonicalize_for_hash`.
    """
    canonical = canonicalize_for_hash(value)
    json_bytes = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(json_bytes).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_versioning(
    level: VersioningLevel,
    initial_value: Any = None,
    *,
    id: str | None = None,
    hash_fn: HashFn | None = None,
) -> NodeVersionInfo:
    """Create initial versioning state for a node.

    Args:
        level: 0 for V0, 1 for V1.
        initial_value: The node's initial cached value (used for V1 cid).
        id: Override auto-generated id.
        hash_fn: Custom hash function for V1 cid (default: SHA-256 truncated).
    """
    # RFC 4122 string with hyphens — matches TypeScript `crypto.randomUUID()`.
    node_id = id or str(uuid.uuid4())
    if level == 0:
        return V0(id=node_id)
    h = hash_fn or default_hash
    cid = h(initial_value)
    return V1(id=node_id, cid=cid, prev=None)


# ---------------------------------------------------------------------------
# Advance
# ---------------------------------------------------------------------------


def advance_version(
    info: NodeVersionInfo,
    new_value: Any,
    hash_fn: HashFn,
) -> None:
    """Advance versioning state after a DATA emission (value changed).

    Mutates ``info`` in place for performance (called on every DATA).
    Only call when the cached value has actually changed (not on RESOLVED).
    """
    info.version += 1
    if isinstance(info, V1):
        info.prev = info.cid
        info.cid = hash_fn(new_value)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def is_v1(info: NodeVersionInfo) -> bool:
    """Type guard: is this V1 versioning info?"""
    return isinstance(info, V1)
