"""Protocol shape, message vocabulary, and batch semantics (roadmap 0.2)."""

from __future__ import annotations

from enum import StrEnum

import pytest

from graphrefly.core.protocol import (
    MessageType,
    batch,
    dispatch_messages,
    emit_with_batch,
    is_batching,
)


def test_messages_is_list_of_tuples_not_shorthand() -> None:
    """GRAPHREFLY-SPEC § 1.1 — always a list of tuples, no single-message shorthand."""
    messages: list[tuple[MessageType, int] | tuple[MessageType]] = [
        (MessageType.DATA, 42),
    ]
    assert messages == [(MessageType.DATA, 42)]
    assert isinstance(messages, list)


def test_message_type_enum_matches_spec_appendix() -> None:
    """Roadmap 0.2 — full vocabulary; values are stable StrEnum strings."""
    expected = {
        "DATA",
        "DIRTY",
        "RESOLVED",
        "INVALIDATE",
        "PAUSE",
        "RESUME",
        "TEARDOWN",
        "COMPLETE",
        "ERROR",
    }
    assert {m.value for m in MessageType} == expected
    assert issubclass(MessageType, StrEnum)


def test_batch_defers_data_not_dirty() -> None:
    """GRAPHREFLY-SPEC § 1.3 #7 — DIRTY immediate; DATA deferred until batch exit."""
    log: list[str] = []

    def sink(msgs: list[tuple[MessageType, object] | tuple[MessageType]]) -> None:
        for m in msgs:
            log.append(m[0].value)

    with batch():
        assert is_batching()
        dispatch_messages(
            [(MessageType.DIRTY,), (MessageType.DATA, 1)],
            sink,
        )
        assert log == ["DIRTY"], "DIRTY must not wait for batch exit"

    assert log == ["DIRTY", "DATA"], "DATA flushes after outermost batch exits"
    assert not is_batching()


def test_batch_defers_resolved_like_data() -> None:
    """Phase-2 RESOLVED is deferred with DATA (two-phase settlement)."""
    log: list[str] = []

    def sink(msgs: list[tuple[MessageType, object] | tuple[MessageType]]) -> None:
        for m in msgs:
            log.append(m[0].value)

    with batch():
        dispatch_messages([(MessageType.DIRTY,), (MessageType.RESOLVED,)], sink)
        assert log == ["DIRTY"]

    assert log == ["DIRTY", "RESOLVED"]


def test_nested_batch_flushes_once_at_outer_exit() -> None:
    order: list[str] = []

    def sink(msgs: list[tuple[MessageType, object] | tuple[MessageType]]) -> None:
        for m in msgs:
            order.append(m[0].value)

    with batch():
        dispatch_messages([(MessageType.DATA, "a")], sink)
        with batch():
            dispatch_messages([(MessageType.DATA, "b")], sink)
        assert order == [], "inner exit must not flush deferred DATA"

    assert order == ["DATA", "DATA"]


def test_non_batch_path_delivers_data_immediately() -> None:
    log: list[str] = []

    def sink(msgs: list[tuple[MessageType, object] | tuple[MessageType]]) -> None:
        for m in msgs:
            log.append(m[0].value)

    dispatch_messages([(MessageType.DATA, 1)], sink)
    assert log == ["DATA"]


def test_nested_batch_inside_deferred_callback_drains() -> None:
    """Nested ``batch()`` during deferred work must flush inner DATA (QA nested drain)."""
    log: list[str] = []

    def sink(msgs: list[tuple[MessageType, object] | tuple[MessageType]]) -> None:
        for m in msgs:
            log.append(m[0].value)
            if m == (MessageType.DATA, "outer"):
                with batch():
                    dispatch_messages([(MessageType.DATA, "inner")], sink)

    with batch():
        dispatch_messages([(MessageType.DATA, "outer")], sink)

    assert log == ["DATA", "DATA"], "outer then inner DATA; inner batch must not stick in pending"


def test_terminal_flushes_deferred_phase2_first() -> None:
    """GRAPHREFLY-SPEC § 1.3 #4 — COMPLETE not observed before deferred DATA in one send."""
    log: list[str] = []

    def sink(msgs: list[tuple[MessageType, object] | tuple[MessageType]]) -> None:
        for m in msgs:
            log.append(m[0].value)

    with batch():
        dispatch_messages(
            [(MessageType.DATA, 1), (MessageType.COMPLETE,)],
            sink,
        )
        assert log == ["DATA", "COMPLETE"], "deferred DATA must flush before COMPLETE in one send"

    assert log == ["DATA", "COMPLETE"]


def test_nested_batch_throw_during_drain_does_not_clear_outer_queue_a4() -> None:
    """Nested ``batch()`` error while draining must not wipe the global phase-2 queue."""
    log: list[str] = []

    def deferred_from_outer_data0(
        _msgs: list[tuple[MessageType, object] | tuple[MessageType]],
    ) -> None:
        emit_with_batch(
            lambda _m: log.append("deferred-from-callback"),
            [(MessageType.DATA, 1)],
        )
        with batch():
            raise RuntimeError("inner")

    with pytest.raises(RuntimeError, match="inner"), batch():
        emit_with_batch(deferred_from_outer_data0, [(MessageType.DATA, 0)])

    assert log == ["deferred-from-callback"]


def test_is_batching_true_while_draining_deferred() -> None:
    """Deferred callbacks still see ``is_batching()`` true until the queue drains."""
    seen: list[bool] = []

    def sink(msgs: list[tuple[MessageType, object] | tuple[MessageType]]) -> None:
        seen.append(is_batching())

    with batch():
        dispatch_messages([(MessageType.DATA, 1)], sink)
    assert seen == [True], "sink runs during flush; batching must still be active"


def test_thread_isolated_batch_state() -> None:
    """Batches do not span threads (same idea as predecessor test_batch_is_thread_isolated)."""
    import threading

    results: dict[str, list[bool]] = {"main": [], "worker": []}

    def worker() -> None:
        results["worker"].append(is_batching())
        with batch():
            results["worker"].append(is_batching())
        results["worker"].append(is_batching())

    t = threading.Thread(target=worker)
    with batch():
        results["main"].append(is_batching())
    results["main"].append(is_batching())
    t.start()
    t.join(timeout=5)
    assert results["main"] == [True, False]
    assert results["worker"] == [False, True, False]
