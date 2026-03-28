"""Core micro-benchmarks — parity with graphrefly-ts benches (callbag-style shapes)."""

from __future__ import annotations

import pytest

from graphrefly.core import MessageType, batch, node

# Benchmarks run single-threaded; disable locking overhead.
_BENCH_OPTS: dict[str, object] = {"thread_safe": False}

pytestmark = pytest.mark.benchmark


def push(n: object, v: int) -> None:
    n.down([(MessageType.DIRTY,), (MessageType.DATA, v)])  # type: ignore[union-attr]


# ─── Primitives ────────────────────────────────────────────


def test_state_get(benchmark) -> None:
    """state.get()"""
    s = node(initial=0, **_BENCH_OPTS)
    benchmark(s.get)


def test_state_set(benchmark) -> None:
    """state.set()"""
    s = node(initial=0, **_BENCH_OPTS)
    i = [0]

    def run() -> None:
        i[0] += 1
        push(s, i[0])

    benchmark(run)


def test_state_set_with_subscriber(benchmark) -> None:
    """state.set() + subscriber"""
    s = node(initial=0, **_BENCH_OPTS)
    unsub = s.subscribe(lambda _m: None)
    i = [0]

    def run() -> None:
        i[0] += 1
        push(s, i[0])

    try:
        benchmark(run)
    finally:
        unsub()


# ─── Derived ───────────────────────────────────────────────


def test_derived_single_dep_set_get(benchmark) -> None:
    """set + get (derived: single-dep)"""
    a = node(initial=0, **_BENCH_OPTS)
    d = node([a], lambda deps, _: deps[0] * 2, **_BENCH_OPTS)
    unsub = d.subscribe(lambda _m: None)
    i = [0]

    def run() -> None:
        i[0] += 1
        push(a, i[0])
        d.get()

    try:
        benchmark(run)
    finally:
        unsub()


def test_derived_multi_dep_set_one_get(benchmark) -> None:
    """set one dep + get"""
    a = node(initial=0, **_BENCH_OPTS)
    b = node(initial=0, **_BENCH_OPTS)
    d = node([a, b], lambda deps, _: deps[0] + deps[1], **_BENCH_OPTS)
    unsub = d.subscribe(lambda _m: None)
    i = [0]

    def run() -> None:
        i[0] += 1
        push(a, i[0])
        d.get()

    try:
        benchmark(run)
    finally:
        unsub()


def test_derived_cached_get(benchmark) -> None:
    """get (cached)"""
    a = node(initial=5, **_BENCH_OPTS)
    d = node([a], lambda deps, _: deps[0] * 2, **_BENCH_OPTS)
    unsub = d.subscribe(lambda _m: None)

    def run() -> None:
        d.get()

    try:
        benchmark(run)
    finally:
        unsub()


# ─── Diamond ───────────────────────────────────────────────


def test_diamond_set_root_get_leaf(benchmark) -> None:
    """set root + get leaf (diamond: A → B,C → D)"""
    a = node(initial=0, **_BENCH_OPTS)
    b = node([a], lambda deps, _: deps[0] + 1, **_BENCH_OPTS)
    c = node([a], lambda deps, _: deps[0] * 2, **_BENCH_OPTS)
    d = node([b, c], lambda deps, _: deps[0] + deps[1], **_BENCH_OPTS)
    unsub = d.subscribe(lambda _m: None)
    i = [0]

    def run() -> None:
        i[0] += 1
        push(a, i[0])
        d.get()

    try:
        benchmark(run)
    finally:
        unsub()


def test_diamond_deep_set_root_get_leaf(benchmark) -> None:
    """set root + get leaf (diamond: deep 5 levels)"""
    root = node(initial=0, **_BENCH_OPTS)
    l1a = node([root], lambda deps, _: deps[0] + 1, **_BENCH_OPTS)
    l1b = node([root], lambda deps, _: deps[0] * 2, **_BENCH_OPTS)
    l2a = node([l1a, l1b], lambda deps, _: deps[0] + deps[1], **_BENCH_OPTS)
    l2b = node([l1a, l1b], lambda deps, _: deps[0] * deps[1], **_BENCH_OPTS)
    leaf = node([l2a, l2b], lambda deps, _: deps[0] + deps[1], **_BENCH_OPTS)
    unsub = leaf.subscribe(lambda _m: None)
    i = [0]

    def run() -> None:
        i[0] += 1
        push(root, i[0])
        leaf.get()

    try:
        benchmark(run)
    finally:
        unsub()


def test_diamond_wide_set_root_get_leaf(benchmark) -> None:
    """set root + get leaf (diamond: wide 10 intermediates)"""
    root = node(initial=0, **_BENCH_OPTS)
    intermediates = [
        node([root], lambda deps, _a, _j=j: deps[0] + _j, **_BENCH_OPTS) for j in range(10)
    ]
    leaf = node(intermediates, lambda deps: sum(deps), **_BENCH_OPTS)
    unsub = leaf.subscribe(lambda _m: None)
    i = [0]

    def run() -> None:
        i[0] += 1
        push(root, i[0])
        leaf.get()

    try:
        benchmark(run)
    finally:
        unsub()


# ─── Effect ────────────────────────────────────────────────


def test_effect_single_dep(benchmark) -> None:
    """state.set() triggers effect"""
    trigger = node(initial=0, **_BENCH_OPTS)
    unsub = trigger.subscribe(lambda _m: trigger.get())
    i = [0]

    def run() -> None:
        i[0] += 1
        push(trigger, i[0])

    try:
        benchmark(run)
    finally:
        unsub()


