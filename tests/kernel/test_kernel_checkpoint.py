"""Checkpoint phase: capture and restore over the kernel (R-checkpoint, graphrefly:D802).

Scenario C-38 of graphrefly spec/conformance.jsonl, qualified against
graphrefly._checkpoint over graphrefly._kernel, plus the reference module's
kernel-graph behavior. The history, pause, demand and restart round trips are C-45
and C-46 and wait for those modules.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from conftest import during

from graphrefly._checkpoint import (
    Built,
    Checkpoint,
    CheckpointError,
    Factory,
    RecipeContext,
    capture,
    recipe,
    restore,
)
from graphrefly._kernel import COMPLETE, DATA, ERROR, Ctx, Graph


def build(
    g: Graph,
    reg: dict[str, Factory],
    node_id: str,
    deps: list[str],
    ref: str,
    config: object = None,
    **opts: Any,
) -> None:
    """Build ``node_id`` from a registered recipe, as restore calls it, and give it the recipe."""

    def make(r: str, c: object, at: str) -> Built:
        built = reg[r](c, RecipeContext(g, at, make))
        return built if isinstance(built, Built) else Built(built)

    built = make(ref, config, node_id)
    g.node(node_id, deps, built.fn, setup=built.setup, recipe=recipe(ref, config), **opts)


class Registry(dict[str, Factory]):
    """Recipes that count how often a real function runs; restore must not run one itself."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = {"sum": 0, "scan": 0}

        def sum_(config: object, at: RecipeContext) -> Callable[[Ctx], None]:
            def fn(ctx: Ctx) -> None:
                self.calls["sum"] += 1
                ctx.down([(DATA, sum(d.value or 0 for d in ctx.deps))])

            return fn

        def scan(config: object, at: RecipeContext) -> Callable[[Ctx], None]:
            def fn(ctx: Ctx) -> None:
                self.calls["scan"] += 1
                acc = ctx.state if isinstance(ctx.state, int) else 0
                acc += sum(v for d in ctx.deps for w in d.waves for v in w)  # type: ignore[misc]
                ctx.state = acc
                ctx.down([(DATA, acc)])

            return fn

        def plus(config: Any, at: RecipeContext) -> Callable[[Ctx], None]:
            return lambda ctx: ctx.down([(DATA, ctx.deps[0].value + config["k"])])

        self["sum"] = sum_
        self["scan"] = scan
        self["plus"] = plus


def original(reg: Registry | None = None) -> Graph:
    """a=1, b=2; s = a+b; t = running total of a and b. Subscribed, then a set to 3."""

    reg = reg if reg is not None else Registry()
    g = Graph(trace=True)
    g.state("a", 1)
    g.state("b", 2)
    build(g, reg, "s", ["a", "b"], "sum")
    build(g, reg, "t", ["a", "b"], "scan")
    g.subscribe("ks", "s")
    g.subscribe("kt", "t")
    g.set("a", 3)
    return g


def round_trip(cp: Checkpoint) -> Checkpoint:
    loaded: Checkpoint = json.loads(json.dumps(cp))
    return loaded


def traced(cp: Checkpoint, reg: dict[str, Factory]) -> Graph:
    g = restore(cp, reg)
    g.trace = []
    return g


def entry(cp: Checkpoint, node_id: str) -> dict[str, Any]:
    found: dict[str, Any] = next(n for n in cp["nodes"] if n["id"] == node_id)
    return found


# ---- C-38: restore keeps the saved state ---------------------------------------


def test_c38_restore_runs_no_function_sends_nothing_and_keeps_the_topology() -> None:
    g = original()
    assert (g.get("s").value, g.get("t").value) == (5, 6)
    reg = Registry()
    r = traced(round_trip(capture(g)), reg)
    assert r.trace == []
    assert reg.calls == {"sum": 0, "scan": 0}
    assert r.describe() == g.describe()
    assert r.get("s").active is False


