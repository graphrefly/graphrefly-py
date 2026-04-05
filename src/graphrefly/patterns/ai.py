"""AI surface patterns (roadmap §4.4).

Domain-layer factories for LLM-backed agents, chat, tool registries, and
agentic memory. Composed from core + extra + Phase 3–4.3 primitives.
"""

from __future__ import annotations

import inspect
import json
import math
import re as _re
import threading
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from graphrefly.core.clock import monotonic_ns
from graphrefly.core.node import Node
from graphrefly.core.protocol import MessageType, batch
from graphrefly.core.sugar import derived, effect, producer, state
from graphrefly.extra.composite import distill
from graphrefly.extra.data_structures import reactive_log
from graphrefly.extra.sources import first_value_from, from_any, from_timer
from graphrefly.extra.tier2 import switch_map
from graphrefly.graph.graph import Graph
from graphrefly.patterns.memory import (
    KnowledgeGraph,
    VectorIndex,
    decay,
    knowledge_graph,
    light_collection,
    vector_index,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from graphrefly.core.node import NodeImpl


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A single chat message in a conversation."""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool invocation request from an LLM."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """The response from an LLM invocation."""

    content: str
    tool_calls: tuple[ToolCall, ...] | None = None
    usage: dict[str, int] | None = None
    finish_reason: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class LLMInvokeOptions:
    """Options for :meth:`LLMAdapter.invoke`."""

    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tools: tuple[ToolDefinition, ...] | None = None
    system_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A tool definition for LLM consumption."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable[..., Any] = field(default=lambda args: None)
    version: dict[str, Any] | None = field(default=None)
    """V0 version of the backing node at ``knobs_as_tools()`` call time (§6.0b).
    Snapshot — re-call ``knobs_as_tools()`` to refresh."""


@runtime_checkable
class LLMAdapter(Protocol):
    """Provider-agnostic LLM client adapter protocol."""

    def invoke(
        self,
        messages: Sequence[ChatMessage],
        opts: LLMInvokeOptions | None = None,
    ) -> Any:
        """Invoke the LLM. Returns NodeInput[LLMResponse]."""
        ...


AgentLoopStatus = str  # "idle" | "thinking" | "acting" | "done" | "error"


# ---------------------------------------------------------------------------
# Meta helpers
# ---------------------------------------------------------------------------


def _ai_meta(kind: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ai": True, "ai_type": kind}
    if extra:
        out.update(extra)
    return out


def _keepalive(n: Any) -> Any:
    """Subscribe to keep derived node wired; returns unsubscribe handle."""
    return n.subscribe(lambda _msgs: None)


_DEFAULT_TIMEOUT = 30.0  # seconds


def _resolve_node_input(raw: Any, *, timeout: float = _DEFAULT_TIMEOUT) -> Any:
    """Resolve tool handler output via ``from_any`` / ``get()`` and first ``DATA``."""
    if isinstance(raw, Node):
        # Only trust get() when node is in settled state
        if getattr(raw, "status", None) == "settled":
            cached = raw.get()
            if cached is not None:
                return cached
        try:
            return first_value_from(raw, timeout=timeout)
        except StopIteration:
            msg = "tool_registry: handler completed without producing a value"
            raise ValueError(msg) from None
    if inspect.isawaitable(raw):
        try:
            return first_value_from(from_any(raw), timeout=timeout)
        except StopIteration:
            msg = "tool_registry: awaitable handler completed without producing a value"
            raise ValueError(msg) from None
    if isinstance(raw, AsyncIterable):
        try:
            return first_value_from(from_any(raw), timeout=timeout)
        except StopIteration:
            msg = "tool_registry: async iterable handler completed without producing a value"
            raise ValueError(msg) from None
    return raw


def _tuple_snapshot(raw: Any) -> tuple[Any, ...]:
    if isinstance(raw, tuple):
        return raw
    if isinstance(raw, list):
        return tuple(raw)
    return ()


# ---------------------------------------------------------------------------
# from_llm
# ---------------------------------------------------------------------------


def from_llm(
    adapter: LLMAdapter,
    messages: Any,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    tools: Sequence[ToolDefinition] | None = None,
    system_prompt: str | None = None,
    name: str | None = None,
) -> Any:
    """Reactive LLM invocation adapter.

    Returns a derived node that re-invokes the LLM whenever the messages
    dep changes. Uses ``switch_map`` internally — new invocations cancel
    stale in-flight ones.
    """
    msgs_node = from_any(messages)
    invoke_opts = LLMInvokeOptions(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tuple(tools) if tools else None,
        system_prompt=system_prompt,
    )

    def _invoke(msgs: Any) -> Any:
        if not msgs:
            return state(None)
        return adapter.invoke(msgs, invoke_opts)

    return switch_map(_invoke)(msgs_node)


# ---------------------------------------------------------------------------
# chat_stream
# ---------------------------------------------------------------------------


class ChatStreamGraph(Graph):
    """Reactive chat message stream with role tracking."""

    __slots__ = ("_keepalive_subs", "_log", "messages", "latest", "message_count")

    def __init__(
        self,
        name: str,
        *,
        opts: dict[str, Any] | None = None,
        max_messages: int | None = None,
    ) -> None:
        super().__init__(name, opts)
        self._keepalive_subs: list[Any] = []
        self._log = reactive_log(max_size=max_messages, name="messages")
        self.messages = self._log.entries
        self.add("messages", self.messages)

        def compute_latest(deps: list[Any], _actions: Any) -> Any:
            raw = deps[0]
            entries = _tuple_snapshot(raw.value if hasattr(raw, "value") else ())
            return entries[-1] if entries else None

        self.latest: NodeImpl[ChatMessage | None] = derived(
            [self.messages],
            compute_latest,
            name="latest",
            meta=_ai_meta("chat_latest"),
            initial=None,
        )
        self.add("latest", self.latest)
        self.connect("messages", "latest")
        self._keepalive_subs.append(_keepalive(self.latest))

        def compute_count(deps: list[Any], _actions: Any) -> int:
            raw = deps[0]
            entries = _tuple_snapshot(raw.value if hasattr(raw, "value") else ())
            return len(entries)

        self.message_count: NodeImpl[int] = derived(
            [self.messages],
            compute_count,
            name="messageCount",
            meta=_ai_meta("chat_message_count"),
            initial=0,
        )
        self.add("messageCount", self.message_count)
        self.connect("messages", "messageCount")
        self._keepalive_subs.append(_keepalive(self.message_count))

    def append(
        self,
        role: str,
        content: str,
        *,
        name: str | None = None,
        tool_call_id: str | None = None,
        tool_calls: tuple[ToolCall, ...] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append a message to the chat stream."""
        self._log.append(
            ChatMessage(
                role=role,
                content=content,
                name=name,
                tool_call_id=tool_call_id,
                tool_calls=tool_calls,
                metadata=metadata,
            )
        )

    def append_tool_result(self, call_id: str, content: str) -> None:
        """Append a tool result message."""
        self._log.append(ChatMessage(role="tool", content=content, tool_call_id=call_id))

    def clear(self) -> None:
        """Clear all messages."""
        self._log.clear()

    def all_messages(self) -> tuple[ChatMessage, ...]:
        """Return all messages as a tuple."""
        raw = self.messages.get()
        if raw is None or not hasattr(raw, "value"):
            return ()
        return _tuple_snapshot(raw.value)

    def destroy(self) -> None:
        for unsub in self._keepalive_subs:
            unsub()
        self._keepalive_subs.clear()
        super().destroy()


def chat_stream(
    name: str,
    *,
    opts: dict[str, Any] | None = None,
    max_messages: int | None = None,
) -> ChatStreamGraph:
    """Create a reactive chat stream graph."""
    return ChatStreamGraph(name, opts=opts, max_messages=max_messages)


# ---------------------------------------------------------------------------
# tool_registry
# ---------------------------------------------------------------------------


class ToolRegistryGraph(Graph):
    """Tool definition store + dispatch."""

    __slots__ = ("_keepalive_subs", "definitions", "schemas")

    def __init__(
        self,
        name: str,
        *,
        opts: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(name, opts)
        self._keepalive_subs: list[Any] = []

        self.definitions: NodeImpl[Mapping[str, ToolDefinition]] = state(
            {},
            name="definitions",
            describe_kind="state",
            meta=_ai_meta("tool_definitions"),
        )
        self.add("definitions", self.definitions)

        def compute_schemas(deps: list[Any], _actions: Any) -> tuple[ToolDefinition, ...]:
            defs = deps[0]
            if defs is None:
                return ()
            return tuple(defs.values()) if isinstance(defs, dict) else ()

        self.schemas: NodeImpl[tuple[ToolDefinition, ...]] = derived(
            [self.definitions],
            compute_schemas,
            name="schemas",
            meta=_ai_meta("tool_schemas"),
            initial=(),
        )
        self.add("schemas", self.schemas)
        self.connect("definitions", "schemas")
        self._keepalive_subs.append(_keepalive(self.schemas))

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool definition."""
        current = self.definitions.get() or {}
        next_defs = dict(current)
        next_defs[tool.name] = tool
        self.definitions.down([(MessageType.DATA, next_defs)])

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        current = self.definitions.get() or {}
        if name not in current:
            return
        next_defs = dict(current)
        del next_defs[name]
        self.definitions.down([(MessageType.DATA, next_defs)])

    def execute(self, name: str, args: dict[str, Any]) -> Any:
        """Execute a tool by name. Resolves async/reactive handler results."""
        defs = self.definitions.get() or {}
        tool = defs.get(name)
        if tool is None:
            msg = f'tool_registry: unknown tool "{name}"'
            raise ValueError(msg)
        return _resolve_node_input(tool.handler(args))

    def get_definition(self, name: str) -> ToolDefinition | None:
        """Get a tool definition by name."""
        defs = self.definitions.get() or {}
        return defs.get(name)

    def destroy(self) -> None:
        for unsub in self._keepalive_subs:
            unsub()
        self._keepalive_subs.clear()
        super().destroy()


def tool_registry(
    name: str,
    *,
    opts: dict[str, Any] | None = None,
) -> ToolRegistryGraph:
    """Create a tool registry graph."""
    return ToolRegistryGraph(name, opts=opts)


# ---------------------------------------------------------------------------
# system_prompt_builder
# ---------------------------------------------------------------------------


class SystemPromptHandle:
    """A system prompt node with a ``dispose()`` method for cleanup."""

    __slots__ = ("_node", "_unsub")

    def __init__(self, node: Any, unsub: Any) -> None:
        self._node = node
        self._unsub = unsub

    def get(self) -> str:
        return self._node.get()

    def subscribe(self, listener: Any) -> Any:
        return self._node.subscribe(listener)

    def down(self, msgs: Any) -> None:
        self._node.down(msgs)

    def dispose(self) -> None:
        self._unsub()

    def describe(self) -> Any:
        return self._node.describe() if hasattr(self._node, "describe") else {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._node, name)


def system_prompt_builder(
    sections: Sequence[Any],
    *,
    separator: str = "\n\n",
    name: str | None = None,
) -> SystemPromptHandle:
    """Assemble a system prompt from reactive sections.

    Each section is a ``NodeInput[str]`` — the prompt updates when any
    section changes.
    """
    section_nodes = [state(s) if isinstance(s, str) else from_any(s) for s in sections]

    def compute_prompt(deps: list[Any], _actions: Any) -> str:
        return separator.join(str(v) for v in deps if v is not None and v != "")

    result = derived(
        section_nodes,
        compute_prompt,
        name=name or "systemPrompt",
        meta=_ai_meta("system_prompt"),
        initial="",
    )
    unsub = _keepalive(result)
    return SystemPromptHandle(result, unsub)


# ---------------------------------------------------------------------------
# llm_extractor / llm_consolidator
# ---------------------------------------------------------------------------


def llm_extractor(
    system_prompt: str,
    *,
    adapter: LLMAdapter,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Callable[[Any, Mapping[str, Any]], Any]:
    """Return an ``extract_fn`` callback for :func:`distill`.

    The system prompt should instruct the LLM to return JSON matching
    ``Extraction`` shape: ``{ "upsert": [{ "key": ..., "value": ... }], "remove": [...] }``.
    """

    def extract_fn(raw: Any, existing: Mapping[str, Any]) -> Any:
        existing_keys = list(existing.keys())[:100]
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(
                role="user",
                content=json.dumps({"input": raw, "existingKeys": existing_keys}, default=str),
            ),
        ]

        def _produce(deps: Any, actions: Any) -> Any:
            result = adapter.invoke(
                messages,
                LLMInvokeOptions(
                    model=model,
                    temperature=temperature if temperature is not None else 0,
                    max_tokens=max_tokens,
                ),
            )
            resolved = from_any(result)
            active = True

            def _on_msg(msgs: Any) -> None:
                nonlocal active
                if not active:
                    return
                done = False
                for msg in msgs:
                    if done:
                        break
                    if msg[0] == MessageType.DATA:
                        response = msg[1]
                        try:
                            parsed = json.loads(response.content)
                            actions.emit(parsed)
                            actions.down([(MessageType.COMPLETE,)])
                        except Exception:
                            actions.down(
                                [
                                    (
                                        MessageType.ERROR,
                                        ValueError("llm_extractor: failed to parse LLM response"),
                                    )
                                ]
                            )
                        done = True
                    elif msg[0] == MessageType.ERROR:
                        actions.down([(MessageType.ERROR, msg[1])])
                        done = True
                    elif msg[0] == MessageType.COMPLETE:
                        actions.down([(MessageType.COMPLETE,)])
                        done = True
                    else:
                        # Forward unknown message types (spec §1.3.6)
                        actions.down([(msg[0], msg[1] if len(msg) > 1 else None)])

            unsub = resolved.subscribe(_on_msg)

            def cleanup() -> None:
                nonlocal active
                unsub()
                active = False

            return cleanup

        return producer(_produce)

    return extract_fn


def llm_consolidator(
    system_prompt: str,
    *,
    adapter: LLMAdapter,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Callable[[Mapping[str, Any]], Any]:
    """Return a ``consolidate_fn`` callback for :func:`distill`.

    The system prompt should instruct the LLM to cluster and merge
    related memories.
    """

    def consolidate_fn(entries: Mapping[str, Any]) -> Any:
        entries_list = [{"key": k, "value": v} for k, v in entries.items()]
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=json.dumps({"memories": entries_list}, default=str)),
        ]

        def _produce(deps: Any, actions: Any) -> Any:
            result = adapter.invoke(
                messages,
                LLMInvokeOptions(
                    model=model,
                    temperature=temperature if temperature is not None else 0,
                    max_tokens=max_tokens,
                ),
            )
            resolved = from_any(result)
            active = True

            def _on_msg(msgs: Any) -> None:
                nonlocal active
                if not active:
                    return
                done = False
                for msg in msgs:
                    if done:
                        break
                    if msg[0] == MessageType.DATA:
                        response = msg[1]
                        try:
                            parsed = json.loads(response.content)
                            actions.emit(parsed)
                            actions.down([(MessageType.COMPLETE,)])
                        except Exception:
                            actions.down(
                                [
                                    (
                                        MessageType.ERROR,
                                        ValueError(
                                            "llm_consolidator: failed to parse LLM response"
                                        ),
                                    )
                                ]
                            )
                        done = True
                    elif msg[0] == MessageType.ERROR:
                        actions.down([(MessageType.ERROR, msg[1])])
                        done = True
                    elif msg[0] == MessageType.COMPLETE:
                        actions.down([(MessageType.COMPLETE,)])
                        done = True
                    else:
                        # Forward unknown message types (spec §1.3.6)
                        actions.down([(msg[0], msg[1] if len(msg) > 1 else None)])

            unsub = resolved.subscribe(_on_msg)

            def cleanup() -> None:
                nonlocal active
                unsub()
                active = False

            return cleanup

        return producer(_produce)

    return consolidate_fn


# ---------------------------------------------------------------------------
# 3D Admission Scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmissionScores:
    """Scores for the three admission dimensions (each 0–1)."""

    persistence: float
    structure: float
    personal_value: float


def _default_admission_scorer(_raw: Any) -> AdmissionScores:
    return AdmissionScores(persistence=0.5, structure=0.5, personal_value=0.5)


def admission_filter_3d(
    *,
    score_fn: Callable[[Any], AdmissionScores] | None = None,
    persistence_threshold: float = 0.3,
    personal_value_threshold: float = 0.3,
    require_structured: bool = False,
) -> Callable[[Any], bool]:
    """Create a 3D admission filter for ``agent_memory``'s ``admission_filter``."""
    scorer = score_fn or _default_admission_scorer

    def _filter(raw: Any) -> bool:
        scores = scorer(raw)
        if scores.persistence < persistence_threshold:
            return False
        if scores.personal_value < personal_value_threshold:
            return False
        return not (require_structured and scores.structure <= 0)

    return _filter


# ---------------------------------------------------------------------------
# Memory Tiers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrievalEntry:
    """A single entry in a retrieval result, with causal trace metadata."""

    key: str
    value: Any
    score: float
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    """Causal trace for a retrieval run."""

    vector_candidates: tuple[Any, ...]
    graph_expanded: tuple[str, ...]
    ranked: tuple[RetrievalEntry, ...]
    packed: tuple[RetrievalEntry, ...]


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """A retrieval query."""

    text: str | None = None
    vector: tuple[float, ...] | None = None
    entity_ids: tuple[str, ...] | None = None


_DEFAULT_DECAY_RATE = math.log(2) / (7 * 86_400)  # 7-day half-life


# ---------------------------------------------------------------------------
# agent_memory
# ---------------------------------------------------------------------------


class AgentMemoryGraph(Graph):
    """Pre-wired agentic memory graph.

    Composes ``distill()`` with optional ``knowledge_graph()``,
    ``vector_index()``, ``light_collection()`` (permanent tier),
    ``decay()``, and ``auto_checkpoint()`` (archive tier). Supports 3D
    admission scoring, a default retrieval pipeline, periodic reflection,
    and retrieval observability traces.
    """

    __slots__ = (
        "_keepalive_subs",
        "compact",
        "distill_bundle",
        "kg",
        "memory_tiers",
        "retrieval",
        "retrieval_trace",
        "retrieve",
        "size_node",
        "vectors",
    )

    def __init__(  # noqa: C901, PLR0912, PLR0915
        self,
        name: str,
        source: Any,
        *,
        score: Callable[[Any, Any], float],
        cost: Callable[[Any], float],
        adapter: LLMAdapter | None = None,
        extract_prompt: str | None = None,
        extract_fn: Callable[[Any, Mapping[str, Any]], Any] | None = None,
        consolidate_prompt: str | None = None,
        consolidate_fn: Callable[[Mapping[str, Any]], Any] | None = None,
        consolidate_trigger: Any | None = None,
        budget: float = 2000,
        context: Any | None = None,
        admission_filter: Callable[[Any], bool] | None = None,
        # New: in-factory composition
        vector_dimensions: int | None = None,
        embed_fn: Callable[[Any], tuple[float, ...] | None] | None = None,
        enable_knowledge_graph: bool = False,
        entity_fn: Callable[[str, Any], dict[str, Any] | None] | None = None,
        tiers: dict[str, Any] | None = None,
        retrieval_opts: dict[str, Any] | None = None,
        reflection: dict[str, Any] | None = None,
        opts: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(name, opts)
        self._keepalive_subs: list[Callable[[], None]] = []

        # --- Extract function resolution ---
        raw_extract: Callable[[Any, Mapping[str, Any]], Any]
        if extract_fn is not None:
            raw_extract = extract_fn
        elif adapter is not None and extract_prompt is not None:
            raw_extract = llm_extractor(extract_prompt, adapter=adapter)
        else:
            msg = "agent_memory: provide either extract_fn or adapter + extract_prompt"
            raise ValueError(msg)

        def resolved_extract(raw: Any, existing: Mapping[str, Any]) -> Any:
            if raw is None:
                return {"upsert": []}
            return raw_extract(raw, existing)

        # --- Admission filter ---
        filtered_source = source
        if admission_filter is not None:
            src_node = from_any(source)
            filt = admission_filter

            def _filter(deps: list[Any], _actions: Any) -> Any:
                raw = deps[0]
                return raw if filt(raw) else None

            filtered_source = derived([src_node], _filter, name="admissionFilter")

        # --- Consolidation ---
        resolved_consolidate: Callable[[Mapping[str, Any]], Any] | None = None
        if consolidate_fn is not None:
            resolved_consolidate = consolidate_fn
        elif adapter is not None and consolidate_prompt is not None:
            resolved_consolidate = llm_consolidator(consolidate_prompt, adapter=adapter)

        # --- Reflection: default consolidate_trigger from from_timer ---
        effective_trigger = consolidate_trigger
        if (
            effective_trigger is None
            and resolved_consolidate is not None
            and (reflection is None or reflection.get("enabled", True))
        ):
            interval_s = (reflection or {}).get("interval", 300.0)
            effective_trigger = from_timer(interval_s, period=interval_s)

        # --- Build distill bundle ---
        bundle = distill(
            filtered_source,
            resolved_extract,
            score=score,
            cost=cost,
            budget=budget,
            context=context,
            consolidate=resolved_consolidate,
            consolidate_trigger=effective_trigger,
        )

        self.distill_bundle = bundle
        self.compact = bundle.compact
        self.size_node = bundle.size

        self.add("store", bundle.store.data)
        self.add("compact", bundle.compact)
        self.add("size", bundle.size)
        self.connect("store", "compact")
        self.connect("store", "size")

        # --- Vector index (optional) ---
        self.vectors: VectorIndex[Any] | None = None
        if vector_dimensions and vector_dimensions > 0 and embed_fn is not None:
            self.vectors = vector_index(dimension=vector_dimensions)
            self.add("vectorIndex", self.vectors.entries)

        # --- Knowledge graph (optional) ---
        self.kg: KnowledgeGraph[Any, str] | None = None
        if enable_knowledge_graph:
            self.kg = knowledge_graph(f"{name}-kg")
            self.mount("kg", self.kg)

        # --- 3-tier storage (optional) ---
        self.memory_tiers: dict[str, Any] | None = None
        if tiers is not None:
            decay_rate = tiers.get("decay_rate", _DEFAULT_DECAY_RATE)
            max_active = tiers.get("max_active", 1000)
            archive_threshold = tiers.get("archive_threshold", 0.1)
            permanent_filter_fn = tiers.get("permanent_filter", lambda _k, _m: False)

            permanent = light_collection(name="permanent")
            self.add("permanent", permanent.entries)
            permanent_keys: set[str] = set()

            def _tier_of(key: str) -> str:
                if key in permanent_keys:
                    return "permanent"
                snap = bundle.store.data.get()
                store_map = _extract_store_map(snap)
                return "active" if key in store_map else "archived"

            def _mark_permanent(key: str, value: Any) -> None:
                permanent_keys.add(key)
                permanent.upsert(key, value)

            store_node = bundle.store.data
            ctx_node = from_any(context) if context is not None else state(None)

            # Track entry creation times for accurate decay age calculation
            entry_created_at_ns: dict[str, int] = {}

            def _classify_tiers(deps: list[Any], _actions: Any) -> None:
                snap = deps[0]
                ctx = deps[1]
                store_map = _extract_store_map(snap)
                now_ns = monotonic_ns()
                to_archive: list[str] = []
                to_permanent: list[tuple[str, Any]] = []

                for key, mem in store_map.items():
                    # Track creation time for new entries
                    if key not in entry_created_at_ns:
                        entry_created_at_ns[key] = now_ns

                    if permanent_filter_fn(key, mem):
                        to_permanent.append((key, mem))
                        continue
                    base_score = score(mem, ctx)
                    created_ns = entry_created_at_ns.get(key, now_ns)
                    age_seconds = (now_ns - created_ns) / 1e9
                    decayed = decay(base_score, age_seconds, decay_rate)
                    if decayed < archive_threshold:
                        to_archive.append(key)

                # Clean up creation times for removed entries
                for key in list(entry_created_at_ns):
                    if key not in store_map:
                        del entry_created_at_ns[key]

                for k, v in to_permanent:
                    if k not in permanent_keys:
                        _mark_permanent(k, v)

                # Exclude permanent keys from active count
                active_count = len(store_map) - len(permanent_keys)
                if active_count > max_active:
                    scored = sorted(
                        (
                            (k, score(m, ctx))
                            for k, m in store_map.items()
                            if k not in permanent_keys
                        ),
                        key=lambda x: x[1],
                    )
                    excess = active_count - max_active
                    for i in range(min(excess, len(scored))):
                        sk = scored[i][0]
                        if sk not in to_archive:
                            to_archive.append(sk)

                if to_archive:
                    with batch():
                        for key in to_archive:
                            bundle.store.delete(key)

            tier_eff = effect([store_node, ctx_node], _classify_tiers)
            self._keepalive_subs.append(tier_eff.subscribe(lambda _msgs: None))

            archive_handle = None
            if "archive_adapter" in tiers:
                archive_handle = self.auto_checkpoint(
                    tiers["archive_adapter"],
                    **(tiers.get("archive_checkpoint_options") or {}),
                )

            self.memory_tiers = {
                "permanent": permanent,
                "tier_of": _tier_of,
                "mark_permanent": _mark_permanent,
                "archive_handle": archive_handle,
            }

        # --- Post-extraction hooks: vector + KG indexing ---
        if self.vectors or self.kg:
            _embed_fn = embed_fn
            _entity_fn = entity_fn
            _vectors = self.vectors
            _kg = self.kg
            store_node = bundle.store.data

            def _index(deps: list[Any], _actions: Any) -> None:
                snap = deps[0]
                store_map = _extract_store_map(snap)
                for key, mem in store_map.items():
                    if _vectors and _embed_fn:
                        vec = _embed_fn(mem)
                        if vec is not None:
                            _vectors.upsert(key, list(vec), mem)
                    if _kg and _entity_fn:
                        extracted = _entity_fn(key, mem)
                        if extracted:
                            for ent in extracted.get("entities", []):
                                _kg.upsert_entity(ent["id"], ent["value"])
                            for rel in extracted.get("relations", []):
                                _kg.link(
                                    rel["from"], rel["to"], rel["relation"], rel.get("weight", 1.0)
                                )

            idx_eff = effect([store_node], _index)
            self._keepalive_subs.append(idx_eff.subscribe(lambda _msgs: None))

        # --- Retrieval pipeline (optional) ---
        self.retrieval: Node[Any] | None = None
        self.retrieval_trace: Node[Any] | None = None
        self.retrieve: Callable[[RetrievalQuery], tuple[RetrievalEntry, ...]] | None = None

        if self.vectors or self.kg:
            top_k = (retrieval_opts or {}).get("top_k", 20)
            graph_depth = (retrieval_opts or {}).get("graph_depth", 1)
            r_budget = budget
            r_cost = cost
            r_score = score
            _vectors = self.vectors
            _kg = self.kg

            query_input: Node[Any] = state(None, name="retrievalQuery")
            self.add("retrievalQuery", query_input)

            ctx_node = from_any(context) if context is not None else state(None)
            trace_state: Node[Any] = state(None, name="retrievalTrace")
            self.add("retrievalTrace", trace_state)
            self.retrieval_trace = trace_state

            store_node = bundle.store.data

            # Last trace captured during retrieval (no side-effect in derived)
            last_trace: list[RetrievalTrace | None] = [None]

            def _retrieve(deps: list[Any], _actions: Any) -> Any:
                query = deps[0]
                snap = deps[1]
                ctx = deps[2]
                if query is None:
                    return ()
                q: RetrievalQuery = query
                store_map = _extract_store_map(snap)

                candidate_map: dict[str, tuple[Any, set[str]]] = {}

                # Stage 1: Vector search
                vector_candidates: list[Any] = []
                if _vectors and q.vector is not None:
                    vector_candidates = list(_vectors.search(list(q.vector), top_k))
                    for vc in vector_candidates:
                        mem = store_map.get(vc.id)
                        if mem is not None:
                            candidate_map[vc.id] = (mem, {"vector"})

                # Stage 2: KG expansion
                graph_expanded: list[str] = []
                if _kg:
                    seed_ids = list(q.entity_ids or ()) + list(candidate_map.keys())
                    visited: set[str] = set()
                    frontier = seed_ids
                    for _depth in range(graph_depth):
                        next_frontier: list[str] = []
                        for eid in frontier:
                            if eid in visited:
                                continue
                            visited.add(eid)
                            for edge in _kg.related(eid):
                                tid = edge.to_id
                                if tid not in visited:
                                    next_frontier.append(tid)
                                    mem = store_map.get(tid)
                                    if mem is not None:
                                        if tid in candidate_map:
                                            candidate_map[tid][1].add("graph")
                                        else:
                                            candidate_map[tid] = (mem, {"graph"})
                                        graph_expanded.append(tid)
                        frontier = next_frontier

                # Include remaining store entries
                for key, mem in store_map.items():
                    if key not in candidate_map:
                        candidate_map[key] = (mem, {"store"})

                # Stage 3: Score and rank
                ranked: list[RetrievalEntry] = []
                for key, (value, sources) in candidate_map.items():
                    s = r_score(value, ctx)
                    ranked.append(
                        RetrievalEntry(key=key, value=value, score=s, sources=tuple(sources))
                    )
                ranked.sort(key=lambda e: e.score, reverse=True)

                # Stage 4: Budget packing
                packed: list[RetrievalEntry] = []
                used_budget = 0.0
                for entry in ranked:
                    c = r_cost(entry.value)
                    if used_budget + c > r_budget and packed:
                        break
                    packed.append(entry)
                    used_budget += c

                # Capture trace (no side-effect — stored for retrieval by _do_retrieve)
                last_trace[0] = RetrievalTrace(
                    vector_candidates=tuple(vector_candidates),
                    graph_expanded=tuple(graph_expanded),
                    ranked=tuple(ranked),
                    packed=tuple(packed),
                )

                return tuple(packed)

            retrieval_derived = derived(
                [query_input, store_node, ctx_node],
                _retrieve,
                name="retrieval",
                initial=(),
            )
            self.add("retrieval", retrieval_derived)
            self.connect("retrievalQuery", "retrieval")
            self.connect("store", "retrieval")
            self._keepalive_subs.append(retrieval_derived.subscribe(lambda _msgs: None))
            self.retrieval = retrieval_derived

            def _do_retrieve(query: RetrievalQuery) -> tuple[RetrievalEntry, ...]:
                query_input.down([(MessageType.DATA, query)])
                result = retrieval_derived.get() or ()
                # Update trace node outside derived callback (avoids reactive glitch)
                if last_trace[0] is not None:
                    trace_state.down([(MessageType.DATA, last_trace[0])])
                return result

            self.retrieve = _do_retrieve

    def destroy(self) -> None:
        for unsub in self._keepalive_subs:
            unsub()
        self._keepalive_subs.clear()
        super().destroy()


def _extract_store_map(snap: Any) -> dict[str, Any]:
    """Extract the key→value mapping from a reactive_map snapshot."""
    if snap is None:
        return {}
    if hasattr(snap, "value") and hasattr(snap.value, "map"):
        m = snap.value.map
        return dict(m) if m is not None else {}
    if isinstance(snap, dict):
        return snap
    return {}


def agent_memory(
    name: str,
    source: Any,
    *,
    score: Callable[[Any, Any], float],
    cost: Callable[[Any], float],
    adapter: LLMAdapter | None = None,
    extract_prompt: str | None = None,
    extract_fn: Callable[[Any, Mapping[str, Any]], Any] | None = None,
    consolidate_prompt: str | None = None,
    consolidate_fn: Callable[[Mapping[str, Any]], Any] | None = None,
    consolidate_trigger: Any | None = None,
    budget: float = 2000,
    context: Any | None = None,
    admission_filter: Callable[[Any], bool] | None = None,
    vector_dimensions: int | None = None,
    embed_fn: Callable[[Any], tuple[float, ...] | None] | None = None,
    enable_knowledge_graph: bool = False,
    entity_fn: Callable[[str, Any], dict[str, Any] | None] | None = None,
    tiers: dict[str, Any] | None = None,
    retrieval_opts: dict[str, Any] | None = None,
    reflection: dict[str, Any] | None = None,
    opts: dict[str, Any] | None = None,
) -> AgentMemoryGraph:
    """Pre-wired agentic memory graph factory."""
    return AgentMemoryGraph(
        name,
        source,
        score=score,
        cost=cost,
        adapter=adapter,
        extract_prompt=extract_prompt,
        extract_fn=extract_fn,
        consolidate_prompt=consolidate_prompt,
        consolidate_fn=consolidate_fn,
        consolidate_trigger=consolidate_trigger,
        budget=budget,
        context=context,
        admission_filter=admission_filter,
        vector_dimensions=vector_dimensions,
        embed_fn=embed_fn,
        enable_knowledge_graph=enable_knowledge_graph,
        entity_fn=entity_fn,
        tiers=tiers,
        retrieval_opts=retrieval_opts,
        reflection=reflection,
        opts=opts,
    )


# ---------------------------------------------------------------------------
# agent_loop
# ---------------------------------------------------------------------------


class AgentLoopGraph(Graph):
    """LLM reasoning loop: think → act → observe → repeat."""

    __slots__ = (
        "_abort_event",
        "_adapter",
        "_max_tokens",
        "_max_turns",
        "_model",
        "_on_tool_call",
        "_running",
        "_status_state",
        "_stop_when",
        "_system_prompt",
        "_temperature",
        "_turn_count_state",
        "chat",
        "last_response",
        "status",
        "tools",
        "turn_count",
    )

    def __init__(
        self,
        name: str,
        *,
        adapter: LLMAdapter,
        tools: Sequence[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_turns: int = 10,
        stop_when: Callable[[LLMResponse], bool] | None = None,
        on_tool_call: Callable[[ToolCall], None] | None = None,
        max_messages: int | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        opts: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(name, opts)
        self._adapter = adapter
        self._max_turns = max_turns
        self._stop_when = stop_when
        self._on_tool_call = on_tool_call
        self._system_prompt = system_prompt if isinstance(system_prompt, str) else None
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._running = False
        self._abort_event: threading.Event | None = None

        # Mount chat subgraph
        self.chat = chat_stream(f"{name}-chat", max_messages=max_messages)
        self.mount("chat", self.chat)

        # Mount tool registry subgraph
        self.tools = tool_registry(f"{name}-tools")
        self.mount("tools", self.tools)

        # Register initial tools
        if tools:
            for tool in tools:
                self.tools.register(tool)

        # Status state
        self._status_state: NodeImpl[str] = state(
            "idle",
            name="status",
            describe_kind="state",
            meta=_ai_meta("agent_status"),
        )
        self.status = self._status_state
        self.add("status", self.status)

        # Turn count
        self._turn_count_state: NodeImpl[int] = state(
            0,
            name="turnCount",
            describe_kind="state",
            meta=_ai_meta("agent_turn_count"),
        )
        self.turn_count = self._turn_count_state
        self.add("turnCount", self.turn_count)

        # Last response
        self.last_response: NodeImpl[LLMResponse | None] = state(
            None,
            name="lastResponse",
            describe_kind="state",
            meta=_ai_meta("agent_last_response"),
        )
        self.add("lastResponse", self.last_response)

    def run(self, user_message: str) -> LLMResponse | None:
        """Start the agent loop with a user message.

        The loop runs: think (LLM call) → act (tool execution) → repeat
        until done. Returns the final LLM response.

        Messages accumulate across calls. Call ``chat.clear()`` before
        ``run()`` to reset conversation history.
        """
        if self._running:
            msg = "agent_loop: already running"
            raise RuntimeError(msg)
        self._running = True
        self._abort_event = threading.Event()

        with batch():
            self._status_state.down([(MessageType.DATA, "idle")])
            self._turn_count_state.down([(MessageType.DATA, 0)])
        self.chat.append("user", user_message)

        try:
            turns = 0
            while turns < self._max_turns:
                if self._abort_event.is_set():
                    msg = "agent_loop: aborted"
                    raise RuntimeError(msg)
                turns += 1
                with batch():
                    self._turn_count_state.down([(MessageType.DATA, turns)])
                    self._status_state.down([(MessageType.DATA, "thinking")])

                # Invoke LLM
                msgs = self.chat.all_messages()
                tool_schemas = self.tools.schemas.get() or ()
                response = self._invoke_llm(msgs, tool_schemas)
                if self._abort_event.is_set():
                    msg = "agent_loop: aborted"
                    raise RuntimeError(msg)

                self.last_response.down([(MessageType.DATA, response)])

                # Append assistant message
                self.chat.append(
                    "assistant",
                    response.content,
                    tool_calls=response.tool_calls,
                )

                # Check stop conditions
                if self._should_stop(response):
                    self._status_state.down([(MessageType.DATA, "done")])
                    self._running = False
                    self._abort_event = None
                    return response

                # Execute tool calls if present
                if response.tool_calls:
                    self._status_state.down([(MessageType.DATA, "acting")])
                    for call in response.tool_calls:
                        if self._abort_event.is_set():
                            msg = "agent_loop: aborted"
                            raise RuntimeError(msg)
                        if self._on_tool_call:
                            self._on_tool_call(call)
                        try:
                            result = self.tools.execute(call.name, call.arguments)
                            self.chat.append_tool_result(call.id, json.dumps(result, default=str))
                        except Exception as err:
                            self.chat.append_tool_result(call.id, json.dumps({"error": str(err)}))
                else:
                    # No tool calls and not explicitly stopped → done
                    self._status_state.down([(MessageType.DATA, "done")])
                    self._running = False
                    self._abort_event = None
                    return response

            # Max turns reached
            self._status_state.down([(MessageType.DATA, "done")])
            self._running = False
            self._abort_event = None
            return self.last_response.get()
        except Exception:
            self._status_state.down([(MessageType.DATA, "error")])
            self._running = False
            self._abort_event = None
            raise

    def _invoke_llm(
        self,
        msgs: tuple[ChatMessage, ...],
        tools: tuple[ToolDefinition, ...] | Any,
    ) -> LLMResponse:
        result = self._adapter.invoke(
            list(msgs),
            LLMInvokeOptions(
                tools=tuple(tools) if tools else None,
                system_prompt=self._system_prompt,
                model=self._model,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            ),
        )
        # Guard: None/null — reject before from_any
        if result is None:
            msg = "_invoke_llm: adapter.invoke() returned None"
            raise RuntimeError(msg)
        # Guard: str — from_any would iterate characters
        if isinstance(result, str):
            msg = "_invoke_llm: adapter.invoke() returned a string, expected LLMResponse"
            raise RuntimeError(msg)
        # Guard: dict/Mapping — iterating a dict yields keys, not values;
        # normalize to LLMResponse if it has a 'content' key.
        if isinstance(result, dict):
            if "content" in result:
                return LLMResponse(
                    content=result.get("content", ""),
                    tool_calls=result.get("tool_calls"),
                    usage=result.get("usage"),
                    finish_reason=result.get("finish_reason"),
                    metadata=result.get("metadata"),
                )
            msg = "_invoke_llm: adapter.invoke() returned a dict without 'content' key"
            raise RuntimeError(msg)
        if isinstance(result, LLMResponse):
            return result
        resolved = from_any(result)
        val = resolved.get()
        if isinstance(val, LLMResponse):
            return val
        try:
            first = first_value_from(resolved)
        except StopIteration as err:
            msg = "agent_loop: adapter completed without producing an LLMResponse"
            raise RuntimeError(msg) from err
        if not isinstance(first, LLMResponse):
            msg = f"agent_loop: expected LLMResponse, got {type(first).__name__}"
            raise TypeError(msg)
        return first

    def _should_stop(self, response: LLMResponse) -> bool:
        if response.finish_reason == "end_turn" and not response.tool_calls:
            return True
        return bool(self._stop_when and self._stop_when(response))

    def destroy(self) -> None:
        if self._abort_event is not None:
            self._abort_event.set()
            self._abort_event = None
        self._running = False
        super().destroy()


def agent_loop(
    name: str,
    *,
    adapter: LLMAdapter,
    tools: Sequence[ToolDefinition] | None = None,
    system_prompt: str | None = None,
    max_turns: int = 10,
    stop_when: Callable[[LLMResponse], bool] | None = None,
    on_tool_call: Callable[[ToolCall], None] | None = None,
    max_messages: int | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    opts: dict[str, Any] | None = None,
) -> AgentLoopGraph:
    """Create an agent loop graph."""
    return AgentLoopGraph(
        name,
        adapter=adapter,
        tools=tools,
        system_prompt=system_prompt,
        max_turns=max_turns,
        stop_when=stop_when,
        on_tool_call=on_tool_call,
        max_messages=max_messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        opts=opts,
    )


# ---------------------------------------------------------------------------
# 5.4 — LLM tool integration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenAIToolSchema:
    """OpenAI function-calling tool schema."""

    type: str  # always "function"
    function: dict[str, Any]


@dataclass(frozen=True, slots=True)
class McpToolSchema:
    """MCP (Model Context Protocol) tool schema."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class KnobsAsToolsResult:
    """Result of :func:`knobs_as_tools`."""

    openai: tuple[OpenAIToolSchema, ...]
    mcp: tuple[McpToolSchema, ...]
    definitions: tuple[ToolDefinition, ...]


def _meta_to_json_schema(meta: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON Schema ``value`` descriptor from a node's meta fields."""
    schema: dict[str, Any] = {}

    meta_type = meta.get("type")
    if meta_type == "enum" and isinstance(meta.get("values"), (list, tuple)):
        schema["type"] = "string"
        schema["enum"] = list(meta["values"])
    elif meta_type == "integer":
        schema["type"] = "integer"
    elif meta_type == "number":
        schema["type"] = "number"
    elif meta_type == "boolean":
        schema["type"] = "boolean"
    elif meta_type == "string":
        schema["type"] = "string"
    else:
        schema["type"] = ["string", "number", "boolean"]

    rng = meta.get("range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        schema["minimum"] = rng[0]
        schema["maximum"] = rng[1]

    fmt = meta.get("format")
    if isinstance(fmt, str):
        schema["description"] = f"Format: {fmt}"

    unit = meta.get("unit")
    if isinstance(unit, str):
        if "description" in schema:
            schema["description"] += f" ({unit})"
        else:
            schema["description"] = f"Unit: {unit}"

    return schema


def knobs_as_tools(
    graph: Graph,
    actor: Any | None = None,
) -> KnobsAsToolsResult:
    """Derive tool schemas from a graph's writable (knob) nodes.

    Knobs are state nodes whose ``meta.access`` is ``"llm"``, ``"both"``, or
    absent (default: writable). Each knob becomes a tool that calls
    ``graph.set()``.

    Speaks **domain language** (spec §5.4): the returned schemas use node names
    and meta descriptions — no protocol internals exposed.

    Args:
        graph: The graph to introspect.
        actor: Optional actor for guard-scoped describe.

    Returns:
        OpenAI, MCP, and GraphReFly tool schemas.
    """
    kwargs: dict[str, Any] = {}
    if actor is not None:
        kwargs["actor"] = actor
    described = graph.describe(**kwargs)

    openai_list: list[OpenAIToolSchema] = []
    mcp_list: list[McpToolSchema] = []
    definitions_list: list[ToolDefinition] = []

    for path, node_desc in described["nodes"].items():
        if node_desc.get("type") != "state":
            continue
        if "::__meta__::" in path:
            continue

        # Skip terminal-state nodes (§1.3.4)
        status = node_desc.get("status")
        if status in ("completed", "errored"):
            continue

        meta = node_desc.get("meta", {})
        access = meta.get("access")
        if access in ("human", "system"):
            continue

        description = meta.get("description") or f"Set the value of {path}"
        value_schema = _meta_to_json_schema(meta)

        parameter_schema: dict[str, Any] = {
            "type": "object",
            "required": ["value"],
            "properties": {"value": value_schema},
            "additionalProperties": False,
        }

        # OpenAI requires [a-zA-Z0-9_-] in function names
        sanitized_name = path.replace("::", "__")

        openai_list.append(
            OpenAIToolSchema(
                type="function",
                function={
                    "name": sanitized_name,
                    "description": description,
                    "parameters": parameter_schema,
                },
            )
        )

        mcp_list.append(
            McpToolSchema(
                name=path,
                description=description,
                input_schema=parameter_schema,
            )
        )

        # Capture for closure
        _graph = graph
        _path = path
        _actor = actor

        def _make_handler(g: Graph, p: str, a: Any | None) -> Callable[[dict[str, Any]], Any]:
            def handler(args: dict[str, Any]) -> Any:
                kwargs: dict[str, Any] = {}
                if a is not None:
                    kwargs["actor"] = a
                g.set(p, args["value"], **kwargs)
                return args["value"]

            return handler

        nv = node_desc.get("v")
        definitions_list.append(
            ToolDefinition(
                name=path,
                description=description,
                parameters=parameter_schema,
                handler=_make_handler(_graph, _path, _actor),
                version={"id": nv["id"], "version": nv["version"]} if nv is not None else None,
            )
        )

    return KnobsAsToolsResult(
        openai=tuple(openai_list),
        mcp=tuple(mcp_list),
        definitions=tuple(definitions_list),
    )


def gauges_as_context(
    graph: Graph,
    actor: Any | None = None,
    *,
    group_by_tags: bool = True,
    separator: str = "\n",
    since_version: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Format a graph's readable (gauge) nodes as a context string for LLM
    system prompts.

    Gauges are nodes with ``meta.description`` or ``meta.format``. Values are
    formatted using ``meta.format`` and ``meta.unit`` hints.

    Args:
        graph: The graph to introspect.
        actor: Optional actor for guard-scoped describe.
        group_by_tags: Group gauges by ``meta.tags`` (default ``True``).
        separator: Separator between gauge lines (default ``"\\n"``).
        since_version: V0 delta mode (§6.0b): map of ``path → {"id", "version"}``.
            Only include nodes whose ``v.version`` exceeds the stored version
            AND whose ``v.id`` matches. Nodes without V0, not in the map, or
            with a different id (replacement) are always included.

    Returns:
        A formatted string ready for system prompt injection.
    """
    kwargs: dict[str, Any] = {}
    if actor is not None:
        kwargs["actor"] = actor
    described = graph.describe(**kwargs)

    entries: list[tuple[str, str, str]] = []  # (path, description, formatted)

    for path, node_desc in described["nodes"].items():
        meta = node_desc.get("meta", {})
        desc = meta.get("description")
        fmt = meta.get("format")
        if not desc and not fmt:
            continue
        # V0 delta filter: skip nodes unchanged since last seen version (§6.0b).
        if since_version is not None:
            nv = node_desc.get("v")
            if nv is not None:
                last_seen = since_version.get(path)
                if (
                    last_seen is not None
                    and last_seen.get("id") == nv.get("id")
                    and nv.get("version", 0) <= last_seen.get("version", -1)
                ):
                    continue

        label = desc or path
        value = node_desc.get("value")
        unit = meta.get("unit")

        if fmt == "currency" and isinstance(value, (int, float)):
            formatted = f"${value:.2f}"
        elif fmt == "percentage" and isinstance(value, (int, float)):
            formatted = f"{value * 100:.1f}%"
        elif value is None:
            formatted = "(no value)"
        else:
            formatted = str(value)

        if unit and fmt not in ("currency", "percentage"):
            formatted = f"{formatted} {unit}"

        entries.append((path, label, formatted))

    if not entries:
        return ""

    if group_by_tags:
        tag_groups: dict[str, list[tuple[str, str, str]]] = {}
        ungrouped: list[tuple[str, str, str]] = []

        for entry in entries:
            node_desc = described["nodes"][entry[0]]
            tags = node_desc.get("meta", {}).get("tags")
            if tags and len(tags) > 0:
                # Use first tag for grouping to avoid duplicating entries
                tag_groups.setdefault(tags[0], []).append(entry)
            else:
                ungrouped.append(entry)

        if not tag_groups:
            return separator.join(f"- {e[1]}: {e[2]}" for e in entries)

        sections: list[str] = []
        for tag in sorted(tag_groups):
            group = tag_groups[tag]
            lines = separator.join(f"- {e[1]}: {e[2]}" for e in group)
            sections.append(f"[{tag}]{separator}{lines}")
        if ungrouped:
            sections.append(separator.join(f"- {e[1]}: {e[2]}" for e in ungrouped))
        return (separator + separator).join(sections)

    return separator.join(f"- {e[1]}: {e[2]}" for e in entries)


# ---------------------------------------------------------------------------
# validateGraphDef
# ---------------------------------------------------------------------------

_VALID_NODE_TYPES = frozenset({"state", "derived", "producer", "operator", "effect"})


@dataclass(frozen=True, slots=True)
class GraphDefValidation:
    """Validation result from :func:`validate_graph_def`."""

    valid: bool
    errors: tuple[str, ...]


def validate_graph_def(definition: Any) -> GraphDefValidation:
    """Validate an LLM-generated graph definition before passing to
    ``Graph.from_snapshot()``.

    Checks required fields, node types, edge references, and duplicates.

    Args:
        definition: The graph definition to validate (parsed JSON).

    Returns:
        Validation result with errors tuple.
    """
    errors: list[str] = []

    if definition is None or not isinstance(definition, dict):
        return GraphDefValidation(valid=False, errors=("Definition must be a non-null dict",))

    d: dict[str, Any] = definition

    name = d.get("name")
    if not isinstance(name, str) or len(name) == 0:
        errors.append("Missing or empty 'name' field")

    nodes = d.get("nodes")
    if nodes is None or not isinstance(nodes, dict):
        errors.append("Missing or invalid 'nodes' field (must be a dict)")
        return GraphDefValidation(valid=False, errors=tuple(errors))

    node_names = set(nodes.keys())

    for nname, raw in nodes.items():
        if raw is None or not isinstance(raw, dict):
            errors.append(f'Node "{nname}": must be a dict')
            continue
        ntype = raw.get("type")
        if not isinstance(ntype, str) or ntype not in _VALID_NODE_TYPES:
            valid_str = ", ".join(sorted(_VALID_NODE_TYPES))
            errors.append(f'Node "{nname}": invalid type "{ntype}" (expected: {valid_str})')
        deps = raw.get("deps")
        if isinstance(deps, list):
            for dep in deps:
                if isinstance(dep, str) and dep not in node_names:
                    errors.append(
                        f'Node "{nname}": dep "{dep}" does not reference an existing node'
                    )

    edges = d.get("edges")
    if edges is not None:
        if not isinstance(edges, list):
            errors.append("'edges' must be a list")
        else:
            seen: set[str] = set()
            for i, edge in enumerate(edges):
                if edge is None or not isinstance(edge, dict):
                    errors.append(f"Edge [{i}]: must be a dict")
                    continue
                efrom = edge.get("from")
                if not isinstance(efrom, str) or efrom not in node_names:
                    errors.append(
                        f"Edge [{i}]: 'from' \"{efrom}\" does not reference an existing node"
                    )
                eto = edge.get("to")
                if not isinstance(eto, str) or eto not in node_names:
                    errors.append(f"Edge [{i}]: 'to' \"{eto}\" does not reference an existing node")
                key = f"{efrom}->{eto}"
                if key in seen:
                    errors.append(f"Edge [{i}]: duplicate edge {key}")
                seen.add(key)

    return GraphDefValidation(valid=len(errors) == 0, errors=tuple(errors))


# ---------------------------------------------------------------------------
# graphFromSpec
# ---------------------------------------------------------------------------

_FENCE_PATTERN = _re.compile(r"^```(?:json)?\s*([\s\S]*?)\s*```[\s\S]*$")


def _strip_fences(text: str) -> str:
    """Strip markdown code fences, handling trailing commentary."""
    m = _FENCE_PATTERN.match(text)
    return m.group(1) if m else text


_GRAPH_FROM_SPEC_SYSTEM_PROMPT = """\
You are a graph architect for GraphReFly, a reactive graph protocol.

Given a natural-language description, produce a JSON graph definition with this structure:

{
  "name": "<graph_name>",
  "nodes": {
    "<node_name>": {
      "type": "state" | "derived" | "producer" | "operator" | "effect",
      "value": <initial_value_or_null>,
      "deps": ["<dep_node_name>", ...],
      "meta": {
        "description": "<human-readable purpose>",
        "type": "string" | "number" | "boolean" | "integer" | "enum",
        "range": [min, max],
        "values": ["a", "b"],
        "format": "currency" | "percentage" | "status",
        "access": "human" | "llm" | "both" | "system",
        "unit": "<unit>",
        "tags": ["<tag>"]
      }
    }
  },
  "edges": [
    { "from": "<source_node>", "to": "<target_node>" }
  ]
}

Rules:
- "state" nodes have no deps and hold user/LLM-writable values (knobs).
- "derived" nodes have deps and compute from them.
- "effect" nodes have deps but produce side effects (no return value).
- "producer" nodes have no deps but generate values asynchronously.
- Edges wire output of one node as input to another. They must match deps.
- meta.description is required for every node.
- Return ONLY valid JSON, no markdown fences or commentary."""


def graph_from_spec(
    natural_language: str,
    adapter: LLMAdapter,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    build: Callable[[Graph], None] | None = None,
    system_prompt_extra: str | None = None,
) -> Graph:
    """Ask an LLM to compose a Graph from a natural-language description.

    The LLM returns a JSON graph definition which is validated and then
    constructed via ``Graph.from_snapshot()``.

    Args:
        natural_language: The problem/use-case description.
        adapter: LLM adapter for the generation call.
        model: Optional model override.
        temperature: Optional temperature (default 0).
        max_tokens: Optional max tokens.
        build: Optional callback to construct topology before values are applied.
        system_prompt_extra: Extra instructions appended to the system prompt.

    Returns:
        A constructed Graph.

    Raises:
        ValueError: On invalid LLM output or validation failure.
    """
    sys_prompt = _GRAPH_FROM_SPEC_SYSTEM_PROMPT
    if system_prompt_extra:
        sys_prompt = f"{sys_prompt}\n\n{system_prompt_extra}"

    messages = [
        ChatMessage(role="system", content=sys_prompt),
        ChatMessage(role="user", content=natural_language),
    ]

    raw_result = adapter.invoke(
        messages,
        LLMInvokeOptions(
            model=model,
            temperature=temperature if temperature is not None else 0.0,
            max_tokens=max_tokens,
        ),
    )

    response = _resolve_node_input(raw_result)
    if not isinstance(response, LLMResponse):
        msg = f"graph_from_spec: expected LLMResponse, got {type(response).__name__}"
        raise ValueError(msg)

    content = response.content.strip()
    if content.startswith("```"):
        content = _strip_fences(content)

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        msg = f"graph_from_spec: LLM response is not valid JSON: {content[:200]}"
        raise ValueError(msg) from exc

    validation = validate_graph_def(parsed)
    if not validation.valid:
        detail = "\n".join(validation.errors)
        msg = f"graph_from_spec: invalid graph definition:\n{detail}"
        raise ValueError(msg)

    # Ensure version and subgraphs fields for from_snapshot
    if "version" not in parsed:
        parsed["version"] = 1
    if "subgraphs" not in parsed or not isinstance(parsed["subgraphs"], list):
        parsed["subgraphs"] = []

    return Graph.from_snapshot(parsed, build)


# ---------------------------------------------------------------------------
# suggestStrategy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategyOperation:
    """A single operation in a strategy plan."""

    type: str
    name: str | None = None
    node_type: str | None = None
    meta: dict[str, Any] | None = None
    initial: Any = None
    from_node: str | None = None
    to_node: str | None = None
    value: Any = None
    key: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyPlan:
    """Structured strategy plan returned by :func:`suggest_strategy`."""

    summary: str
    operations: tuple[StrategyOperation, ...]
    reasoning: str


_SUGGEST_STRATEGY_SYSTEM_PROMPT = """\
You are a reactive graph optimizer for GraphReFly.

Given a graph's current structure (from describe()) and a problem statement, \
suggest topology and parameter changes to solve the problem.

Return ONLY valid JSON with this structure:
{
  "summary": "<one-line summary of the strategy>",
  "reasoning": "<explanation of why these changes help>",
  "operations": [
    { "type": "add_node", "name": "<name>",
      "nodeType": "state|derived|effect|producer|operator",
      "meta": {...}, "initial": <value> },
    { "type": "remove_node", "name": "<name>" },
    { "type": "connect", "from": "<source>", "to": "<target>" },
    { "type": "disconnect", "from": "<source>", "to": "<target>" },
    { "type": "set_value", "name": "<name>", "value": <new_value> },
    { "type": "update_meta", "name": "<name>",
      "key": "<meta_key>", "value": <new_value> }
  ]
}

Rules:
- Only suggest operations that reference existing nodes
  (for remove/disconnect/set_value/update_meta)
  or new nodes you define (for add_node).
- Keep changes minimal.
- Return ONLY valid JSON, no markdown fences or commentary."""


def _parse_strategy_operation(raw: dict[str, Any]) -> StrategyOperation:
    """Parse a raw operation dict into a StrategyOperation."""
    return StrategyOperation(
        type=raw.get("type", ""),
        name=raw.get("name"),
        node_type=raw.get("nodeType"),
        meta=raw.get("meta"),
        initial=raw.get("initial"),
        from_node=raw.get("from"),
        to_node=raw.get("to"),
        value=raw.get("value"),
        key=raw.get("key"),
    )


def suggest_strategy(
    graph: Graph,
    problem: str,
    adapter: LLMAdapter,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    actor: Any | None = None,
) -> StrategyPlan:
    """Ask an LLM to analyze a graph and suggest topology/parameter changes
    to solve a stated problem.

    Returns a structured plan — does NOT auto-apply. The caller reviews
    and selectively applies operations.

    Args:
        graph: The graph to analyze.
        problem: Natural-language problem statement.
        adapter: LLM adapter for the analysis call.
        model: Optional model override.
        temperature: Optional temperature (default 0).
        max_tokens: Optional max tokens.
        actor: Optional actor for guard-scoped describe.

    Returns:
        A structured strategy plan.

    Raises:
        ValueError: On invalid LLM output.
    """
    kwargs: dict[str, Any] = {}
    if actor is not None:
        kwargs["actor"] = actor
    described = graph.describe(**kwargs)

    messages = [
        ChatMessage(role="system", content=_SUGGEST_STRATEGY_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=json.dumps({"graph": described, "problem": problem}),
        ),
    ]

    raw_result = adapter.invoke(
        messages,
        LLMInvokeOptions(
            model=model,
            temperature=temperature if temperature is not None else 0.0,
            max_tokens=max_tokens,
        ),
    )

    response = _resolve_node_input(raw_result)
    if not isinstance(response, LLMResponse):
        msg = f"suggest_strategy: expected LLMResponse, got {type(response).__name__}"
        raise ValueError(msg)

    content = response.content.strip()
    if content.startswith("```"):
        content = _strip_fences(content)

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        msg = f"suggest_strategy: LLM response is not valid JSON: {content[:200]}"
        raise ValueError(msg) from exc

    if not isinstance(parsed, dict):
        msg = "suggest_strategy: expected a JSON object"
        raise ValueError(msg)

    summary = parsed.get("summary")
    if not isinstance(summary, str):
        msg = "suggest_strategy: missing 'summary' in response"
        raise ValueError(msg)

    reasoning = parsed.get("reasoning")
    if not isinstance(reasoning, str):
        msg = "suggest_strategy: missing 'reasoning' in response"
        raise ValueError(msg)

    ops_raw = parsed.get("operations")
    if not isinstance(ops_raw, list):
        msg = "suggest_strategy: missing 'operations' list in response"
        raise ValueError(msg)

    operations = tuple(_parse_strategy_operation(op) for op in ops_raw if isinstance(op, dict))

    return StrategyPlan(
        summary=summary,
        operations=operations,
        reasoning=reasoning,
    )
