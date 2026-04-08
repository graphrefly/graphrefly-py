"""harnessLoop() factory (roadmap §9.0).

Wires the static 7-stage topology: INTAKE → TRIAGE → QUEUE → GATE →
EXECUTE → VERIFY → REFLECT. Static topology, flowing data — the Kafka
insight applied to human+LLM collaboration.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import effect, state
from graphrefly.extra.tier1 import merge, with_latest_from
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
    PrioritySignals,
    QueueConfig,
    TriagedItem,
    default_error_classifier,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _tracking_key(item: Any) -> str:
    """Stable tracking key for an item.

    Uses ``related_to[0]`` if the item is already a retry or reingestion
    (carries the original key forward). Falls back to the raw summary.
    """
    related = (
        item.get("related_to") if isinstance(item, dict) else getattr(item, "related_to", None)
    )
    if related:
        first = related[0] if isinstance(related, (list, tuple)) else None
        if first:
            return str(first)
    if isinstance(item, dict):
        summary = item.get("summary", str(item))
    else:
        summary = getattr(item, "summary", str(item))
    return str(summary)



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
        "total_retries",
        "total_reingestions",
    )

    def __init__(
        self,
        name: str,
        intake: TopicGraph,
        queues: dict[str, TopicGraph],
        gates: dict[str, Any],  # GateController
        strategy: StrategyModelBundle,
        verify_results: TopicGraph,
        total_retries: Any,  # Node[int] — reactive state node
        total_reingestions: Any,  # Node[int] — reactive state node
    ) -> None:
        super().__init__(name)
        self.intake = intake
        self.queues = queues
        self.gates = gates
        self.strategy = strategy
        self.verify_results = verify_results
        self.total_retries = total_retries
        self.total_reingestions = total_reingestions


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
    priority_signals: PrioritySignals | None = None,
    error_classifier: Callable[..., ErrorClass] | None = None,
    max_retries: int = 2,
    max_total_retries: int | None = None,
    max_reingestions: int = 1,
    max_total_reingestions: int | None = None,
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
    # Strategy context uses with_latest_from: intake.latest is the reactive
    # trigger, strat.node is sampled without being a trigger. This breaks the
    # feedback cycle (verify → strategy.record() → strat.node) reactively —
    # strategy changes don't re-fire triage, only new intake items do.
    triage_input = intake.latest | with_latest_from(strat.node)

    _triage_prompt = triage_prompt or (
        lambda pair: DEFAULT_TRIAGE_PROMPT.format(
            strategy=json.dumps(pair[1], default=str),
            item=json.dumps(pair[0], default=str) if not isinstance(pair[0], str) else pair[0],
        )
        if pair[0] is not None
        else ""
    )

    triage_node = prompt_node(
        adapter,
        [triage_input],
        _triage_prompt,
        name="triage",
        format="json",
        retries=1,
    )

    # --- Stage 3: QUEUE ---
    queue_topics: dict[str, TopicGraph] = {}
    for route in QUEUE_NAMES:
        queue_topics[route] = TopicGraph(f"queue/{route}", retained_limit=retained_limit)

    # Router: merge intake fields into triage classification before routing.
    # Sample triage_input (not intake.latest) — triage_input holds the
    # [item, strategy] pair that triggered this specific triage, so we get
    # the correct item even if a newer intake has arrived since.
    router_input = triage_node | with_latest_from(triage_input)

    def _route(deps: list[Any], _actions: Any) -> None:
        pair = deps[0]
        if pair is None:
            return
        classification, triage_pair = pair
        if classification is None:
            return
        route = (
            classification.get("route") if isinstance(classification, dict)
            else getattr(classification, "route", None)
        )
        if not route or route not in queue_topics:
            return
        # Merge intake item fields with triage classification
        intake_item = triage_pair[0] if triage_pair else None
        intake_dict: dict[str, Any] = {}
        if intake_item is not None:
            if isinstance(intake_item, dict):
                intake_dict = intake_item
            else:
                for f in ("source", "summary", "evidence", "affects_areas",
                          "affects_eval_tasks", "severity", "related_to"):
                    v = getattr(intake_item, f, None)
                    if v is not None:
                        intake_dict[f] = v
        if isinstance(classification, dict):
            merged = {**intake_dict, **classification}
        else:
            merged = classification
        queue_topics[route].publish(merged)

    _router = effect([router_input], _route)
    _router_unsub = _router.subscribe(lambda _msgs: None)  # keepalive (COMPOSITION-GUIDE §1)

    # --- Stage 4: GATE ---
    gate_graph = Graph("gates")
    gate_controllers: dict[str, Any] = {}

    for route in QUEUE_NAMES:
        config = queue_configs[route]
        topic = queue_topics[route]

        if config.gated:
            gate_graph.add(f"{route}/source", topic.latest)
            max_pending = (
                int(config.max_pending)
                if config.max_pending != float("inf")
                else 2**31
            )
            ctrl = gate(
                gate_graph,
                f"{route}/gate",
                f"{route}/source",
                max_pending=max_pending,
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
            execution=(
                json.dumps(execution, default=str)
                if not isinstance(execution, str)
                else execution
            ),
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
    # Assemble full VerifyResult from verify output + execution context.
    #
    # F2 fix: use nested with_latest_from so the effect fires ONLY when
    # verify_node settles, sampling execute_node + execute_input at that moment.
    verify_with_exec = verify_node | with_latest_from(execute_node)
    verify_context = verify_with_exec | with_latest_from(execute_input)

    _max_reingestions = max_reingestions
    _max_total_retries = (
        min(max_total_retries, 100) if max_total_retries is not None
        else min(max_retries * 10, 100)
    )
    _max_total_reingestions = (
        min(max_total_reingestions, 100) if max_total_reingestions is not None
        else min(max_reingestions * 10, 100)
    )
    total_retries = state(0)
    total_reingestions = state(0)

    def _fast_retry(deps: list[Any], _actions: Any) -> None:
        ctx = deps[0]
        if ctx is None:
            return
        (vo, exec_raw), item = ctx
        if vo is None or item is None:
            return

        verified = vo.get("verified") if isinstance(vo, dict) else getattr(vo, "verified", None)
        findings = vo.get("findings", ()) if isinstance(vo, dict) else getattr(vo, "findings", ())

        # Assemble full ExecutionResult + VerifyResult from LLM outputs + context
        rc = (
            item.get("root_cause")
            if isinstance(item, dict)
            else getattr(item, "root_cause", None)
        )
        iv = (
            item.get("intervention")
            if isinstance(item, dict)
            else getattr(item, "intervention", None)
        )
        exec_outcome = (
            exec_raw.get("outcome", "failure") if isinstance(exec_raw, dict)
            else getattr(exec_raw, "outcome", "failure")
        ) if exec_raw else "failure"
        exec_detail = (
            exec_raw.get("detail", "unknown") if isinstance(exec_raw, dict)
            else getattr(exec_raw, "detail", "unknown")
        ) if exec_raw else "unknown"
        err_class_raw = (
            vo.get("error_class")
            if isinstance(vo, dict)
            else getattr(vo, "error_class", None)
        )
        vr = {
            "item": item,
            "execution": {"item": item, "outcome": exec_outcome, "detail": exec_detail},
            "verified": verified,
            "findings": list(findings) if findings else [],
            "error_class": err_class_raw,
        }

        if verified:
            if rc and iv:
                strat.record(rc, iv, True)
            verify_results.publish(vr)
            return

        # Failed verification
        err_class = err_class_raw
        if not err_class:
            err_class = classify_error(
                ExecutionResult(
                    item=item,
                    outcome="failure",
                    detail="; ".join(findings) if findings else "",
                )
            )

        item_retries = (
            item.get("_retries", 0) if isinstance(item, dict)
            else getattr(item, "_retries", 0)
        )

        if (
            err_class == "self-correctable"
            and item_retries < max_retries
            and (total_retries.get() or 0) < _max_total_retries
        ):
            total_retries.down([(MessageType.DATA, (total_retries.get() or 0) + 1)])
            key = _tracking_key(item)
            prev_findings = "; ".join(findings)
            retry_item: Any
            if isinstance(item, TriagedItem):
                retry_item = replace(
                    item,
                    _retries=item_retries + 1,
                    summary=(
                        f"[RETRY {item_retries + 1}/{max_retries}]"
                        f" {key} — Previous: {prev_findings}"
                    ),
                    related_to=(key,),
                )
            elif isinstance(item, dict):
                retry_item = {
                    **item,
                    "_retries": item_retries + 1,
                    "summary": (
                        f"[RETRY {item_retries + 1}/{max_retries}]"
                        f" {key} — Previous: {prev_findings}"
                    ),
                    "related_to": (key,),
                }
            else:
                retry_item = item
            retry_topic.publish(retry_item)
        else:
            # Structural failure or max retries exceeded → full loop via INTAKE
            if rc and iv:
                strat.record(rc, iv, False)
            verify_results.publish(vr)

            key = _tracking_key(item)
            item_reingestions = (
                item.get("_reingestions", 0) if isinstance(item, dict)
                else getattr(item, "_reingestions", 0)
            )
            if (
                item_reingestions < _max_reingestions
                and (total_reingestions.get() or 0) < _max_total_reingestions
            ):
                total_reingestions.down([(MessageType.DATA, (total_reingestions.get() or 0) + 1)])
                areas = (
                    item.get("affects_areas", ())
                    if isinstance(item, dict)
                    else getattr(item, "affects_areas", ())
                )
                eval_tasks = (
                    item.get("affects_eval_tasks")
                    if isinstance(item, dict)
                    else getattr(item, "affects_eval_tasks", None)
                )
                intake.publish(
                    IntakeItem(
                        source="eval",
                        summary=f"Verification failed for: {key}",
                        evidence="\n".join(findings) if findings else "",
                        affects_areas=tuple(areas),
                        affects_eval_tasks=tuple(eval_tasks) if eval_tasks else None,
                        severity="high",
                        related_to=(key,),
                        _reingestions=item_reingestions + 1,
                    )
                )

    _retry_effect = effect([verify_context], _fast_retry)
    _retry_unsub = _retry_effect.subscribe(lambda _msgs: None)  # keepalive (COMPOSITION-GUIDE §1)

    # --- Assemble HarnessGraph ---
    harness = HarnessGraph(
        name,
        intake=intake,
        queues=queue_topics,
        gates=gate_controllers,
        strategy=strat,
        verify_results=verify_results,
        total_retries=total_retries,
        total_reingestions=total_reingestions,
    )

    # Register keepalive disposers
    harness.add_disposer(_router_unsub)
    harness.add_disposer(_retry_unsub)
    harness.add_disposer(strat.dispose)

    # Mount subgraphs
    harness.mount("intake", intake)
    for route, topic in queue_topics.items():
        harness.mount(f"queue/{route}", topic)
    harness.mount("gates", gate_graph)
    harness.mount("retry-input", retry_topic)
    harness.mount("verify-results", verify_results)

    return harness
