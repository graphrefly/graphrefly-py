"""Operator protocol matrix — systematic verification of every operator against
all message types.

Mirrors the TS operator protocol tests. Each operator is tested for:
1. DIRTY propagation — downstream sees DIRTY when upstream sends DIRTY
2. RESOLVED suppression — downstream emits RESOLVED when value unchanged
3. ERROR propagation — downstream forwards ERROR from upstream
4. COMPLETE propagation — downstream forwards COMPLETE from upstream
5. Bare [DATA] rejection — bare DATA tuple (no payload) is silently skipped

# Spec: GRAPHREFLY-SPEC §1.2, §1.3
"""

from __future__ import annotations

from typing import Any

from graphrefly.core.protocol import Messages, MessageType
from graphrefly.core.sugar import pipe, state
from graphrefly.extra.tier1 import filter, map, scan

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect(n: Any) -> list[Messages]:
    """Subscribe and return collected message batches."""
    sink: list[Messages] = []
    n.subscribe(sink.append)
    return sink


def _flat_types(sink: list[Messages]) -> list[MessageType]:
    """Flatten all message batches and extract types."""
    return [m[0] for batch in sink for m in batch]


# ---------------------------------------------------------------------------
# map
# ---------------------------------------------------------------------------


class TestMapProtocolMatrix:
    def test_dirty_propagation(self) -> None:
        s = state(1)
        m = pipe(s, map(lambda x: x * 2))
        sink = _collect(m)
        sink.clear()
        s.down([(MessageType.DIRTY,), (MessageType.DATA, 2)])
        types = _flat_types(sink)
        assert MessageType.DIRTY in types
        assert MessageType.DATA in types

    def test_resolved_suppression(self) -> None:
        s = state(1)
        m = pipe(s, map(lambda x: "same", equals=lambda a, b: a == b))
        sink = _collect(m)
        sink.clear()
        s.down([(MessageType.DIRTY,), (MessageType.DATA, 2)])
        types = _flat_types(sink)
        assert MessageType.RESOLVED in types
        assert MessageType.DATA not in types

    def test_error_propagation(self) -> None:
        s = state(1)
        m = pipe(s, map(lambda x: x * 2))
        sink = _collect(m)
        sink.clear()
        err = Exception("boom")
        s.down([(MessageType.ERROR, err)])
        types = _flat_types(sink)
        assert MessageType.ERROR in types

    def test_complete_propagation(self) -> None:
        s = state(1)
        m = pipe(s, map(lambda x: x * 2))
        sink = _collect(m)
        sink.clear()
        s.down([(MessageType.COMPLETE,)])
        types = _flat_types(sink)
        assert MessageType.COMPLETE in types

    def test_bare_data_skipped(self) -> None:
        s = state(1)
        m = pipe(s, map(lambda x: x * 2))
        _collect(m)
        assert m.get() == 2
        # Bare [DATA] without payload — should not crash or change value
        s.down([(MessageType.DATA,)])
        assert m.get() == 2  # unchanged


# ---------------------------------------------------------------------------
# filter
# ---------------------------------------------------------------------------


class TestFilterProtocolMatrix:
    def test_dirty_propagation(self) -> None:
        s = state(2)
        f = pipe(s, filter(lambda x: x > 0))
        sink = _collect(f)
        sink.clear()
        s.down([(MessageType.DIRTY,), (MessageType.DATA, 4)])
        types = _flat_types(sink)
        assert MessageType.DIRTY in types

    def test_resolved_on_filtered_value(self) -> None:
        s = state(2)
        f = pipe(s, filter(lambda x: x > 0))
        sink = _collect(f)
        sink.clear()
        # Value changes but filter output remains same (still positive)
        s.down([(MessageType.DIRTY,), (MessageType.DATA, 3)])
        types = _flat_types(sink)
        # filter should emit DATA or RESOLVED depending on equals
        assert MessageType.DATA in types or MessageType.RESOLVED in types

    def test_error_propagation(self) -> None:
        s = state(2)
        f = pipe(s, filter(lambda x: x > 0))
        sink = _collect(f)
        sink.clear()
        s.down([(MessageType.ERROR, Exception("boom"))])
        types = _flat_types(sink)
        assert MessageType.ERROR in types

    def test_complete_propagation(self) -> None:
        s = state(2)
        f = pipe(s, filter(lambda x: x > 0))
        sink = _collect(f)
        sink.clear()
        s.down([(MessageType.COMPLETE,)])
        types = _flat_types(sink)
        assert MessageType.COMPLETE in types

    def test_bare_data_skipped(self) -> None:
        s = state(2)
        f = pipe(s, filter(lambda x: x > 0))
        _collect(f)
        assert f.get() == 2
        s.down([(MessageType.DATA,)])
        assert f.get() == 2


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


class TestScanProtocolMatrix:
    def test_dirty_propagation(self) -> None:
        s = state(1)
        sc = pipe(s, scan(lambda acc, val: acc + val, 0))
        sink = _collect(sc)
        sink.clear()
        s.down([(MessageType.DIRTY,), (MessageType.DATA, 2)])
        types = _flat_types(sink)
        assert MessageType.DIRTY in types
        assert MessageType.DATA in types

    def test_resolved_suppression(self) -> None:
        s = state(0)
        # scan with equals: accumulator stays 0 when adding 0
        sc = pipe(s, scan(lambda acc, val: acc + val, 0, equals=lambda a, b: a == b))
        sink = _collect(sc)
        sink.clear()
        s.down([(MessageType.DIRTY,), (MessageType.DATA, 0)])
        types = _flat_types(sink)
        assert MessageType.RESOLVED in types
        assert MessageType.DATA not in types

    def test_error_propagation(self) -> None:
        s = state(1)
        sc = pipe(s, scan(lambda acc, val: acc + val, 0))
        sink = _collect(sc)
        sink.clear()
        s.down([(MessageType.ERROR, Exception("boom"))])
        types = _flat_types(sink)
        assert MessageType.ERROR in types

    def test_complete_propagation(self) -> None:
        s = state(1)
        sc = pipe(s, scan(lambda acc, val: acc + val, 0))
        sink = _collect(sc)
        sink.clear()
        s.down([(MessageType.COMPLETE,)])
        types = _flat_types(sink)
        assert MessageType.COMPLETE in types

    def test_bare_data_skipped(self) -> None:
        s = state(1)
        sc = pipe(s, scan(lambda acc, val: acc + val, 0))
        _collect(sc)
        assert sc.get() == 1  # 0 + 1
        s.down([(MessageType.DATA,)])
        assert sc.get() == 1  # unchanged
