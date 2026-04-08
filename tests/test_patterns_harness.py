"""Patterns harness tests (roadmap §9.0)."""

from __future__ import annotations

import time
from typing import Any

from graphrefly import state
from graphrefly.core.clock import monotonic_ns
from graphrefly.core.protocol import MessageType
from graphrefly.patterns.harness.bridge import (
    EvalJudgeScore,
    EvalResult,
    EvalTaskResult,
    eval_intake_bridge,
)
from graphrefly.patterns.harness.loop import HarnessGraph, harness_loop
from graphrefly.patterns.harness.strategy import priority_score, strategy_model
from graphrefly.patterns.harness.types import (
    ExecutionResult,
    IntakeItem,
    TriagedItem,
    default_error_classifier,
    strategy_key,
)
from graphrefly.patterns.messaging import TopicGraph
from tests.helpers.mock_llm import MockLLMAdapter, MockScript, StageScript


def _wait_for(predicate: Any, timeout_s: float = 5.0) -> None:
    deadline = monotonic_ns() + int(timeout_s * 1_000_000_000)
    while not predicate() and monotonic_ns() < deadline:
        time.sleep(0.02)

# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------


class TestTypes:
    def test_strategy_key_format(self) -> None:
        assert strategy_key("composition", "template") == "composition→template"
        assert strategy_key("missing-fn", "catalog-fn") == "missing-fn→catalog-fn"

    def test_default_error_classifier_self_correctable(self) -> None:
        item = TriagedItem(
            source="eval",
            summary="test",
            evidence="test",
            affects_areas=(),
            root_cause="composition",
            intervention="template",
            route="auto-fix",
            priority=50,
        )
        result = ExecutionResult(
            item=item, outcome="failure", detail="JSON parse error at line 3"
        )
        assert default_error_classifier(result) == "self-correctable"

        result2 = ExecutionResult(
            item=item, outcome="failure", detail="Config validation failed"
        )
        assert default_error_classifier(result2) == "self-correctable"

    def test_default_error_classifier_structural(self) -> None:
        item = TriagedItem(
            source="eval",
            summary="test",
            evidence="test",
            affects_areas=(),
            root_cause="composition",
            intervention="template",
            route="auto-fix",
            priority=50,
        )
        result = ExecutionResult(
            item=item,
            outcome="failure",
            detail="Missing node: rateLimiter not found in catalog",
        )
        assert default_error_classifier(result) == "structural"


# ---------------------------------------------------------------------------
# strategy model
# ---------------------------------------------------------------------------


class TestStrategyModel:
    def test_starts_empty(self) -> None:
        sm = strategy_model()
        val = sm.node.get()
        assert isinstance(val, dict)
        assert len(val) == 0
        assert sm.lookup("composition", "template") is None

    def test_records_successes_and_failures(self) -> None:
        sm = strategy_model()
        sm.record("composition", "template", True)
        sm.record("composition", "template", True)
        sm.record("composition", "template", False)

        entry = sm.lookup("composition", "template")
        assert entry is not None
        assert entry.attempts == 3
        assert entry.successes == 2
        assert abs(entry.success_rate - 2 / 3) < 0.01

    def test_tracks_pairs_independently(self) -> None:
        sm = strategy_model()
        sm.record("composition", "template", True)
        sm.record("missing-fn", "catalog-fn", True)
        sm.record("missing-fn", "catalog-fn", True)

        assert sm.lookup("composition", "template").attempts == 1
        assert sm.lookup("missing-fn", "catalog-fn").attempts == 2

    def test_reactive_node_updates(self) -> None:
        sm = strategy_model()
        values: list[dict] = []
        sm.node.subscribe(
            lambda msgs: [
                values.append(msg[1])
                for msg in msgs
                if msg[0] == MessageType.DATA
            ]
        )

        sm.record("bad-docs", "docs", True)
        assert len(values) >= 1
        assert "bad-docs→docs" in values[-1]


# ---------------------------------------------------------------------------
# priority score
# ---------------------------------------------------------------------------


