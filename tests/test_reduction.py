"""Tests for reduction primitives (Phase 8.1).

Mirrors graphrefly-ts/src/__tests__/patterns/reduction.test.ts.
"""

from __future__ import annotations

import pytest

from graphrefly.core.node import NodeImpl
from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import state
from graphrefly.graph.graph import Graph
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


class TestStratify:
    def test_routes_values_to_matching_branches(self) -> None:
        source = state(0)
        rules = [
            StratifyRule("even", classify=lambda v: v % 2 == 0),
            StratifyRule("odd", classify=lambda v: v % 2 != 0),
        ]

        g = stratify("classify", source, rules)
        assert isinstance(g, Graph)

        even_seen: list[int] = []
        odd_seen: list[int] = []

        def _even_sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    even_seen.append(msg[1])

        def _odd_sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    odd_seen.append(msg[1])

        g.resolve("branch/even").subscribe(_even_sink)
        g.resolve("branch/odd").subscribe(_odd_sink)

        source.down([(MessageType.DATA, 2)])
        source.down([(MessageType.DATA, 3)])
        source.down([(MessageType.DATA, 4)])
        source.down([(MessageType.DATA, 7)])

        assert even_seen == [2, 4]
        assert odd_seen == [3, 7]

    def test_reactive_rules_rewriting(self) -> None:
        source = state("a")
        rules = [StratifyRule("match", classify=lambda v: v == "a")]

        g = stratify("dynamic", source, rules)

        seen: list[str] = []

        def _sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    seen.append(msg[1])

        g.resolve("branch/match").subscribe(_sink)

        source.down([(MessageType.DATA, "a")])
        assert seen == ["a"]

        # Rewrite rules: now match "b" instead
        g.set("rules", [StratifyRule("match", classify=lambda v: v == "b")])
        source.down([(MessageType.DATA, "a")])
        source.down([(MessageType.DATA, "b")])
        assert seen == ["a", "b"]

    def test_graph_has_correct_edges(self) -> None:
        source = state(0)
        rules = [StratifyRule("pos", classify=lambda v: v > 0)]

        g = stratify("edges", source, rules)
        edges = g.edges()
        assert ("source", "branch/pos") in edges

    def test_propagates_complete_through_branches(self) -> None:
        source = state(0)
        rules = [StratifyRule("all", classify=lambda _v: True)]

        g = stratify("term", source, rules)
        completed = [False]

        def _sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.COMPLETE:
                    completed[0] = True

        g.resolve("branch/all").subscribe(_sink)

        source.down([(MessageType.COMPLETE,)])
        assert completed[0] is True


# ---------------------------------------------------------------------------
# funnel
# ---------------------------------------------------------------------------


class TestFunnel:
    def test_merges_sources_and_pipes_through_stages(self) -> None:
        s1 = state(0)
        s2 = state(0)

        def _build_double(sub: Graph) -> None:
            inp = state(0)
            sub.add("input", inp)
            out = state(0)
            sub.add("output", out)

            def _wire(msgs: list) -> None:
                for msg in msgs:
                    if msg[0] is MessageType.DATA:
                        out.down([(MessageType.DATA, msg[1] * 2)])

            inp.subscribe(_wire)

        g = funnel("pipe", [s1, s2], [FunnelStage("double", _build_double)])

        assert isinstance(g, Graph)

        results: list[int] = []

        def _sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    results.append(msg[1])

        g.resolve("double::output").subscribe(_sink)

        s1.down([(MessageType.DATA, 5)])
        s2.down([(MessageType.DATA, 3)])

        assert results == [10, 6]

    def test_rejects_empty_sources(self) -> None:
        with pytest.raises(ValueError, match="at least one source"):
            funnel("bad", [], [FunnelStage("s", build=lambda _sub: None)])

    def test_rejects_empty_stages(self) -> None:
        with pytest.raises(ValueError, match="at least one stage"):
            funnel("bad", [state(0)], [])

    def test_rejects_stage_without_input_node(self) -> None:
        def _build(sub: Graph) -> None:
            sub.add("output", state(0))

        with pytest.raises(ValueError, match='must define an "input" node'):
            funnel("bad", [state(0)], [FunnelStage("noInput", _build)])

    def test_rejects_stage_without_output_node(self) -> None:
        def _build(sub: Graph) -> None:
            sub.add("input", state(0))

        with pytest.raises(ValueError, match='must define an "output" node'):
            funnel("bad", [state(0)], [FunnelStage("noOutput", _build)])


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------