def test_c38_the_first_subscription_gets_the_saved_value_and_state_without_a_run() -> None:
    reg = Registry()
    r = traced(round_trip(capture(original())), reg)

    def subscribe() -> None:
        r.subscribe("ks", "s")
        r.subscribe("kt", "t")

    assert during(r, subscribe) == ["ks <- s: [START, DATA(5)]", "kt <- t: [START, DATA(6)]"]
    assert reg.calls == {"sum": 0, "scan": 0}
    assert during(r, lambda: r.set("b", 10)) == [
        "ks <- s: [PENDING, DATA(13)]",
        "kt <- t: [PENDING, DATA(16)]",  # t's private state came back: 6 + 10
    ]
    assert reg.calls == {"sum": 1, "scan": 1}
    assert entry(capture(r), "t")["saved"]["values"] == [16]


def test_c38_an_upstream_change_runs_the_function_once_with_only_that_input() -> None:
    reg = Registry()
    r = traced(round_trip(capture(original())), reg)
    r.set("a", 5)  # nobody is subscribed: no function runs
    assert reg.calls == {"sum": 0, "scan": 0}

    def subscribe() -> None:
        r.subscribe("ks", "s")
        r.subscribe("kt", "t")

    assert during(r, subscribe) == ["ks <- s: [START, DATA(7)]", "kt <- t: [START, DATA(11)]"]
    assert reg.calls == {"sum": 1, "scan": 1}  # t: 6 + 5; b did not change, so it brought nothing


def test_c38_a_run_that_sends_nothing_keeps_the_saved_value() -> None:
    reg: dict[str, Factory] = {
        "even": lambda c, at: (
            lambda ctx: (
                ctx.down([(DATA, ctx.deps[0].value)]) if ctx.deps[0].value % 2 == 0 else None  # type: ignore[operator]
            )
        )
    }
    g = Graph()
    g.state("a", 2)
    build(g, reg, "v", ["a"], "even")
    g.subscribe("k", "v")
    r = traced(round_trip(capture(g)), reg)
    r.set("a", 3)
    assert during(r, lambda: r.subscribe("k", "v")) == ["k <- v: [START, DATA(2)]"]


def _vals() -> dict[str, Factory]:
    return {
        "vals": lambda c, at: (
            lambda ctx: ctx.down([(DATA, [v for w in ctx.deps[0].waves for v in w])])
        )
    }


def test_c38_an_invalidate_then_a_set_before_the_first_subscription_is_seen_as_the_set() -> None:
    reg = _vals()
    g = Graph()
    g.state("a", 1)
    build(g, reg, "p", ["a"], "vals")
    g.subscribe("k", "p")
    cp = round_trip(capture(g))
    early = traced(cp, reg)
    early.invalidate("a")
    early.set("a", 9)
    assert during(early, lambda: early.subscribe("k", "p")) == ["k <- p: [START, DATA([9])]"]
    first = traced(cp, reg)
    first.subscribe("k", "p")
    assert during(first, lambda: first.invalidate("a")) == ["k <- p: [PENDING, INVALIDATE]"]


def test_c38_an_input_that_only_completed_brings_its_end_not_its_values_again() -> None:
    reg: dict[str, Factory] = {
        "seen": lambda c, at: (
            lambda ctx: ctx.down([(DATA, [ctx.deps[0].waves, ctx.deps[0].terminal is not None])])
        )
    }
    g = Graph()
    g.state("a", 1)
    build(g, reg, "p", ["a"], "seen", complete="manual")
    g.subscribe("k", "p")
    r = traced(round_trip(capture(g)), reg)
    r.complete("a")
    assert during(r, lambda: r.subscribe("k", "p")) == ["k <- p: [START, DATA([[],true])]"]


def test_c38_an_ended_node_comes_back_ended_with_its_dependencies_and_no_state() -> None:
    reg = Registry()
    g = Graph()
    g.state("a", 1)
    g.state("b", 2)
    build(g, reg, "t", ["a", "b"], "scan")
    g.subscribe("k", "t")
    g.set("a", 3)
    assert entry(round_trip(capture(g)), "t")["saved"]["state"] == 6
    g.complete("a")
    g.complete("b")
    assert g.get("t").ended == (COMPLETE,)
    cp = round_trip(capture(g))
    saved = entry(cp, "t")["saved"]
    assert (saved["values"], saved["ended"], "state" in saved) == ([6], ["COMPLETE"], False)
    r = traced(cp, reg)
    assert r.private_state("t") is None
    assert r.definition("t").deps == ("a", "b")
    assert during(r, lambda: r.subscribe("k", "t")) == ["k <- t: [START, DATA(6), COMPLETE]"]


