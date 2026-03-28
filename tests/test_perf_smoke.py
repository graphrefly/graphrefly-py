"""Loose perf smoke tests — regression guard for catastrophic slowdowns (roadmap 0.7)."""

from __future__ import annotations

import os
import time

from graphrefly.core import MessageType, node


def test_many_sequential_derived_updates_stay_within_loose_budget() -> None:
    """Hot-path sanity: many DIRTY+DATA cycles on a single derived node."""
    src = node(initial=0)
    d = node([src], lambda deps, _: deps[0] + 1)
    _ = d.subscribe(lambda _m: None)
    n = 40_000
    t0 = time.perf_counter()
    for i in range(n):
        src.down([(MessageType.DIRTY,), (MessageType.DATA, i)])
    elapsed = time.perf_counter() - t0
    assert d.get() == n
    # Wall-clock budgets are flaky on shared CI runners; always assert correctness above.
    if os.environ.get("CI"):
        return
    assert elapsed < 30.0, f"expected <30s for {n} updates, took {elapsed:.2f}s"