class TestPriorityScore:
    def test_computes_score(self) -> None:
        import time

        item = state(
            TriagedItem(
                source="eval",
                summary="test",
                evidence="test",
                affects_areas=(),
                root_cause="composition",
                intervention="template",
                route="auto-fix",
                priority=50,
                severity="high",
            )
        )
        sm = strategy_model()
        last_interaction = state(time.monotonic_ns())

        score = priority_score(item, sm.node, last_interaction)
        score.subscribe(lambda _: None)

        val = score.get()
        assert isinstance(val, (int, float))
        assert val > 0

    def test_strategy_boost(self) -> None:
        import time

        item = state(
            TriagedItem(
                source="eval",
                summary="test",
                evidence="test",
                affects_areas=(),
                root_cause="composition",
                intervention="template",
                route="auto-fix",
                priority=50,
                severity="medium",
            )
        )
        sm = strategy_model()
        last_interaction = state(time.monotonic_ns())

        score_without = priority_score(item, sm.node, last_interaction)
        score_without.subscribe(lambda _: None)
        val_without = score_without.get()

        sm.record("composition", "template", True)
        sm.record("composition", "template", True)
        sm.record("composition", "template", True)

        score_with = priority_score(item, sm.node, last_interaction)
        score_with.subscribe(lambda _: None)
        val_with = score_with.get()

        assert val_with > val_without


# ---------------------------------------------------------------------------
# eval intake bridge
# ---------------------------------------------------------------------------


class TestEvalIntakeBridge:
    def test_publishes_per_criterion_findings(self) -> None:
        eval_source = state(None)
        intake = TopicGraph("test-intake")
        bridge_node = eval_intake_bridge(eval_source, intake)
        bridge_node.subscribe(lambda _: None)

        items: list[Any] = []
        intake.latest.subscribe(
            lambda msgs: [
                items.append(msg[1])
                for msg in msgs
                if msg[0] == MessageType.DATA and msg[1] is not None
            ]
        )

        eval_source.down([
            (
                MessageType.DATA,
                EvalResult(
                    run_id="run-1",
                    model="claude",
                    tasks=(
                        EvalTaskResult(
                            task_id="T1",
                            valid=True,
                            judge_scores=(
                                EvalJudgeScore(claim="correct topology", pass_=True, reasoning="ok"),
                                EvalJudgeScore(claim="uses feedback edges", pass_=False, reasoning="missing feedback"),
                                EvalJudgeScore(claim="handles errors", pass_=False, reasoning="no error handling"),
                            ),
                        ),
                    ),
                ),
            )
        ])

        assert len(items) == 2
        assert "uses feedback edges" in items[0].summary
        assert "handles errors" in items[1].summary
        assert items[0].source == "eval"
        assert items[0].affects_eval_tasks == ("T1",)

    def test_handles_task_level_invalidity(self) -> None:
        eval_source = state(None)
        intake = TopicGraph("test-intake-2")
        bridge_node = eval_intake_bridge(eval_source, intake)
        bridge_node.subscribe(lambda _: None)

        items: list[Any] = []
        intake.latest.subscribe(
            lambda msgs: [
                items.append(msg[1])
                for msg in msgs
                if msg[0] == MessageType.DATA and msg[1] is not None
            ]
        )

        eval_source.down([
            (
                MessageType.DATA,
                EvalResult(
                    run_id="run-2",
                    model="gemini",
                    tasks=(EvalTaskResult(task_id="T2", valid=False),),
                ),
            )
        ])

        assert len(items) == 1
        assert "T2 invalid" in items[0].summary

    def test_skips_fully_passing_tasks(self) -> None:
        eval_source = state(None)
        intake = TopicGraph("test-intake-3")
        bridge_node = eval_intake_bridge(eval_source, intake)
        bridge_node.subscribe(lambda _: None)

        items: list[Any] = []
        intake.latest.subscribe(
            lambda msgs: [
                items.append(msg[1])
                for msg in msgs
                if msg[0] == MessageType.DATA and msg[1] is not None
            ]
        )

        eval_source.down([
            (
                MessageType.DATA,
                EvalResult(
                    run_id="run-3",
                    model="claude",
                    tasks=(
                        EvalTaskResult(
                            task_id="T3",
                            valid=True,
                            judge_scores=(
                                EvalJudgeScore(claim="correct", pass_=True, reasoning="ok"),
                            ),
                        ),
                    ),
                ),
            )
        ])

        assert len(items) == 0


