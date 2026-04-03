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

    __slots__ = ()

    def schedule(self, coro: Any, on_result: Any, on_error: Any) -> Any:
        def _run() -> None:
            try:
                result = asyncio.run(coro)
            except BaseException as err:
                on_error(err)
            else:
                on_result(result)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return lambda: None


@pytest.fixture(autouse=True)
def _set_test_runner() -> Generator[None]:
    """Provide a thread-based default runner for sync tests that use async sources."""
    set_default_runner(_ThreadRunner())
    yield
    set_default_runner(None)