def test_c38_an_ended_error_round_trips_with_its_json_payload() -> None:
    reg: dict[str, Factory] = {
        "last": lambda c, at: (
            lambda ctx: ctx.down([(DATA, ctx.deps[0].value)]) if ctx.deps[0].waves else None
        )
    }
    g = Graph()
    g.state("a", 1)
    build(g, reg, "e", ["a"], "last")
    g.subscribe("ke", "e")
    g.set("a", 2)
    g.error("a", "boom")
    r = traced(round_trip(capture(g)), reg)
    for node_id in ("a", "e"):
        assert r.get(node_id).ended == (ERROR, "boom")
        assert during(r, lambda n=node_id: r.subscribe("k", n)) == [  # type: ignore[misc]
            f'k <- {node_id}: [START, DATA(2), ERROR("boom")]'
        ]
    assert round_trip(capture(traced(round_trip(capture(g)), reg))) == round_trip(capture(g))


def test_c38_capture_is_refused_inside_a_wave_and_a_batch_body() -> None:
    g = original()
    seen: list[str] = []

    def watch(array: tuple[Any, ...]) -> None:
        if g.get("a").value == 4:
            try:
                capture(g)
            except CheckpointError as error:
                seen.append(str(error))

    g.subscribe("watch", "a", watch)
    g.set("a", 4)
    assert seen and "wave boundary" in seen[0]
    with pytest.raises(CheckpointError, match="wave boundary"):
        g.batch(lambda: capture(g))
    assert entry(capture(g), "a")["saved"]["values"] == [4]


def test_c38_values_state_configs_and_error_payloads_that_are_not_json_are_refused() -> None:
    def keep(c: object, at: RecipeContext) -> Callable[[Ctx], None]:
        def fn(ctx: Ctx) -> None:
            ctx.state = {"f": print}
            ctx.down([(DATA, 1)])

        return fn

    reg: dict[str, Factory] = {**Registry(), "keep": keep}
    g = Graph()
    g.state("a", 1)
    build(g, reg, "k", ["a"], "keep")
    g.subscribe("s", "k")
    with pytest.raises(CheckpointError, match="node k is not JSON"):
        capture(g)
    h = Graph()
    h.state("a", 1)
    build(h, reg, "x", ["a"], "plus", {"k": (1, 2)})
    with pytest.raises(CheckpointError, match="node x is not JSON"):
        capture(h)
    e = Graph()
    e.state("a", 1)
    e.error("a", ValueError("boom"))
    with pytest.raises(CheckpointError, match="node a is not JSON"):
        capture(e)
    v = Graph()
    v.state("a", (1, 2))  # a tuple does not come back from JSON as itself
    with pytest.raises(CheckpointError, match="node a is not JSON"):
        capture(v)


def test_c38_a_missing_recipe_a_local_only_node_and_an_earlier_format_fail() -> None:
    cp = round_trip(capture(original()))
    without_scan = {k: v for k, v in Registry().items() if k != "scan"}
    with pytest.raises(CheckpointError, match="no recipe scan"):
        restore(cp, without_scan)

    reg = Registry()
    g = Graph()
    g.state("a", 1)
    build(g, reg, "s", ["a"], "sum")
    g.node("raw", ["a"], lambda ctx: ctx.down([(DATA, 0)]))
    local = capture(g)
    assert [n["id"] for n in local["nodes"] if n.get("local")] == ["raw"]
    with pytest.raises(CheckpointError, match="raw is local-only"):
        restore(local, reg)

    older = {
        **cp,
        "nodes": [
            n
            if n["kind"] == "state" or "saved" not in n
            else {**n, "saved": {k: v for k, v in n["saved"].items() if k != "inputs"}}
            for n in cp["nodes"]
        ],
    }
    with pytest.raises(CheckpointError, match="s has no record of what it received"):
        restore(older, Registry())
    with pytest.raises(CheckpointError, match="not a graphrefly-py/checkpoint@1"):
        restore({"nodes": cp["nodes"]}, Registry())
    restore(cp, Registry())  # the same checkpoint with the record restores


