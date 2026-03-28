"""Core micro-benchmarks — parity with graphrefly-ts ``src/__bench__/graphrefly.bench.ts``."""

from __future__ import annotations

import pytest

from graphrefly.core import MessageType, batch, node

pytestmark = pytest.mark.benchmark

_n_counter: list[int] = [0]

linear_head = node(initial=0)
_cur = linear_head
for _ in range(9):
    _prev = _cur
    _cur = node([_prev], lambda deps, _: deps[0] + 1)
linear_tail = _cur
_linear_unsub = linear_tail.subscribe(lambda _m: None)

diamond_a = node(initial=0)
diamond_b = node([diamond_a], lambda deps, _: deps[0] + 1)
diamond_c = node([diamond_a], lambda deps, _: deps[0] + 2)
diamond_d = node([diamond_b, diamond_c], lambda deps, _: deps[0] + deps[1])
_diamond_unsub = diamond_d.subscribe(lambda _m: None)

fan1 = node(initial=0)
fan2 = node(initial=0)
fan_sum = node([fan1, fan2], lambda deps, _: deps[0] + deps[1])
_fan_unsub = fan_sum.subscribe(lambda _m: None)


def _next_n() -> int:
    _n_counter[0] += 1
    return _n_counter[0]


def test_linear_10_node_chain_dirty_data(benchmark) -> None:
    """linear 10-node chain: DIRTY+DATA (vitest bench name)."""

    def run() -> None:
        n = _next_n()
        linear_head.down([(MessageType.DIRTY,), (MessageType.DATA, n)])

    benchmark(run)


def test_diamond_single_source_update(benchmark) -> None:
    """diamond: single source update (vitest bench name)."""

    def run() -> None:
        n = _next_n()
        diamond_a.down([(MessageType.DIRTY,), (MessageType.DATA, n)])

    benchmark(run)


def test_fan_in_batched_dirty_data_on_two_sources(benchmark) -> None:
    """fan-in: batched DIRTY+DATA on two sources (vitest bench name)."""

    def run() -> None:
        n = _next_n()
        with batch():
            fan1.down([(MessageType.DIRTY,), (MessageType.DATA, n)])
            fan2.down([(MessageType.DIRTY,), (MessageType.DATA, n + 1)])

    benchmark(run)


@pytest.fixture(scope="module", autouse=True)
def _bench_cleanup() -> None:
    yield
    _linear_unsub()
    _diamond_unsub()
    _fan_unsub()
