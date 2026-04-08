"""Approximate in-memory size estimation for arbitrary Python values.

Uses a recursive walk with cycle detection. Not exact — provides a
reasonable approximation for profiling and hotspot detection.

.. note:: Internal module.
"""

from __future__ import annotations

import sys
from typing import Any

# Approximate per-type overhead in bytes (CPython heuristics).
_OVERHEAD = {
    "object": 56,
    "dict": 64,
    "list": 56,
    "tuple": 40,
    "str": 49,  # header; content added separately
    "bytes": 33,
    "int": 28,
    "float": 24,
    "bool": 28,
    "none": 16,
    "set": 200,
    "frozenset": 200,
}


def sizeof(value: Any) -> int:
    """Estimate the approximate retained memory (in bytes) of a Python value.

    Handles primitives, dicts, lists, tuples, sets, frozensets, bytes,
    and nested combinations.  Uses an ``id``-set for cycle detection —
    cyclic refs are counted once.

    Args:
        value: The value to measure.

    Returns:
        Approximate size in bytes.
    """
    seen: set[int] = set()
    return _sizeof(value, seen)


def _sizeof(value: Any, seen: set[int]) -> int:
    if value is None:
        return _OVERHEAD["none"]

    obj_id = id(value)

    t = type(value)

    if t is int:
        return sys.getsizeof(value)
    if t is float:
        return _OVERHEAD["float"]
    if t is bool:
        return _OVERHEAD["bool"]
    if t is str:
        return _OVERHEAD["str"] + len(value)  # approximate (UTF-8 internal)
    if t is bytes:
        return _OVERHEAD["bytes"] + len(value)

    # Reference types — cycle detection
    if obj_id in seen:
        return 0
    seen.add(obj_id)

    if t is dict or isinstance(value, dict):
        size = _OVERHEAD["dict"]
        for k, v in value.items():
            size += _sizeof(k, seen) + _sizeof(v, seen)
        return size

    if t is list or isinstance(value, list):
        size = _OVERHEAD["list"] + len(value) * 8  # pointer slots
        for item in value:
            size += _sizeof(item, seen)
        return size

    if t is tuple or isinstance(value, tuple):
        size = _OVERHEAD["tuple"] + len(value) * 8
        for item in value:
            size += _sizeof(item, seen)
        return size

    if t is set or t is frozenset or isinstance(value, (set, frozenset)):
        size = _OVERHEAD["set"]
        for item in value:
            size += _sizeof(item, seen)
        return size

    if isinstance(value, (bytearray, memoryview)):
        return len(value)

    # Dataclass or slotted object — approximate via __dict__ or __slots__
    if hasattr(value, "__dict__"):
        size = _OVERHEAD["object"]
        for k, v in value.__dict__.items():
            size += _sizeof(k, seen) + _sizeof(v, seen)
        return size

    if hasattr(value, "__slots__"):
        size = _OVERHEAD["object"]
        for slot in value.__slots__:
            attr = getattr(value, slot, None)
            if attr is not None:
                size += _sizeof(slot, seen) + _sizeof(attr, seen)
        return size

    # Fallback — use sys.getsizeof for unknown types
    return sys.getsizeof(value)
