"""harnessLoop() factory (roadmap §9.0).

Wires the static 7-stage topology: INTAKE → TRIAGE → QUEUE → GATE →
EXECUTE → VERIFY → REFLECT. Static topology, flowing data — the Kafka
insight applied to human+LLM collaboration.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from graphrefly.core.sugar import effect
from graphrefly.extra.tier1 import merge
from graphrefly.graph.graph import Graph
from graphrefly.patterns.ai import prompt_node
from graphrefly.patterns.messaging import TopicGraph
from graphrefly.patterns.orchestration import gate

from .strategy import StrategyModelBundle, strategy_model
from .types import (
    DEFAULT_QUEUE_CONFIGS,
    QUEUE_NAMES,
    ErrorClass,
    ExecutionResult,
    IntakeItem,
    QueueConfig,
    TriagedItem,
    default_error_classifier,
)

if TYPE_CHECKING:
    from collections.abc import Callable



# ---------------------------------------------------------------------------
# Default prompts
# ---------------------------------------------------------------------------

DEFAULT_TRIAGE_PROMPT = """You are a triage classifier for a reactive collaboration harness.

Given an intake item, classify it and output JSON:
{{
  "root_cause": "composition" | "missing-fn" | "bad-docs" | "schema-gap" | "regression" | "unknown",
  "intervention": "template" | "catalog-fn" | "docs" | "wrapper" | "schema-change" | "investigate",
  "route": "auto-fix" | "needs-decision" | "investigation" | "backlog",
  "priority": <number 0-100>,
  "triage_reasoning": "<one sentence>"
}}

Strategy model (past effectiveness):
{strategy}

Intake item:
{item}"""

DEFAULT_EXECUTE_PROMPT = """You are an implementation agent.

Given a triaged issue with root cause and intervention type, produce a fix.

Issue:
{item}

Output JSON:
{{
  "outcome": "success" | "failure" | "partial",
  "detail": "<description of what was done or what failed>"
}}"""

DEFAULT_VERIFY_PROMPT = """You are a QA reviewer.

Given an execution result, verify whether the fix is correct.

Execution:
{execution}

Original issue:
{item}