# ---------------------------------------------------------------------------
# harness_loop
# ---------------------------------------------------------------------------


class TestHarnessLoop:
    @staticmethod
    def _mock_adapter() -> Any:
        """Mock LLM adapter that returns predictable JSON."""

        class MockAdapter:
            def invoke(self, msgs: Any, **kw: Any) -> Any:
                import json

                return {"content": json.dumps({"root_cause": "unknown", "intervention": "investigate", "route": "backlog", "priority": 10})}

        return MockAdapter()

    def test_creates_harness_graph(self) -> None:
        adapter = self._mock_adapter()
        h = harness_loop("test-harness", adapter=adapter)

        assert isinstance(h, HarnessGraph)
        assert isinstance(h.intake, TopicGraph)
        assert len(h.queues) == 4
        assert "auto-fix" in h.queues
        assert "needs-decision" in h.queues
        assert "investigation" in h.queues
        assert "backlog" in h.queues
        assert h.strategy is not None
        assert isinstance(h.verify_results, TopicGraph)

    def test_gates_created_for_configured_queues(self) -> None:
        adapter = self._mock_adapter()
        h = harness_loop("test-harness-2", adapter=adapter)

        assert "needs-decision" in h.gates
        assert "investigation" in h.gates
        assert "auto-fix" not in h.gates
        assert "backlog" not in h.gates

    def test_custom_queue_config(self) -> None:
        from graphrefly.patterns.harness.types import QueueConfig

        adapter = self._mock_adapter()
        h = harness_loop(
            "test-harness-3",
            adapter=adapter,
            queues={
                "auto-fix": QueueConfig(gated=True),
                "needs-decision": QueueConfig(gated=False),
            },
        )

        assert "auto-fix" in h.gates
        assert "needs-decision" not in h.gates

    def test_intake_publish_no_error(self) -> None:
        adapter = self._mock_adapter()
        h = harness_loop("test-harness-4", adapter=adapter)

        h.intake.publish(
            IntakeItem(
                source="human",
                summary="Test issue",
                evidence="Test evidence",
                affects_areas=("core",),
            )
        )

    def test_strategy_accessible(self) -> None:
        adapter = self._mock_adapter()
        h = harness_loop("test-harness-5", adapter=adapter)

        h.strategy.record("composition", "template", True)
        entry = h.strategy.lookup("composition", "template")
        assert entry is not None
        assert entry.success_rate == 1.0

    def test_gate_modify_overrides_classification(self) -> None:
        """gate.modify() overrides rootCause/intervention before forwarding."""
        mock = MockLLMAdapter(MockScript(stages={
            "triage": StageScript(responses=[{
                "root_cause": "unknown",
                "intervention": "investigate",
                "route": "needs-decision",
                "priority": 60,
            }]),
            "execute": StageScript(responses=[{
                "outcome": "success",
                "detail": "Fixed with template",
            }]),
            "verify": StageScript(responses=[{
                "verified": True,
                "findings": ["ok"],
            }]),
        }))

        h = harness_loop("mock-gate-modify", adapter=mock, max_reingestions=0)

        h.intake.publish(IntakeItem(
            source="eval",
            summary="T5: resilience ordering wrong",
            evidence="wrong order in retry stack",
            affects_areas=("graphspec",),
            severity="high",
        ))

        # Wait for the item to arrive at the needs-decision queue
        queue = h.queues["needs-decision"]
        _wait_for(lambda: len(queue.retained()) >= 1)
        assert len(queue.retained()) >= 1

        gate_ctrl = h.gates["needs-decision"]

        # Human steering: override triage classification
        gate_ctrl.modify(
            lambda item, *_args: {
                **(item if isinstance(item, dict) else {}),
                "root_cause": "composition",
                "intervention": "template",
            },
            count=1,
        )

        # Wait for modified item to flow through execute → verify → strategy
        _wait_for(lambda: len(h.strategy.node.get()) > 0)

        # Strategy should record the OVERRIDDEN classification
        entry = h.strategy.lookup("composition", "template")
        assert entry is not None
        assert entry.successes >= 1

        # Original classification should NOT appear
        assert h.strategy.lookup("unknown", "investigate") is None
