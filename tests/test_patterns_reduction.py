"""Patterns reduction tests (roadmap 8.1)."""

from __future__ import annotations

from graphrefly import Graph, state
from graphrefly.core.protocol import MessageType
from graphrefly.patterns.reduction import (
    BudgetConstraint,
    FunnelStage,
    ScoredItem,
    StratifyRule,
    budget_gate,
    feedback,
    funnel,
    scorer,
    stratify,
)

# ---------------------------------------------------------------------------
# stratify
# ---------------------------------------------------------------------------


def test_stratify_routes_values_to_matching_branches() -> None:
    source = state(0)
    rules = [
        StratifyRule("even", lambda v: v % 2 == 0),
        StratifyRule("odd", lambda v: v % 2 != 0),
    ]
    g = stratify("classify", source, rules)
    assert isinstance(g, Graph)

    even_seen: list[int] = []
    odd_seen: list[int] = []

    def even_sink(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                even_seen.append(msg[1])

    def odd_sink(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                odd_seen.append(msg[1])

    g.resolve("branch/even").subscribe(even_sink)
    g.resolve("branch/odd").subscribe(odd_sink)

    source.down([(MessageType.DATA, 2)])
    source.down([(MessageType.DATA, 3)])
    source.down([(MessageType.DATA, 4)])
    source.down([(MessageType.DATA, 7)])

    assert even_seen == [2, 4]
    assert odd_seen == [3, 7]


def test_stratify_reactive_rules() -> None:
    source = state("a")
    rules = [StratifyRule("match", lambda v: v == "a")]
    g = stratify("dynamic", source, rules)

    seen: list[str] = []

    def sink(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                seen.append(msg[1])

    g.resolve("branch/match").subscribe(sink)

    source.down([(MessageType.DATA, "a")])
    assert seen == ["a"]

    # Rewrite rules: now match "b"
    g.set("rules", [StratifyRule("match", lambda v: v == "b")])
    source.down([(MessageType.DATA, "a")])
    source.down([(MessageType.DATA, "b")])
    assert seen == ["a", "b"]


def test_stratify_graph_edges() -> None:
    source = state(0)
    rules = [StratifyRule("pos", lambda v: v > 0)]
    g = stratify("edges", source, rules)
    assert ("source", "branch/pos") in g.edges()


def test_stratify_propagates_complete() -> None:
    source = state(0)
    rules = [StratifyRule("all", lambda _v: True)]
    g = stratify("term", source, rules)
    completed = [False]

    def sink(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.COMPLETE:
                completed[0] = True

    g.resolve("branch/all").subscribe(sink)
    source.down([(MessageType.COMPLETE,)])
    assert completed[0] is True


# ---------------------------------------------------------------------------
# funnel
# ---------------------------------------------------------------------------


def test_funnel_merges_and_pipes_through_stages() -> None:
    s1 = state(0)
    s2 = state(0)

    def build_double(sub: Graph) -> None:
        inp = state(0)
        sub.add("input", inp)
        out = state(0)
        sub.add("output", out)

        def bridge(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    out.down([(MessageType.DATA, msg[1] * 2)])

        inp.subscribe(bridge)

    g = funnel("pipe", [s1, s2], [FunnelStage("double", build_double)])
    assert isinstance(g, Graph)

    results: list[int] = []

    def sink(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                results.append(msg[1])

    g.resolve("double::output").subscribe(sink)

    s1.down([(MessageType.DATA, 5)])
    s2.down([(MessageType.DATA, 3)])
    assert results == [10, 6]


def test_funnel_rejects_empty_sources() -> None:
    try:
        funnel("bad", [], [FunnelStage("s", lambda _sub: None)])
        assert False, "should raise"  # noqa: B011
    except ValueError as e:
        assert "at least one source" in str(e)


def test_funnel_rejects_empty_stages() -> None:
    try:
        funnel("bad", [state(0)], [])
        assert False, "should raise"  # noqa: B011
    except ValueError as e:
        assert "at least one stage" in str(e)


def test_funnel_rejects_stage_without_input() -> None:
    def build_no_input(sub: Graph) -> None:
        sub.add("output", state(0))

    try:
        funnel("bad", [state(0)], [FunnelStage("noInput", build_no_input)])
        assert False, "should raise"  # noqa: B011
    except ValueError as e:
        assert '"input" node' in str(e)


def test_funnel_rejects_stage_without_output() -> None:
    def build_no_output(sub: Graph) -> None:
        sub.add("input", state(0))

    try:
        funnel("bad", [state(0)], [FunnelStage("noOutput", build_no_output)])
        assert False, "should raise"  # noqa: B011
    except ValueError as e:
        assert '"output" node' in str(e)


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------


def test_feedback_routes_condition_back_to_reentry() -> None:
    g = Graph("fb")
    inp = state(0)
    g.add("input", inp)

    cond = state(None)
    g.add("condition", cond)

    input_node = g.resolve("input")

    def input_bridge(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                v = msg[1]
                cond.down([(MessageType.DATA, v + 1 if v < 5 else None)])

    input_node.subscribe(input_bridge)

    feedback(g, "condition", "input", max_iterations=10)

    seen: list[int] = []

    def track(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                seen.append(msg[1])

    input_node.subscribe(track)

    inp.down([(MessageType.DATA, 1)])
    assert 1 in seen
    counter = g.get("__feedback_condition")
    assert counter > 0


def test_feedback_respects_max_iterations() -> None:
    g = Graph("bounded")
    inp = state(0)
    g.add("input", inp)
    cond = state(0)
    g.add("condition", cond)

    input_node = g.resolve("input")

    def always_feedback(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                cond.down([(MessageType.DATA, msg[1] + 1)])

    input_node.subscribe(always_feedback)

    feedback(g, "condition", "input", max_iterations=3)
    cond.subscribe(lambda _: None)

    inp.down([(MessageType.DATA, 0)])
    counter = g.get("__feedback_condition")
    assert counter <= 3


# ---------------------------------------------------------------------------
# budget_gate
# ---------------------------------------------------------------------------


def test_budget_gate_passes_when_available() -> None:
    source = state(0)
    budget = state(100)
    gated = budget_gate(source, [BudgetConstraint(budget, lambda v: v > 0)])

    seen: list[int] = []

    def sink(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                seen.append(msg[1])

    gated.subscribe(sink)
    source.down([(MessageType.DATA, 42)])
    assert seen == [42]


def test_budget_gate_buffers_then_flushes() -> None:
    source = state(0)
    budget = state(0)  # exhausted
    gated = budget_gate(source, [BudgetConstraint(budget, lambda v: v > 0)])

    seen: list[int] = []

    def sink(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                seen.append(msg[1])

    gated.subscribe(sink)
    source.down([(MessageType.DATA, 1)])
    source.down([(MessageType.DATA, 2)])
    assert seen == []

    budget.down([(MessageType.DATA, 50)])
    assert seen == [1, 2]


def test_budget_gate_rejects_empty_constraints() -> None:
    try:
        budget_gate(state(0), [])
        assert False, "should raise"  # noqa: B011
    except ValueError as e:
        assert "at least one constraint" in str(e)


def test_budget_gate_propagates_complete() -> None:
    source = state(0)
    budget = state(0)
    gated = budget_gate(source, [BudgetConstraint(budget, lambda v: v > 0)])

    completed = [False]

    def sink(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.COMPLETE:
                completed[0] = True

    gated.subscribe(sink)
    source.down([(MessageType.DATA, 10)])
    source.down([(MessageType.COMPLETE,)])
    assert completed[0] is True


def test_budget_gate_propagates_error() -> None:
    source = state(0)
    budget = state(100)
    gated = budget_gate(source, [BudgetConstraint(budget, lambda v: v > 0)])

    errored = [False]

    def sink(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.ERROR:
                errored[0] = True

    gated.subscribe(sink)
    source.down([(MessageType.ERROR, RuntimeError("boom"))])
    assert errored[0] is True


# ---------------------------------------------------------------------------
# scorer
# ---------------------------------------------------------------------------


def test_scorer_computes_weighted_scores() -> None:
    sig1 = state(0)
    sig2 = state(0)
    w1 = state(1)
    w2 = state(1)

    s = scorer([sig1, sig2], [w1, w2])
    result: list[ScoredItem] = []

    def sink(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                result.append(msg[1])

    s.subscribe(sink)

    sig1.down([(MessageType.DATA, 3)])
    sig2.down([(MessageType.DATA, 7)])

    assert len(result) > 0
    last = result[-1]
    assert last.value == [3.0, 7.0]
    assert last.score == 10.0
    assert last.breakdown == [3.0, 7.0]


def test_scorer_reacts_to_weight_changes() -> None:
    sig1 = state(5)
    w1 = state(1)
    s = scorer([sig1], [w1])

    result: list[ScoredItem] = []

    def sink(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                result.append(msg[1])

    s.subscribe(sink)
    sig1.down([(MessageType.DATA, 5)])
    assert result[-1].score == 5.0

    w1.down([(MessageType.DATA, 3)])
    assert result[-1].score == 15.0


def test_scorer_custom_score_fns() -> None:
    sig1 = state(0)
    w1 = state(1)
    s = scorer([sig1], [w1], score_fns=[lambda v: v**2])

    result: list[ScoredItem] = []

    def sink(msgs: list) -> None:
        for msg in msgs:
            if msg[0] is MessageType.DATA:
                result.append(msg[1])

    s.subscribe(sink)
    sig1.down([(MessageType.DATA, 4)])
    assert result[-1].score == 16.0


def test_scorer_rejects_mismatched_sources_weights() -> None:
    try:
        scorer([state(0), state(0)], [state(1)])
        assert False, "should raise"  # noqa: B011
    except ValueError as e:
        assert "same number" in str(e)


def test_scorer_rejects_empty_sources() -> None:
    try:
        scorer([], [])
        assert False, "should raise"  # noqa: B011
    except ValueError as e:
        assert "at least one source" in str(e)


def test_scorer_has_reduction_meta() -> None:
    s = scorer([state(0)], [state(1)])
    assert s.meta["reduction"].get() is True
    assert s.meta["reduction_type"].get() == "scorer"