def _times() -> dict[str, Factory]:
    return {
        "times": lambda c, at: (
            lambda ctx: (
                ctx.down([(DATA, ctx.deps[0].value * c["k"])]) if ctx.deps[0].has else None
            )
        )
    }


def test_c38_a_replace_without_a_recipe_makes_the_node_local_only() -> None:
    reg = _times()
    g = Graph()
    g.state("a", 1)
    build(g, reg, "p", ["a"], "times", {"k": 2})
    build(g, reg, "q", ["a"], "times", {"k": 2})
    g.subscribe("s", "p")
    g.replace("p", ["a"], lambda ctx: ctx.down([(DATA, ctx.deps[0].value * 3)]))  # type: ignore[operator]
    assert g.definition("p").recipe is None
    cp = round_trip(capture(g))
    assert [n["id"] for n in cp["nodes"] if n.get("local")] == ["p"]
    with pytest.raises(CheckpointError, match="p is local-only"):
        restore(cp, reg)


def test_c38_a_replace_with_a_recipe_restores_as_that_recipe() -> None:
    reg = _times()
    g = Graph()
    g.state("a", 1)
    build(g, reg, "p", ["a"], "times", {"k": 2})
    g.subscribe("s", "p")
    new_fn = reg["times"]({"k": 3}, RecipeContext(g, "p", lambda *a: Built(lambda ctx: None)))
    assert not isinstance(new_fn, Built)
    g.replace("p", ["a"], new_fn, recipe=recipe("times", {"k": 3}))
    cp = round_trip(capture(g))
    assert entry(cp, "p")["recipe"] == {"ref": "times", "config": {"k": 3}}
    r = traced(cp, reg)
    r.subscribe("s", "p")
    for x in (g, r):
        x.set("a", 5)
    assert (r.get("p").value, g.get("p").value) == (15, 15)


def test_c38_a_replace_with_another_setup_restores_that_instances_setup() -> None:
    log: list[str] = []

    def named(name: str) -> Factory:
        def factory(c: object, at: RecipeContext) -> Built:
            return Built(lambda ctx: ctx.down([(DATA, name)]), lambda ctx: log.append(name))

        return factory

    reg: dict[str, Factory] = {"a": named("A"), "b": named("B")}
    g = Graph()
    build(g, reg, "n", [], "a")
    g.subscribe("k", "n")
    built = reg["b"](None, RecipeContext(g, "n", lambda *a: Built(lambda ctx: None)))
    assert isinstance(built, Built)
    g.replace("n", [], built.fn, setup=built.setup, recipe=recipe("b"))
    g.unsubscribe("k", "n")
    g.subscribe("k", "n")
    assert g.get("n").value == "B"
    assert log == ["A", "B", "B"]
    log.clear()
    r = traced(round_trip(capture(g)), reg)
    r.subscribe("k", "n")
    assert log == ["B"] and r.get("n").value == "B"


def test_c38_a_node_replaced_onto_a_later_dependency_is_restored_after_it() -> None:
    reg = Registry()
    g = Graph()
    build(g, reg, "n", [], "sum")
    g.state("later", 4)
    plus = reg["plus"]({"k": 1}, RecipeContext(g, "n", lambda *a: Built(lambda ctx: None)))
    assert not isinstance(plus, Built)
    g.replace("n", ["later"], plus, recipe=recipe("plus", {"k": 1}))
    g.subscribe("k", "n")
    assert g.get("n").value == 5
    cp = round_trip(capture(g))
    assert [n["id"] for n in cp["nodes"]] == ["n", "later"]
    r = traced(cp, reg)
    assert r.definition("n").deps == ("later",)
    assert during(r, lambda: r.subscribe("k", "n")) == ["k <- n: [START, DATA(5)]"]
    r.set("later", 9)
    assert r.get("n").value == 10