def test_effect_multi_dep_diamond(benchmark) -> None:
    """set root, effect runs once"""
    a = node(initial=0, **_BENCH_OPTS)
    b = node([a], lambda deps, _: deps[0] + 1, **_BENCH_OPTS)
    c = node([a], lambda deps, _: deps[0] * 2, **_BENCH_OPTS)
    eff = node([b, c], lambda deps, _: 0, **_BENCH_OPTS)
    unsub = eff.subscribe(lambda _m: None)
    i = [0]

    def run() -> None:
        i[0] += 1
        push(a, i[0])

    try:
        benchmark(run)
    finally:
        unsub()


# ─── Fan-out ───────────────────────────────────────────────


def test_fan_out_10_subscribers(benchmark) -> None:
    """set with 10 subscribers"""
    src = node(initial=0, **_BENCH_OPTS)
    unsubs = [src.subscribe(lambda _m: None) for _ in range(10)]
    i = [0]

    def run() -> None:
        i[0] += 1
        push(src, i[0])

    try:
        benchmark(run)
    finally:
        for u in unsubs:
            u()


def test_fan_out_100_subscribers(benchmark) -> None:
    """set with 100 subscribers"""
    src = node(initial=0, **_BENCH_OPTS)
    unsubs = [src.subscribe(lambda _m: None) for _ in range(100)]
    i = [0]

    def run() -> None:
        i[0] += 1
        push(src, i[0])

    try:
        benchmark(run)
    finally:
        for u in unsubs:
            u()


# ─── Batching ──────────────────────────────────────────────


def test_batch_unbatched_10_sets(benchmark) -> None:
    """unbatched (10 sets)"""
    items = [node(initial=k, **_BENCH_OPTS) for k in range(10)]
    agg = node(items, lambda deps: sum(deps), **_BENCH_OPTS)
    unsub = agg.subscribe(lambda _m: None)
    k = [0]

    def run() -> None:
        for s in items:
            push(s, k[0])
            k[0] += 1

    try:
        benchmark(run)
    finally:
        unsub()


def test_batch_batched_10_sets(benchmark) -> None:
    """batched (10 sets)"""
    items = [node(initial=k, **_BENCH_OPTS) for k in range(10)]
    agg = node(items, lambda deps: sum(deps), **_BENCH_OPTS)
    unsub = agg.subscribe(lambda _m: None)
    k = [0]

    def run() -> None:
        with batch():
            for s in items:
                push(s, k[0])
                k[0] += 1

    try:
        benchmark(run)
    finally:
        unsub()


# ─── Equals ────────────────────────────────────────────────


def test_equals_without(benchmark) -> None:
    """without equals"""
    a1 = node(initial=0, **_BENCH_OPTS)
    b1 = node([a1], lambda deps, _: 1 if deps[0] >= 5 else 0, **_BENCH_OPTS)
    c1 = node([a1], lambda deps, _: deps[0] * 2, **_BENCH_OPTS)
    e1 = node([b1, c1], lambda deps, _: deps[0] + deps[1], **_BENCH_OPTS)
    unsub = e1.subscribe(lambda _m: None)
    k1 = [0]

    def run() -> None:
        k1[0] += 1
        push(a1, k1[0])

    try:
        benchmark(run)
    finally:
        unsub()


def test_equals_with_subtree_skip(benchmark) -> None:
    """with equals (subtree skip)"""
    a2 = node(initial=0, **_BENCH_OPTS)
    b2 = node(
        [a2],
        lambda deps, _: 1 if deps[0] >= 5 else 0,
        equals=lambda x, y: x == y,
        **_BENCH_OPTS,
    )
    c2 = node([a2], lambda deps, _: deps[0] * 2, **_BENCH_OPTS)
    e2 = node([b2, c2], lambda deps, _: deps[0] + deps[1], **_BENCH_OPTS)
    unsub = e2.subscribe(lambda _m: None)
    k2 = [0]

    def run() -> None:
        k2[0] += 1
        push(a2, k2[0])

    try:
        benchmark(run)
    finally:
        unsub()


# ─── GraphReFly extras ─────────────────────────────────────


def test_linear_10_node_chain_dirty_data(benchmark) -> None:
    """linear 10-node chain: DIRTY+DATA"""
    head = node(initial=0, **_BENCH_OPTS)
    cur = head
    for _ in range(9):
        prev = cur
        cur = node([prev], lambda deps, _: deps[0] + 1, **_BENCH_OPTS)
    tail = cur
    unsub = tail.subscribe(lambda _m: None)
    i = [0]

    def run() -> None:
        i[0] += 1
        push(head, i[0])

    try:
        benchmark(run)
    finally:
        unsub()


def test_fan_in_batched_dirty_data_on_two_sources(benchmark) -> None:
    """fan-in: batched DIRTY+DATA on two sources"""
    x = node(initial=0, **_BENCH_OPTS)
    y = node(initial=0, **_BENCH_OPTS)
    s = node([x, y], lambda deps, _: deps[0] + deps[1], **_BENCH_OPTS)
    unsub = s.subscribe(lambda _m: None)
    i = [0]

    def run() -> None:
        n = i[0]
        i[0] += 1
        with batch():
            push(x, n)
            push(y, n + 1)

    try:
        benchmark(run)
    finally:
        unsub()
