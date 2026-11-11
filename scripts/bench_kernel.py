"""Reference measurement of graphrefly._kernel and the public facade on the
python-kernel-benchmark scenarios.

The scenarios (chain, fan, batch, build and subscribe; node functions ``inc`` and
``heavy``) follow graphrefly sessions/active/python-kernel-benchmark/bench.py. Each
step is one host input and the cost is reported per node run, best of five. This is
a reference figure for graphrefly-py:D797-KERNEL-PY, not a gate. The facade column
runs the same scenarios through ``graphrefly.Graph`` (state, derived, subscribe, set,
batch), so the gap between the two columns is the facade's own cost.

    PYTHONPATH=src uv run python scripts/bench_kernel.py
    PYTHONPATH=src uv run python scripts/bench_kernel.py \
        --reference ../graphrefly/sessions/active/python-kernel-benchmark

``--reference`` also runs that directory's ``kernel_fast`` port for comparison; it is
imported from the given path and only read.
"""

from __future__ import annotations

import argparse
import gc
import importlib
import platform
import sys
import time
from collections.abc import Callable
from typing import Any

import graphrefly
from graphrefly._kernel import DATA, Ctx, Graph


def inc(x: int) -> int:
    return x + 1


def add(*xs: int) -> int:
    return sum(xs)


def heavy(x: int) -> int:  # about 1 microsecond of real Python work per run
    s = 0
    for i in range(40):
        s += i * x
    return x + 1


WORK = {"inc": (inc, add), "heavy": (heavy, add)}


def derived(f: Callable[..., Any]) -> Callable[[Ctx], None]:
    """The facade's derived sugar in its plainest form: gate on values, recompute on DATA."""

    def run(ctx: Ctx) -> None:
        deps = ctx.deps
        if all(d.has for d in deps) and any(d.waves for d in deps):
            ctx.down([(DATA, f(*(d.value for d in deps)))])

    return run


class Kernel:
    name = "graphrefly._kernel"

    def __init__(self) -> None:
        self.g = Graph()
        self.i = 0

    def state(self, v: object) -> str:
        self.i += 1
        return self.g.state(f"n{self.i}", v)

    def derived(self, deps: list[str], fn: Callable[..., Any]) -> str:
        self.i += 1
        return self.g.node(f"n{self.i}", deps, derived(fn))

    def sink(self, n: str) -> None:
        self.g.subscribe("sink" + n, n, lambda m: None)

    def set(self, n: str, v: object) -> None:
        self.g.set(n, v)

    def batch(self, body: Callable[[], object]) -> None:
        batch = getattr(self.g, "batch", None)
        if batch is None:
            raise NotImplementedError
        batch(body)


class Facade(Kernel):
    name = "graphrefly.Graph"

    def __init__(self) -> None:  # one graph per scenario, as in the kernel column
        self.fg = graphrefly.Graph()
        self.nodes: dict[str, Any] = {}
        self.subs: list[graphrefly.Subscription] = []
        self.i = 0

    def state(self, v: object) -> str:
        self.i += 1
        n = f"n{self.i}"
        self.nodes[n] = self.fg.state(v, n)
        return n

    def derived(self, deps: list[str], fn: Callable[..., Any]) -> str:
        self.i += 1
        n = f"n{self.i}"
        self.nodes[n] = self.fg.derived([self.nodes[d] for d in deps], fn, n)
        return n

    def sink(self, n: str) -> None:
        self.subs.append(self.nodes[n].subscribe(lambda array: None))

    def set(self, n: str, v: object) -> None:
        self.nodes[n].set(v)

    def batch(self, body: Callable[[], object]) -> None:
        self.fg.batch(body)