class TestFeedback:
    def test_routes_condition_output_back_to_reentry(self) -> None:
        g = Graph("fb")
        inp = state(0)
        g.add("input", inp)

        cond = state(None)
        g.add("condition", cond)

        input_node = g.resolve("input")

        def _wire(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    v = msg[1]
                    cond.down([(MessageType.DATA, v + 1 if v < 5 else None)])

        input_node.subscribe(_wire)

        feedback(g, "condition", "input", max_iterations=10)

        seen: list[int] = []

        def _sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    seen.append(msg[1])

        input_node.subscribe(_sink)

        # Kick off
        inp.down([(MessageType.DATA, 1)])

        assert 1 in seen
        assert len(seen) >= 1
        counter = g.get("__feedback_condition")
        assert counter > 0

    def test_respects_max_iterations_bound(self) -> None:
        g = Graph("bounded")
        inp = state(0)
        g.add("input", inp)

        cond = state(0)
        g.add("condition", cond)

        def _wire(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    cond.down([(MessageType.DATA, msg[1] + 1)])

        inp.subscribe(_wire)

        feedback(g, "condition", "input", max_iterations=3)

        # Subscribe to activate
        cond.subscribe(lambda _msgs: None)

        inp.down([(MessageType.DATA, 0)])

        counter = g.get("__feedback_condition")
        assert counter <= 3


# ---------------------------------------------------------------------------
# budget_gate
# ---------------------------------------------------------------------------


class TestBudgetGate:
    def test_passes_data_when_budget_available(self) -> None:
        source = state(0)
        budget = state(100)
        gated = budget_gate(source, [BudgetConstraint(budget, check=lambda v: v > 0)])

        seen: list[int] = []

        def _sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    seen.append(msg[1])

        gated.subscribe(_sink)

        source.down([(MessageType.DATA, 42)])
        assert seen == [42]

    def test_buffers_data_when_budget_exhausted_flushes_on_replenish(self) -> None:
        source = state(0)
        budget = state(0)  # exhausted
        gated = budget_gate(source, [BudgetConstraint(budget, check=lambda v: v > 0)])

        seen: list[int] = []

        def _sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    seen.append(msg[1])

        gated.subscribe(_sink)

        source.down([(MessageType.DATA, 1)])
        source.down([(MessageType.DATA, 2)])
        assert seen == []  # buffered

        # Replenish budget
        budget.down([(MessageType.DATA, 50)])
        assert seen == [1, 2]  # flushed

    def test_rejects_zero_constraints(self) -> None:
        with pytest.raises(ValueError, match="at least one constraint"):
            budget_gate(state(0), [])

    def test_propagates_complete_and_flushes_buffer(self) -> None:
        source = state(0)
        budget = state(0)
        gated = budget_gate(source, [BudgetConstraint(budget, check=lambda v: v > 0)])

        seen: list[int] = []
        completed = [False]

        def _sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    seen.append(msg[1])
                if msg[0] is MessageType.COMPLETE:
                    completed[0] = True

        gated.subscribe(_sink)

        source.down([(MessageType.DATA, 10)])
        source.down([(MessageType.COMPLETE,)])
        assert completed[0] is True

    def test_propagates_error(self) -> None:
        source = state(0)
        budget = state(100)
        gated = budget_gate(source, [BudgetConstraint(budget, check=lambda v: v > 0)])

        errored = [False]

        def _sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.ERROR:
                    errored[0] = True

        gated.subscribe(_sink)

        source.down([(MessageType.ERROR, RuntimeError("boom"))])
        assert errored[0] is True


# ---------------------------------------------------------------------------
# scorer
# ---------------------------------------------------------------------------


class TestScorer:
    def test_computes_weighted_scores(self) -> None:
        sig1 = state(0)
        sig2 = state(0)
        w1 = state(1)
        w2 = state(1)

        s = scorer([sig1, sig2], [w1, w2])

        result: list[ScoredItem] = []

        def _sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    result.append(msg[1])

        s.subscribe(_sink)

        sig1.down([(MessageType.DATA, 3)])
        sig2.down([(MessageType.DATA, 7)])

        assert len(result) > 0
        last = result[-1]
        assert last.value == [3, 7]
        assert last.score == 10  # 3*1 + 7*1
        assert last.breakdown == [3, 7]

    def test_reacts_to_weight_changes(self) -> None:
        sig1 = state(5)
        w1 = state(1)

        s = scorer([sig1], [w1])

        result: list[ScoredItem] = []

        def _sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    result.append(msg[1])

        s.subscribe(_sink)

        sig1.down([(MessageType.DATA, 5)])
        assert result[-1].score == 5

        w1.down([(MessageType.DATA, 3)])
        assert result[-1].score == 15  # 5 * 3

    def test_supports_custom_score_fns(self) -> None:
        sig1 = state(0)
        w1 = state(1)

        s = scorer([sig1], [w1], score_fns=[lambda v: v**2])  # square

        result: list[ScoredItem] = []

        def _sink(msgs: list) -> None:
            for msg in msgs:
                if msg[0] is MessageType.DATA:
                    result.append(msg[1])

        s.subscribe(_sink)

        sig1.down([(MessageType.DATA, 4)])
        assert result[-1].score == 16  # 4^2 * 1

    def test_rejects_mismatched_sources_weights(self) -> None:
        with pytest.raises(ValueError, match="same number of sources and weights"):
            scorer([state(0), state(0)], [state(1)])

    def test_rejects_empty_sources(self) -> None:
        with pytest.raises(ValueError, match="at least one source"):
            scorer([], [])

    def test_has_reduction_meta(self) -> None:
        s = scorer([state(0)], [state(1)])
        assert isinstance(s, NodeImpl)
        meta = s.meta
        assert meta["reduction"].get() is True
        assert meta["reduction_type"].get() == "scorer"
