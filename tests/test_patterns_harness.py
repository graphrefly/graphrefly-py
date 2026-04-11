"""Patterns harness tests (roadmap §9.0)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from graphrefly import state
from graphrefly.core.protocol import MessageType
from graphrefly.patterns.harness.bridge import (
    EvalJudgeScore,
    EvalResult,
    EvalTaskResult,
    eval_intake_bridge,
)
from graphrefly.patterns.harness.loop import HarnessGraph, harness_loop
from graphrefly.patterns.harness.strategy import priority_score, strategy_model
from graphrefly.patterns.harness.trace import TraceEvent, harness_trace
from graphrefly.patterns.harness.types import (
    ExecutionResult,
    IntakeItem,
    TriagedItem,
    default_error_classifier,
    strategy_key,
)
from graphrefly.patterns.messaging import TopicGraph
from tests.helpers.mock_llm import MockLLMAdapter, MockScript, StageScript

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
        result = ExecutionResult(item=item, outcome="failure", detail="JSON parse error at line 3")
        assert default_error_classifier(result) == "self-correctable"

        result2 = ExecutionResult(item=item, outcome="failure", detail="Config validation failed")
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
            lambda msgs: [values.append(msg[1]) for msg in msgs if msg[0] == MessageType.DATA]
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

        eval_source.down(
            [
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
                                    EvalJudgeScore(
                                        claim="correct topology", pass_=True, reasoning="ok"
                                    ),
                                    EvalJudgeScore(
                                        claim="uses feedback edges",
                                        pass_=False,
                                        reasoning="missing feedback",
                                    ),
                                    EvalJudgeScore(
                                        claim="handles errors",
                                        pass_=False,
                                        reasoning="no error handling",
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
            ]
        )

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

        eval_source.down(
            [
                (
                    MessageType.DATA,
                    EvalResult(
                        run_id="run-2",
                        model="gemini",
                        tasks=(EvalTaskResult(task_id="T2", valid=False),),
                    ),
                )
            ]
        )

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

        eval_source.down(
            [
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
            ]
        )

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

                return {
                    "content": json.dumps(
                        {
                            "root_cause": "unknown",
                            "intervention": "investigate",
                            "route": "backlog",
                            "priority": 10,
                        }
                    )
                }

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

    async def test_gate_modify_overrides_classification(self) -> None:
        """gate.modify() overrides rootCause/intervention before forwarding.

        Revalidated with harnessTrace (inspection consolidation §5) to verify
        that INTAKE → TRIAGE → QUEUE → GATE → EXECUTE → VERIFY → STRATEGY
        stages fire in the expected order after human steering.
        """
        mock = MockLLMAdapter(
            MockScript(
                stages={
                    "triage": StageScript(
                        responses=[
                            {
                                "root_cause": "unknown",
                                "intervention": "investigate",
                                "route": "needs-decision",
                                "priority": 60,
                            }
                        ]
                    ),
                    "execute": StageScript(
                        responses=[
                            {
                                "outcome": "success",
                                "detail": "Fixed with template",
                            }
                        ]
                    ),
                    "verify": StageScript(
                        responses=[
                            {
                                "verified": True,
                                "findings": ["ok"],
                            }
                        ]
                    ),
                }
            )
        )

        h = harness_loop("mock-gate-modify", adapter=mock, max_reingestions=0)

        # Wire harnessTrace to validate stage ordering
        trace_lines: list[str] = []
        trace_handle = harness_trace(h, logger=trace_lines.append)

        # Reactive waits: subscribe BEFORE triggering the pipeline.
        loop = asyncio.get_running_loop()

        # 1. Queue population — subscribe to the pre-created queue topic.
        queue = h.queues["needs-decision"]
        queue_ready: asyncio.Future[None] = loop.create_future()

        def _on_queue(msgs: object) -> None:
            for msg in msgs:  # type: ignore[union-attr]
                if msg[0] is MessageType.DATA and len(queue.retained()) >= 1:
                    with contextlib.suppress(asyncio.InvalidStateError):
                        loop.call_soon_threadsafe(queue_ready.set_result, None)

        unsub_queue = queue.latest.subscribe(_on_queue)

        # 2. Strategy entry — subscribe to strategy node.
        strategy_ready: asyncio.Future[None] = loop.create_future()

        def _on_strategy(msgs: object) -> None:
            for msg in msgs:  # type: ignore[union-attr]
                if (
                    msg[0] is MessageType.DATA
                    and h.strategy.lookup("composition", "template") is not None
                ):
                    with contextlib.suppress(asyncio.InvalidStateError):
                        loop.call_soon_threadsafe(strategy_ready.set_result, None)

        unsub_strat = h.strategy.node.subscribe(_on_strategy)

        # Publish intake item — pipeline runs async via AsyncioRunner.
        try:
            h.intake.publish(
                IntakeItem(
                    source="eval",
                    summary="T5: resilience ordering wrong",
                    evidence="wrong order in retry stack",
                    affects_areas=("graphspec",),
                    severity="high",
                )
            )

            # Await queue population (reactive, no polling — §5.8).
            await asyncio.wait_for(queue_ready, timeout=5.0)

            assert len(queue.retained()) >= 1

            gate_ctrl = h.gates["needs-decision"]

            # Human steering: override triage classification.
            # Items flowing through gates are dicts (router merges intake + triage
            # as plain dicts), so dict spread is the correct transform here.
            # Approve ALL pending items (START handshake may have buffered the
            # initial None value from topic.latest, matching the TS test).
            pending_count = gate_ctrl.count.get() or 1
            gate_ctrl.modify(
                lambda item, *_args: {
                    **(item if isinstance(item, dict) else {}),
                    "root_cause": "composition",
                    "intervention": "template",
                },
                count=pending_count,
            )

            # Reactive wait for strategy entry (no polling — §5.8).
            await asyncio.wait_for(strategy_ready, timeout=5.0)
        finally:
            unsub_queue()
            unsub_strat()

        entry = h.strategy.lookup("composition", "template")
        assert entry is not None
        assert entry.successes >= 1

        # Original classification should NOT appear
        assert h.strategy.lookup("unknown", "investigate") is None

        # harnessTrace revalidation: structured events validate stage ordering
        # without string parsing (inspection consolidation §5).
        trace_handle.dispose()
        evts: list[TraceEvent] = trace_handle.events
        stages_seen = [e.stage for e in evts if e.type == "data"]
        assert "INTAKE" in stages_seen, f"INTAKE stage not traced: {stages_seen}"
        assert "TRIAGE" in stages_seen, f"TRIAGE stage not traced: {stages_seen}"
        assert "STRATEGY" in stages_seen, f"STRATEGY stage not traced: {stages_seen}"

        # Verify stage ordering: INTAKE must come before TRIAGE, TRIAGE before STRATEGY
        intake_idx = next(i for i, s in enumerate(stages_seen) if s == "INTAKE")
        triage_idx = next(i for i, s in enumerate(stages_seen) if s == "TRIAGE")
        strategy_idx = next(i for i, s in enumerate(stages_seen) if s == "STRATEGY")
        assert intake_idx < triage_idx < strategy_idx, (
            f"Stage order violation: INTAKE@{intake_idx} "
            f"TRIAGE@{triage_idx} STRATEGY@{strategy_idx}"
        )

        # Verify trace_lines still work as the string logger fallback
        assert any("INTAKE" in line for line in trace_lines)


# ---------------------------------------------------------------------------
# harness_trace
# ---------------------------------------------------------------------------


class TestHarnessTrace:
    @staticmethod
    def _mock_adapter() -> Any:
        """Mock LLM adapter that returns predictable JSON."""

        class MockAdapter:
            def invoke(self, msgs: Any, **kw: Any) -> Any:
                import json

                return {
                    "content": json.dumps(
                        {
                            "root_cause": "unknown",
                            "intervention": "investigate",
                            "route": "backlog",
                            "priority": 10,
                        }
                    )
                }

        return MockAdapter()

    def test_harness_trace_returns_handle(self) -> None:
        from graphrefly.patterns.harness.trace import HarnessTraceHandle

        h = harness_loop("trace-test-1", adapter=self._mock_adapter())
        handle = harness_trace(h)

        assert isinstance(handle, HarnessTraceHandle)
        handle.dispose()

    def test_harness_trace_captures_intake(self) -> None:
        h = harness_loop("trace-test-2", adapter=self._mock_adapter())
        lines: list[str] = []
        handle = harness_trace(h, logger=lines.append)

        h.intake.publish(
            IntakeItem(
                source="human",
                summary="Test trace capture",
                evidence="evidence",
                affects_areas=("core",),
                severity="medium",
            )
        )

        assert any("INTAKE" in line for line in lines)
        handle.dispose()

    def test_harness_trace_captures_stage_nodes(self) -> None:
        h = harness_loop("trace-test-stages", adapter=self._mock_adapter())
        lines: list[str] = []
        handle = harness_trace(h, logger=lines.append)

        h.intake.publish(
            IntakeItem(
                source="human",
                summary="Stage node trace test",
                evidence="evidence",
                affects_areas=("core",),
                severity="medium",
            )
        )

        assert any("TRIAGE" in line for line in lines)
        handle.dispose()

    def test_harness_trace_captures_strategy(self) -> None:
        """STRATEGY events fire after a full auto-fix pipeline run."""
        mock = MockLLMAdapter(
            MockScript(
                stages={
                    "triage": StageScript(
                        responses=[
                            {
                                "root_cause": "composition",
                                "intervention": "template",
                                "route": "auto-fix",
                                "priority": 80,
                            }
                        ]
                    ),
                    "execute": StageScript(responses=[{"outcome": "success", "detail": "Fixed"}]),
                    "verify": StageScript(responses=[{"verified": True, "findings": []}]),
                }
            )
        )
        h = harness_loop("trace-test-strategy", adapter=mock, max_reingestions=0)
        lines: list[str] = []
        handle = harness_trace(h, logger=lines.append)

        h.intake.publish(
            IntakeItem(
                source="eval",
                summary="Trace strategy test item",
                evidence="evidence",
                affects_areas=("core",),
                severity="high",
            )
        )

        assert any("STRATEGY" in line for line in lines)
        handle.dispose()

    def test_harness_trace_elapsed_timestamps(self) -> None:
        """Lines include elapsed-time prefix in [X.XXXs] format."""
        h = harness_loop("trace-test-elapsed", adapter=self._mock_adapter())
        lines: list[str] = []
        handle = harness_trace(h, logger=lines.append)

        h.intake.publish(
            IntakeItem(
                source="human",
                summary="Elapsed timestamp test",
                evidence="evidence",
                affects_areas=("core",),
                severity="low",
            )
        )

        # All lines should start with [X.XXXs]
        assert all(line.startswith("[") and "s]" in line for line in lines)
        handle.dispose()

    def test_harness_trace_dispose_stops_capture(self) -> None:
        h = harness_loop("trace-test-3", adapter=self._mock_adapter())
        lines: list[str] = []
        handle = harness_trace(h, logger=lines.append)
        handle.dispose()

        h.intake.publish(
            IntakeItem(
                source="human",
                summary="After dispose",
                evidence="evidence",
                affects_areas=("core",),
                severity="low",
            )
        )

        # Give time for any accidental delivery
        time.sleep(0.05)
        assert not any("After dispose" in line for line in lines)


# ---------------------------------------------------------------------------
# Composition A: eval_source
# ---------------------------------------------------------------------------


class TestEvalSource:
    def test_fires_runner_on_trigger_and_emits_result(self) -> None:
        import threading

        from graphrefly.core.protocol import MessageType
        from graphrefly.patterns.harness.bridge import eval_source

        trigger = state("run-a")
        ready = threading.Event()

        def runner() -> EvalResult:
            return EvalResult(run_id="r1", model="test", tasks=())

        results: list[EvalResult] = []

        result_node = eval_source(trigger, runner)

        def _on(msgs: Any) -> None:
            for m in msgs:
                if m[0] is MessageType.DATA and m[1] is not None:
                    results.append(m[1])
                    ready.set()

        unsub = result_node.subscribe(_on)
        ready.wait(timeout=2.0)

        assert len(results) >= 1
        assert results[-1].run_id == "r1"
        unsub()


# ---------------------------------------------------------------------------
# Composition A: before_after_compare
# ---------------------------------------------------------------------------


class TestBeforeAfterCompare:
    def _make(
        self,
        run_id: str,
        tasks: list[tuple[str, bool, int | None, int | None]],
    ) -> EvalResult:
        """(task_id, valid, passes, total)"""
        task_list = []
        for tid, valid, passes, total in tasks:
            if total is not None:
                scores = tuple(
                    EvalJudgeScore(claim=f"c{i}", pass_=i < (passes or 0), reasoning="")
                    for i in range(total)
                )
            else:
                scores = None
            task_list.append(EvalTaskResult(task_id=tid, valid=valid, judge_scores=scores))
        return EvalResult(run_id=run_id, model="test", tasks=tuple(task_list))

    def test_identifies_new_failures_and_resolved(self) -> None:
        from graphrefly.patterns.harness.bridge import before_after_compare

        before = state(self._make("b", [("t1", True, None, None), ("t2", False, None, None)]))
        after = state(self._make("a", [("t1", False, None, None), ("t2", True, None, None)]))

        delta = before_after_compare(before, after)
        unsub = delta.subscribe(lambda _: None)

        d = delta.get()
        assert d is not None
        assert "t1" in d.new_failures
        assert "t2" in d.resolved
        assert d.overall_improved is False  # 1 resolved, 1 failure — equal
        unsub()

    def test_overall_improved_when_more_resolved(self) -> None:
        from graphrefly.patterns.harness.bridge import before_after_compare

        before = state(
            self._make(
                "b",
                [("t1", False, None, None), ("t2", False, None, None), ("t3", True, None, None)],
            )
        )
        after = state(
            self._make(
                "a",
                [("t1", True, None, None), ("t2", True, None, None), ("t3", False, None, None)],
            )
        )

        delta = before_after_compare(before, after)
        unsub = delta.subscribe(lambda _: None)

        d = delta.get()
        assert d is not None
        assert len(d.resolved) == 2
        assert len(d.new_failures) == 1
        assert d.overall_improved is True
        unsub()

    def test_score_diff_with_judge_scores(self) -> None:
        from graphrefly.patterns.harness.bridge import before_after_compare

        before = state(self._make("b", [("t1", True, 2, 4)]))
        after = state(self._make("a", [("t1", True, 3, 4)]))

        delta = before_after_compare(before, after)
        unsub = delta.subscribe(lambda _: None)

        d = delta.get()
        assert d is not None
        td = d.task_deltas[0]
        assert td.score_diff == 1  # 3 - 2
        unsub()


# ---------------------------------------------------------------------------
# Composition A: affected_task_filter
# ---------------------------------------------------------------------------


class TestAffectedTaskFilter:
    def _mk(self, tasks: list[str]) -> TriagedItem:
        return TriagedItem(
            source="eval",
            summary="",
            evidence="",
            affects_areas=(),
            root_cause="unknown",
            intervention="investigate",
            route="backlog",
            priority=0.0,
            affects_eval_tasks=tuple(tasks),
        )

    def test_collects_affected_task_ids(self) -> None:
        from graphrefly.patterns.harness.bridge import affected_task_filter

        issues = state([self._mk(["T1", "T2"]), self._mk(["T2", "T3"])])
        filtered = affected_task_filter(issues)
        unsub = filtered.subscribe(lambda _: None)

        assert filtered.get() == ["T1", "T2", "T3"]
        unsub()

    def test_intersects_with_full_task_set(self) -> None:
        from graphrefly.patterns.harness.bridge import affected_task_filter

        issues = state([self._mk(["T1", "T2", "T3"])])
        filtered = affected_task_filter(issues, ("T1", "T3", "T5"))
        unsub = filtered.subscribe(lambda _: None)

        assert filtered.get() == ["T1", "T3"]  # T2 excluded, T5 not affected
        unsub()


# ---------------------------------------------------------------------------
# Composition D: code_change_bridge
# ---------------------------------------------------------------------------


class TestCodeChangeBridge:
    def test_publishes_items_for_lint_and_test_failures(self) -> None:
        from graphrefly.patterns.harness.bridge import (
            CodeChange,
            LintError,
            TestFailure,
            code_change_bridge,
        )
        from graphrefly.patterns.messaging import topic

        source = state(None)
        intake_topic = topic("intake")
        published: list[IntakeItem] = []

        unsub_intake = intake_topic.latest.subscribe(
            lambda msgs: [
                published.append(m[1])
                for m in msgs
                if m[0] is MessageType.DATA and m[1] is not None
            ]
        )

        bridge = code_change_bridge(source, intake_topic)
        unsub_bridge = bridge.subscribe(lambda _: None)

        change = CodeChange(
            files=("src/foo.py",),
            lint_errors=(
                LintError(file="src/foo.py", line=10, col=3, rule="E501", message="line too long"),
            ),
            test_failures=(
                TestFailure(test_id="test_foo", file="src/foo.py", message="AssertionError"),
            ),
        )
        source.down([[MessageType.DATA, change]])

        assert len(published) >= 2
        sources = [i.source for i in published]
        assert "code-change" in sources
        assert "test" in sources
        unsub_intake()
        unsub_bridge()


# ---------------------------------------------------------------------------
# Composition D: notify_effect
# ---------------------------------------------------------------------------


class TestNotifyEffect:
    def test_calls_transport_for_each_entry(self) -> None:
        from graphrefly.patterns.harness.bridge import notify_effect
        from graphrefly.patterns.messaging import topic

        alert_topic = topic("alerts")
        calls: list[str] = []
        eff = notify_effect(alert_topic, calls.append)
        unsub = eff.subscribe(lambda _: None)

        alert_topic.publish("first")
        alert_topic.publish("second")

        assert "first" in calls
        assert "second" in calls
        unsub()


# ---------------------------------------------------------------------------
# Composition B: redactor
# ---------------------------------------------------------------------------


class TestRedactor:
    def test_replaces_matched_patterns(self) -> None:
        import re

        from graphrefly.patterns.ai import StreamChunk, redactor
        from graphrefly.patterns.messaging import topic

        stream = topic("stream")
        ssn_pattern = re.compile(r"\d{3}-\d{2}-\d{4}")
        sanitized = redactor(stream, [ssn_pattern])
        results: list[StreamChunk] = []
        unsub = sanitized.subscribe(
            lambda msgs: [
                results.append(m[1])
                for m in msgs
                if m[0] is MessageType.DATA and m[1] is not None and m[1].index >= 0
            ]
        )

        stream.publish(StreamChunk(
            source="test", token="SSN: 123-45-6789",
            accumulated="SSN: 123-45-6789", index=0,
        ))

        last = results[-1] if results else None
        assert last is not None
        assert last.accumulated == "SSN: [REDACTED]"
        unsub()

    def test_custom_replace_fn(self) -> None:
        import re

        from graphrefly.patterns.ai import StreamChunk, redactor
        from graphrefly.patterns.messaging import topic

        stream = topic("stream2")
        pattern = re.compile(r"secret", re.IGNORECASE)
        sanitized = redactor(stream, [pattern], replace_fn=lambda _m: "***")
        results: list[StreamChunk] = []
        unsub = sanitized.subscribe(
            lambda msgs: [
                results.append(m[1])
                for m in msgs
                if m[0] is MessageType.DATA and m[1] is not None and m[1].index >= 0
            ]
        )

        stream.publish(StreamChunk(
            source="test", token="my secret",
            accumulated="my secret data", index=0,
        ))

        last = results[-1] if results else None
        assert last is not None
        assert last.accumulated == "my *** data"
        unsub()


# ---------------------------------------------------------------------------
# Composition B: content_gate
# ---------------------------------------------------------------------------


class TestContentGate:
    def _push(self, stream: Any, acc: str) -> None:
        from graphrefly.patterns.ai import StreamChunk

        stream.publish(StreamChunk(source="test", token=acc, accumulated=acc, index=0))

    def test_returns_allow_below_threshold(self) -> None:
        from graphrefly.patterns.ai import content_gate
        from graphrefly.patterns.messaging import topic

        stream = topic("stream-cg-allow")
        gate = content_gate(stream, lambda text: len(text) / 100, threshold=0.5)
        decisions: list[str] = []
        unsub = gate.subscribe(
            lambda msgs: [
                decisions.append(m[1])
                for m in msgs
                if m[0] is MessageType.DATA and m[1] is not None
            ]
        )

        self._push(stream, "hi")  # 2/100 = 0.02 — below 0.5
        assert decisions[-1] == "allow"
        unsub()

    def test_returns_review_in_middle_band(self) -> None:
        from graphrefly.patterns.ai import content_gate
        from graphrefly.patterns.messaging import topic

        stream = topic("stream-cg-review")
        gate = content_gate(stream, lambda _: 0.6, threshold=0.5)  # hard = 0.75
        decisions: list[str] = []
        unsub = gate.subscribe(
            lambda msgs: [
                decisions.append(m[1])
                for m in msgs
                if m[0] is MessageType.DATA and m[1] is not None
            ]
        )

        self._push(stream, "x")
        assert decisions[-1] == "review"
        unsub()

    def test_returns_block_above_hard_threshold(self) -> None:
        from graphrefly.patterns.ai import content_gate
        from graphrefly.patterns.messaging import topic

        stream = topic("stream-cg-block")
        gate = content_gate(stream, lambda _: 0.9, threshold=0.5)  # 0.9 >= 0.75
        decisions: list[str] = []
        unsub = gate.subscribe(
            lambda msgs: [
                decisions.append(m[1])
                for m in msgs
                if m[0] is MessageType.DATA and m[1] is not None
            ]
        )

        self._push(stream, "x")
        assert decisions[-1] == "block"
        unsub()

    def test_accepts_node_classifier(self) -> None:
        from graphrefly.patterns.ai import content_gate
        from graphrefly.patterns.messaging import topic

        stream = topic("stream-cg-node")
        score = state(0.8)
        gate = content_gate(stream, score, threshold=0.5)  # 0.8 >= 0.75 → block
        decisions: list[str] = []
        unsub = gate.subscribe(
            lambda msgs: [
                decisions.append(m[1])
                for m in msgs
                if m[0] is MessageType.DATA and m[1] is not None
            ]
        )

        self._push(stream, "x")
        assert decisions[-1] == "block"

        # Lower score to allow
        score.down([[MessageType.DATA, 0.1]])
        self._push(stream, "y")
        assert decisions[-1] == "allow"
        unsub()
