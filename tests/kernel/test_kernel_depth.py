"""Long chains: delivery, activation and deactivation need no deep Python stack.

The D797 review (graphrefly-py, Q15) found that a recursive kernel broke a chain of
about 330 nodes. These tests run chains several times the interpreter's recursion
limit through every path that used to recurse: activation, a wave, an INVALIDATE, a
terminal, deactivation, a replace at the root and a checkpoint round trip.
"""

from __future__ import annotations

import sys

from graphrefly._checkpoint import Built, RecipeContext, capture, recipe, restore
from graphrefly._kernel import COMPLETE, DATA, Ctx, Graph

DEPTH = 3 * sys.getrecursionlimit()  # 3,000 with the default limit


def passes(ctx: Ctx) -> None:
    for wave in ctx.deps[0].waves:
        if wave:
            ctx.down([(DATA, wave[-1])])


def chain(g: Graph, depth: int = DEPTH, **opts: object) -> str:
    g.state("n0", 0)
    for i in range(1, depth + 1):
        g.node(f"n{i}", [f"n{i - 1}"], passes, **opts)  # type: ignore[arg-type]
    return f"n{depth}"


def test_a_long_chain_activates_runs_invalidates_ends_and_deactivates() -> None:
    reports: list[object] = []
    g = Graph(report=reports.append)
    last = chain(g)
    seen: list[tuple[object, ...]] = []
    g.subscribe("s", last, seen.append)
    assert seen == [(("START",), (DATA, 0))]
    g.set("n0", 1)
    assert seen[-1] == (("PENDING",), (DATA, 1))
    g.invalidate("n0")
    assert seen[-1] == (("PENDING",), ("INVALIDATE",)) and not g.get(last).has
    g.set("n0", 2)
    g.complete("n0")
    assert g.get(last).ended == (COMPLETE,) and g.get(last).value == 2
    assert all(g.get(f"n{i}").ended == (COMPLETE,) for i in range(1, DEPTH + 1))
    g.unsubscribe("s", last)
    assert reports == []
    assert g.idle()


def test_a_long_chain_deactivates_every_node_and_runs_cleanups_upstream_first() -> None:
    g = Graph()
    closed: list[int] = []
    g.state("n0", 0)
    for i in range(1, DEPTH + 1):

        def setup(ctx: Ctx, i: int = i) -> object:
            return lambda: closed.append(i)

        g.node(f"n{i}", [f"n{i - 1}"], passes, setup=setup)
    g.subscribe("s", f"n{DEPTH}")
    assert all(g.get(f"n{i}").active for i in range(DEPTH + 1))
    g.unsubscribe("s", f"n{DEPTH}")
    assert not any(g.get(f"n{i}").active for i in range(1, DEPTH + 1))
    # A node's cleanup runs after its dependencies were released (R-lifetime).
    assert closed == list(range(1, DEPTH + 1))


def test_a_replace_at_the_root_of_a_long_chain_reaches_the_end() -> None:
    g = Graph()
    g.state("s", 7)
    g.node("n0", ["s"], passes)
    for i in range(1, DEPTH + 1):
        g.node(f"n{i}", [f"n{i - 1}"], passes)
    g.subscribe("k", f"n{DEPTH}")
    g.replace("n0", ["s"], lambda ctx: ctx.down([(DATA, ctx.deps[0].value * 2)]))
    assert g.get(f"n{DEPTH}").value == 14


def test_a_long_quiet_chain_and_a_long_diamond_ladder() -> None:
    g = Graph()
    last = chain(g, quiet=True)
    g.subscribe("k", last)
    g.set("n0", 3)
    assert g.get(last).value == 3

    g = Graph()
    g.state("a0", 0)
    g.state("b0", 0)
    for i in range(1, DEPTH // 2):
        both = [f"a{i - 1}", f"b{i - 1}"]
        g.node(f"a{i}", both, lambda ctx: ctx.down([(DATA, sum(d.value for d in ctx.deps))]))
        g.node(f"b{i}", both, lambda ctx: ctx.down([(DATA, 0)]))
    top = f"a{DEPTH // 2 - 1}"
    g.subscribe("k", top)
    g.set("a0", 1)
    assert g.get(top).value == 1


def test_a_long_chain_round_trips_through_a_checkpoint() -> None:
    def factory(config: object, at: RecipeContext) -> Built:
        return Built(passes)

    g = Graph()
    g.state("n0", 5)
    for i in range(1, DEPTH + 1):
        g.node(f"n{i}", [f"n{i - 1}"], passes, recipe=recipe("pass"))
    g.subscribe("k", f"n{DEPTH}")
    r = restore(capture(g), {"pass": factory})
    r.subscribe("k", f"n{DEPTH}")
    assert r.get(f"n{DEPTH}").value == 5
    r.set("n0", 6)
    assert r.get(f"n{DEPTH}").value == 6