Output JSON:
{{
  "verified": true/false,
  "findings": ["<finding1>", ...],
  "error_class": "self-correctable" | "structural"
}}"""


# ---------------------------------------------------------------------------
# HarnessGraph
# ---------------------------------------------------------------------------


class HarnessGraph(Graph):
    """The graph returned by :func:`harness_loop`."""

    __slots__ = (
        "intake",
        "queues",
        "gates",
        "strategy",
        "verify_results",
    )

    def __init__(
        self,
        name: str,
        intake: TopicGraph,
        queues: dict[str, TopicGraph],
        gates: dict[str, Any],  # GateController
        strategy: StrategyModelBundle,
        verify_results: TopicGraph,
    ) -> None:
        super().__init__(name)
        self.intake = intake
        self.queues = queues
        self.gates = gates
        self.strategy = strategy
        self.verify_results = verify_results


# ---------------------------------------------------------------------------
# harness_loop factory
# ---------------------------------------------------------------------------


def harness_loop(
    name: str,
    *,
    adapter: Any,
    triage_prompt: str | Callable[..., str] | None = None,
    execute_prompt: str | Callable[..., str] | None = None,
    verify_prompt: str | Callable[..., str] | None = None,
    queues: dict[str, QueueConfig] | None = None,
    error_classifier: Callable[..., ErrorClass] | None = None,
    max_retries: int = 2,
    max_reingestions: int = 1,
    retained_limit: int = 1000,
) -> HarnessGraph:
    """Wire the reactive collaboration loop as a static-topology graph.

    The loop has 7 stages:

    1. **INTAKE** — items arrive from multiple sources via ``intake.publish()``
    2. **TRIAGE** — prompt_node classifies, routes, and prioritizes
    3. **QUEUE** — 4 priority-ordered TopicGraphs
    4. **GATE** — human approval on configurable queues
    5. **EXECUTE** — prompt_node or human implements the fix
    6. **VERIFY** — prompt_node reviews + optional fast-retry
    7. **REFLECT** — strategy model records outcomes
    """
    classify_error = error_classifier or default_error_classifier

    # Merge queue configs
    queue_configs: dict[str, QueueConfig] = {}
    for route in QUEUE_NAMES:
        base = DEFAULT_QUEUE_CONFIGS[route]
        override = (queues or {}).get(route)
        if override:
            queue_configs[route] = override
        else:
            queue_configs[route] = base

    # --- Stage 1: INTAKE ---
    intake = TopicGraph("intake", retained_limit=retained_limit)

    # --- Strategy model ---
    strat = strategy_model()

    # --- Stage 2: TRIAGE ---
    _triage_prompt = triage_prompt or (
        lambda item, strategy: DEFAULT_TRIAGE_PROMPT.format(
            strategy=json.dumps(strategy, default=str),
            item=json.dumps(item, default=str) if not isinstance(item, str) else item,
        )
    )

    triage_node = prompt_node(
        adapter,
        [intake.latest, strat.node],
        _triage_prompt,
        name="triage",
        format="json",
        retries=1,
    )

    # --- Stage 3: QUEUE ---
    queue_topics: dict[str, TopicGraph] = {}
    for route in QUEUE_NAMES:
        queue_topics[route] = TopicGraph(f"queue/{route}", retained_limit=retained_limit)

    # Router effect
    def _route(deps: list[Any], _actions: Any) -> None:
        item = deps[0]
        if item is None:
            return
        route = getattr(item, "route", None)
        if isinstance(item, dict):
            route = item.get("route")
        if route and route in queue_topics:
            queue_topics[route].publish(item)

    _router = effect([triage_node], _route)

    # --- Stage 4: GATE ---
    gate_graph = Graph("gates")
    gate_controllers: dict[str, Any] = {}

    for route in QUEUE_NAMES:
        config = queue_configs[route]
        topic = queue_topics[route]

        if config.gated:
            gate_graph.add(f"{route}/source", topic.latest)
            ctrl = gate(
                gate_graph,
                f"{route}/gate",
                f"{route}/source",
                max_pending=int(config.max_pending) if config.max_pending != float("inf") else 2**31,
                start_open=config.start_open,
            )
            gate_controllers[route] = ctrl

    # --- Stage 5: EXECUTE ---
    # Merge all gate outputs + ungated queue latests + retry feedback into a
    # single execute input using merge() (no imperative .down()).
    retry_topic: TopicGraph = TopicGraph("retry-input", retained_limit=retained_limit)

    queue_outputs: list[Any] = []
    for route in QUEUE_NAMES:
        config = queue_configs[route]
        if config.gated and route in gate_controllers:
            queue_outputs.append(gate_controllers[route].node)
        else:
            queue_outputs.append(queue_topics[route].latest)
    queue_outputs.append(retry_topic.latest)

    execute_input = merge(*queue_outputs)

    _execute_prompt = execute_prompt or (
        lambda item: DEFAULT_EXECUTE_PROMPT.format(
            item=json.dumps(item, default=str) if not isinstance(item, str) else item,
        )
    )

    execute_node = prompt_node(
        adapter,
        [execute_input],
        _execute_prompt,
        name="execute",
        format="json",
        retries=1,
    )

    # --- Stage 6: VERIFY ---
    verify_results = TopicGraph("verify-results", retained_limit=retained_limit)

    _verify_prompt = verify_prompt or (
        lambda execution, item: DEFAULT_VERIFY_PROMPT.format(
            execution=json.dumps(execution, default=str) if not isinstance(execution, str) else execution,
            item=json.dumps(item, default=str) if not isinstance(item, str) else item,
        )
    )

    verify_node = prompt_node(
        adapter,
        [execute_node, execute_input],
        _verify_prompt,
        name="verify",
        format="json",
        retries=1,
    )

    # --- Fast-retry path ---
    _max_reingestions = max_reingestions
    _reingestion_count = 0

    def _fast_retry(deps: list[Any], _actions: Any) -> None:
        nonlocal _reingestion_count
        vr = deps[0]
        if vr is None:
            return

        verified = getattr(vr, "verified", None)
        if isinstance(vr, dict):
            verified = vr.get("verified")

        if verified:
            item = getattr(vr, "item", None)
            if isinstance(vr, dict):
                item = vr.get("item")
            if item:
                rc = getattr(item, "root_cause", None)
                iv = getattr(item, "intervention", None)
                if isinstance(item, dict):
                    rc = item.get("root_cause")
                    iv = item.get("intervention")
                if rc and iv:
                    strat.record(rc, iv, True)
            verify_results.publish(vr)
            return

        # Failed verification
        item = getattr(vr, "item", vr.get("item") if isinstance(vr, dict) else None)
        execution = getattr(vr, "execution", vr.get("execution") if isinstance(vr, dict) else None)
        findings = getattr(vr, "findings", vr.get("findings") if isinstance(vr, dict) else ())
        retry_count = getattr(execution, "retry_count", 0) if execution else 0

        err_class = getattr(vr, "error_class", None)
        if isinstance(vr, dict):
            err_class = vr.get("error_class")
        if not err_class and item:
            err_class = classify_error(
                ExecutionResult(
                    item=item,
                    outcome="failure",
                    detail="; ".join(findings) if findings else "",
                    retry_count=0,
                )
            )

        if err_class == "self-correctable" and retry_count < max_retries:
            # Fast-retry: publish to retry topic (reactive, not imperative .down())
            if isinstance(item, TriagedItem):
                retry_item = replace(
                    item,
                    summary=f"[RETRY {retry_count + 1}/{max_retries}] {item.summary} — Previous: {'; '.join(findings)}",
                )
            else:
                retry_item = item
            retry_topic.publish(retry_item)
        else:
            # Structural failure or max retries → full loop via INTAKE
            if item:
                rc = getattr(item, "root_cause", None)
                iv = getattr(item, "intervention", None)
                if isinstance(item, dict):
                    rc = item.get("root_cause")
                    iv = item.get("intervention")
                if rc and iv:
                    strat.record(rc, iv, False)
            verify_results.publish(vr)

            # Re-ingest only if under reingestion cap
            if item and _reingestion_count < _max_reingestions:
                _reingestion_count += 1
                summary = getattr(item, "summary", str(item))
                areas = getattr(item, "affects_areas", ())
                eval_tasks = getattr(item, "affects_eval_tasks", None)
                intake.publish(
                    IntakeItem(
                        source="eval",
                        summary=f"Verification failed for: {summary}",
                        evidence="\n".join(findings) if findings else "",
                        affects_areas=tuple(areas),
                        affects_eval_tasks=tuple(eval_tasks) if eval_tasks else None,
                        severity="high",
                        related_to=(summary,),
                    )
                )

    _retry_effect = effect([verify_node], _fast_retry)

    # --- Assemble HarnessGraph ---
    harness = HarnessGraph(
        name,
        intake=intake,
        queues=queue_topics,
        gates=gate_controllers,
        strategy=strat,
        verify_results=verify_results,
    )

    # Mount subgraphs
    harness.mount("intake", intake)
    for route, topic in queue_topics.items():
        harness.mount(f"queue/{route}", topic)
    harness.mount("gates", gate_graph)
    harness.mount("retry-input", retry_topic)
    harness.mount("verify-results", verify_results)

    return harness