def test_c38_a_rewired_node_is_restored_onto_its_lifetimes_dependencies() -> None:
    reg = Registry()
    kept: list[Ctx] = []
    inner = reg["sum"]

    def grab(c: object, at: RecipeContext) -> Callable[[Ctx], None]:
        fn = inner(c, at)
        assert not isinstance(fn, Built)

        def run(ctx: Ctx) -> None:
            kept.append(ctx)
            fn(ctx)

        return run

    reg["grab"] = grab
    g = Graph()
    g.state("a", 1)
    g.state("b", 2)
    build(g, reg, "s", ["a"], "grab")
    g.subscribe("k", "s")
    kept[-1].rewire(["a", "b"])
    assert g.get("s").value == 3
    cp = round_trip(capture(g))
    saved = entry(cp, "s")
    assert (saved.get("local"), saved["deps"], saved["saved"]["deps"]) == (None, ["a"], ["a", "b"])
    r = traced(cp, reg)
    assert r.definition("s").deps == ("a",)
    assert during(r, lambda: r.subscribe("k", "s")) == ["k <- s: [START, DATA(3)]"]
    assert next(d for d in r.describe() if d.id == "s").deps == ("a", "b")
    r.set("b", 5)
    assert r.get("s").value == 6
    r.unsubscribe("k", "s")
    assert next(d for d in r.describe() if d.id == "s").deps == ("a",)


def test_c38_a_timer_source_reopens_its_timer_in_setup_and_counts_on() -> None:
    timers: list[Callable[[], None]] = []
    counts = {"setups": 0, "runs": 0}

    def interval(c: object, at: RecipeContext) -> Built:
        def fn(ctx: Ctx) -> None:
            counts["runs"] += 1

        def setup(ctx: Ctx) -> Callable[[], None]:
            counts["setups"] += 1

            def tick() -> None:
                n = (ctx.state if isinstance(ctx.state, int) else 0) + 1
                ctx.state = n
                ctx.down([(DATA, n)])

            timers.append(tick)
            return lambda: timers.remove(tick)

        return Built(fn, setup)

    reg: dict[str, Factory] = {"interval": interval}
    g = Graph()
    build(g, reg, "i", [], "interval")
    g.subscribe("k", "i")
    for tick in list(timers):
        tick()
    for tick in list(timers):
        tick()
    cp = round_trip(capture(g))
    g.unsubscribe("k", "i")
    assert timers == []  # the cleanup stopped the original's timer
    counts.update(setups=0, runs=0)
    r = traced(cp, reg)
    r.subscribe("k", "i")
    assert (counts["setups"], counts["runs"], len(timers), r.get("i").value) == (1, 0, 1, 2)
    timers[0]()
    assert r.get("i").value == 3


def test_c38_a_restored_graph_captured_again_before_a_subscription_keeps_its_state() -> None:
    reg = Registry()
    once = traced(round_trip(capture(original())), reg)
    once.set("a", 5)  # received by nobody yet: the saved inputs still say a=3
    twice = traced(round_trip(capture(once)), reg)
    assert during(twice, lambda: twice.subscribe("kt", "t")) == ["kt <- t: [START, DATA(11)]"]
    assert reg.calls["scan"] == 1


def test_c38_no_checkpoint_carries_revision_or_version_evidence() -> None:
    text = json.dumps(capture(original()))
    for word in ("revision", "version", "valueRevision"):
        assert word not in text


# ---- reference behavior ----------------------------------------------------------


def test_a_chain_of_restored_nodes_needs_no_run() -> None:
    reg = Registry()
    g = original(reg)
    build(g, reg, "q", ["s"], "plus", {"k": 100})
    build(g, reg, "u", ["q", "t"], "sum")
    g.subscribe("ku", "u")
    reg2 = Registry()
    r = traced(round_trip(capture(g)), reg2)
    assert during(r, lambda: r.subscribe("ku", "u")) == ["ku <- u: [START, DATA(111)]"]
    assert reg2.calls == {"sum": 0, "scan": 0}
    assert during(r, lambda: r.set("a", 4)) == ["ku <- u: [PENDING, DATA(116)]"]