def reference_runner(module: Any) -> type[Kernel]:
    class Reference(Kernel):
        name = "reference kernel_fast"

        def __init__(self) -> None:
            self.g = module.Graph()
            self.i = 0

        def derived(self, deps: list[str], fn: Callable[..., Any]) -> str:
            self.i += 1
            return str(self.g.node(f"n{self.i}", deps, module.derived_direct(fn)))

        def batch(self, body: Callable[[], object]) -> None:
            graph: Any = self.g
            graph.batch(body)

    return Reference


Step = tuple[Callable[[], None], int]


def chain(r_cls: type[Kernel], work: str, depth: int = 100) -> Step:
    f, _ = WORK[work]
    r = r_cls()
    s = r.state(0)
    n = s
    for _ in range(depth):
        n = r.derived([n], f)
    r.sink(n)
    v = [0]

    def step() -> None:
        v[0] += 1
        r.set(s, v[0])

    return step, depth


def fan(r_cls: type[Kernel], work: str, width: int = 100) -> Step:
    f, g = WORK[work]
    r = r_cls()
    s = r.state(0)
    mids = [r.derived([s], f) for _ in range(width)]
    out = r.derived(mids, g)
    r.sink(out)
    v = [0]

    def step() -> None:
        v[0] += 1
        r.set(s, v[0])

    return step, width + 1


def batch(r_cls: type[Kernel], work: str, width: int = 100) -> Step:
    f, g = WORK[work]
    r = r_cls()
    ss = [r.state(0) for _ in range(width)]
    mids = [r.derived([s], f) for s in ss]
    out = r.derived(mids, g)
    r.sink(out)
    v = [0]

    def body() -> None:
        for s in ss:
            r.set(s, v[0])

    def step() -> None:
        v[0] += 1
        r.batch(body)

    return step, width + 1


def build(r_cls: type[Kernel], work: str, width: int = 200) -> Step:
    f, _ = WORK[work]

    def step() -> None:
        r = r_cls()
        s = r.state(0)
        for _ in range(width):
            r.sink(r.derived([s], f))

    return step, width


def time_it(
    make: Callable[[type[Kernel], str], Step],
    r_cls: type[Kernel],
    work: str,
    budget: float = 0.4,
    repeats: int = 5,
) -> tuple[float, float] | None:
    try:
        step, runs = make(r_cls, work)
        step()  # warm
    except NotImplementedError:
        return None
    n = 1
    while True:
        t = time.perf_counter()
        for _ in range(n):
            step()
        if time.perf_counter() - t > 0.05:
            break
        n *= 2
    n = max(1, int(n * budget / 0.05 / repeats / 2))
    best = float("inf")
    gc.collect()
    gc.disable()
    try:
        for _ in range(repeats):
            t = time.perf_counter()
            for _ in range(n):
                step()
            best = min(best, (time.perf_counter() - t) / n)
    finally:
        gc.enable()
    return best * 1e6, best * 1e6 / runs


SCENARIOS: list[tuple[str, Callable[[type[Kernel], str], Step]]] = [
    ("chain 100 (per set)", chain),
    ("fan 100 -> 1 (per set)", fan),
    ("batch 100 states (per batch)", batch),
    ("build+subscribe 200 nodes", build),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reference", help="python-kernel-benchmark directory to compare with")
    args = parser.parse_args()
    runners: list[type[Kernel]] = [Kernel, Facade]
    if args.reference:
        sys.path.insert(0, args.reference)
        runners.append(reference_runner(importlib.import_module("kernel_fast")))
    print(f"Python {platform.python_version()} {platform.machine()}")
    for work in ("inc", "heavy"):
        print(f"\n== node function: {work} ==")
        print(f"{'scenario':<30}" + "".join(f"{r.name:>28}" for r in runners))
        for label, make in SCENARIOS:
            cells = []
            for r_cls in runners:
                result = time_it(make, r_cls, work)
                cells.append(
                    "n/a" if result is None else f"{result[0]:,.0f}us ({result[1]:.2f}/node)"
                )
            print(f"{label:<30}" + "".join(f"{c:>28}" for c in cells))


if __name__ == "__main__":
    main()
