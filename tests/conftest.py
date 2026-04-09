"""Shared pytest configuration for graphrefly tests."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

import pytest

from graphrefly.core.runner import set_default_runner


class _ThreadRunner:
    """Minimal test-only runner that spawns a thread per coroutine.

    Tests that exercise async sources without an explicit runner need a
    default.  This provides one without pulling in AsyncioRunner (which
    requires a running event loop).
    """

    __slots__ = ("_scheduled", "_completed")

    def __init__(self) -> None:
        self._scheduled = 0
        self._completed = 0

    def schedule(self, coro: Any, on_result: Any, on_error: Any) -> Any:
        self._scheduled += 1

        def _run() -> None:
            try:
                result = asyncio.run(coro)
            except BaseException as err:
                on_error(err)
            else:
                on_result(result)
            finally:
                self._completed += 1

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return lambda: None

    def __repr__(self) -> str:
        pending = self._scheduled - self._completed
        return (
            f"_ThreadRunner(scheduled={self._scheduled}, "
            f"completed={self._completed}, pending={pending})"
        )


@pytest.fixture(autouse=True)
def _set_test_runner() -> Generator[None]:
    """Provide a thread-based default runner for sync tests that use async sources."""
    set_default_runner(_ThreadRunner())
    yield
    set_default_runner(None)