def test_a_computed_node_without_a_lifetime_saves_nothing_and_its_policies_come_back() -> None:
    reg = Registry()
    g = Graph()
    g.state("a", 1)
    build(g, reg, "p", ["a"], "plus", {"k": 10}, complete="manual", invalidate="manual", quiet=True)
    cp = round_trip(capture(g))
    assert cp["nodes"][1] == {
        "id": "p",
        "kind": "node",
        "deps": ["a"],
        "recipe": {"ref": "plus", "config": {"k": 10}},
        "policies": {"complete": "manual", "error": "auto", "invalidate": "manual", "quiet": True},
    }
    r = traced(cp, reg)
    assert r.definition("p") == r.definition("p")
    d = r.definition("p")
    assert (d.complete, d.error, d.invalidate, d.quiet) == ("manual", "auto", "manual", True)
    r2 = traced(round_trip(capture(Graph())), reg)
    assert r2.describe() == []


def test_a_node_holding_an_ended_dependency_comes_back_and_works() -> None:
    reg = Registry()
    last_calls = [0]

    def last(c: object, at: RecipeContext) -> Callable[[Ctx], None]:
        def fn(ctx: Ctx) -> None:
            last_calls[0] += 1
            if ctx.deps[0].waves:
                ctx.down([(DATA, ctx.deps[0].value)])

        return fn

    reg["last"] = last
    g = Graph()
    g.state("config", 1)
    g.state("input", 0)
    build(g, reg, "e", ["config"], "last")
    build(g, reg, "s", ["e", "input"], "sum")
    g.subscribe("k", "s")
    g.complete("config")
    assert (g.get("e").ended, g.get("s").ended) == ((COMPLETE,), None)
    r = traced(round_trip(capture(g)), reg)
    before = reg.calls["sum"]
    last_calls[0] = 0
    assert during(r, lambda: r.subscribe("k", "s")) == ["k <- s: [START, DATA(1)]"]
    assert during(r, lambda: r.set("input", 5)) == ["k <- s: [PENDING, DATA(6)]"]
    assert (reg.calls["sum"] - before, last_calls[0]) == (1, 0)
    assert during(r, lambda: r.subscribe("k2", "e")) == ["k2 <- e: [START, DATA(1), COMPLETE]"]


def test_a_snapshot_is_compared_as_a_whole_array() -> None:
    reg = _vals()
    g = Graph()
    g.state("s")
    build(g, reg, "d", ["s"], "vals")
    g.subscribe("k", "d")

    def one_two() -> None:
        g.set("s", 1)
        g.set("s", 2)

    g.batch(one_two)
    cp = round_trip(capture(g))
    assert entry(cp, "d")["saved"]["inputs"][0]["values"] == [1, 2]
    r = traced(cp, reg)

    def three_two() -> None:
        r.set("s", 3)
        r.set("s", 2)

    r.batch(three_two)
    r.subscribe("k", "d")
    assert r.get("d").value == [3, 2]
    same = traced(cp, reg)
    same.subscribe("k", "d")
    assert same.get("d").value == [1, 2]


def test_every_value_of_the_last_array_is_saved_and_comes_back() -> None:
    reg: dict[str, Factory] = {
        "pair": lambda c, at: (
            lambda ctx: ctx.down(
                [(DATA, ctx.deps[0].value), (DATA, -ctx.deps[0].value)]  # type: ignore[operator]
            )
        )
    }
    g = Graph()
    g.state("a", 0)
    build(g, reg, "p", ["a"], "pair")
    g.subscribe("k", "p")

    def two() -> None:
        g.set("a", 1)
        g.set("a", 2)

    g.batch(two)
    r = traced(round_trip(capture(g)), reg)
    assert r.get("a").values == (1, 2)
    assert during(r, lambda: r.subscribe("k", "p")) == ["k <- p: [START, DATA(2), DATA(-2)]"]
