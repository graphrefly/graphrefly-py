"""AI surface pattern tests (roadmap §4.4)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import state
from graphrefly.extra.composite import Extraction
from graphrefly.graph.graph import Graph
from graphrefly.patterns.ai import (
    AdmissionScores,
    AgentLoopGraph,
    ChatMessage,
    ChatStreamGraph,
    CostMeterReading,
    ExtractedToolCall,
    KeywordFlag,
    KnobsAsToolsResult,
    LLMInvokeOptions,
    LLMResponse,
    RetrievalQuery,
    StrategyPlan,
    StreamChunk,
    StreamingPromptNodeHandle,
    ToolCall,
    ToolDefinition,
    ToolRegistryGraph,
    admission_filter_3d,
    agent_loop,
    agent_memory,
    chat_stream,
    cost_meter_extractor,
    from_llm,
    gated_stream,
    gauges_as_context,
    graph_from_spec,
    keyword_flag_extractor,
    knobs_as_tools,
    llm_consolidator,
    llm_extractor,
    prompt_node,
    stream_extractor,
    streaming_prompt_node,
    suggest_strategy,
    system_prompt_builder,
    tool_call_extractor,
    tool_registry,
    validate_graph_def,
)

# ---------------------------------------------------------------------------
# Mock LLM adapter
# ---------------------------------------------------------------------------


class MockAdapter:
    """Simple mock adapter that returns canned responses in sequence."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self._idx = 0

    def invoke(
        self,
        messages: list[ChatMessage],
        opts: LLMInvokeOptions | None = None,
    ) -> LLMResponse:
        resp = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return resp


# ---------------------------------------------------------------------------
# chat_stream
# ---------------------------------------------------------------------------


def test_chat_stream_creates_graph() -> None:
    cs = chat_stream("test-chat")
    assert isinstance(cs, ChatStreamGraph)
    assert cs.get("messageCount") == 0
    assert cs.get("latest") is None


def test_chat_stream_append_updates_derived() -> None:
    cs = chat_stream("test-chat")
    cs.append("user", "hello")
    cs.append("assistant", "hi there")

    assert cs.get("messageCount") == 2
    latest = cs.get("latest")
    assert isinstance(latest, ChatMessage)
    assert latest.role == "assistant"
    assert latest.content == "hi there"


def test_chat_stream_append_tool_result() -> None:
    cs = chat_stream("test-chat")
    cs.append_tool_result("call-1", '{"result": 42}')

    msgs = cs.all_messages()
    assert len(msgs) == 1
    assert msgs[0].role == "tool"
    assert msgs[0].tool_call_id == "call-1"


def test_chat_stream_clear() -> None:
    cs = chat_stream("test-chat")
    cs.append("user", "test")
    cs.clear()
    assert cs.get("messageCount") == 0
    assert len(cs.all_messages()) == 0


def test_chat_stream_describe() -> None:
    cs = chat_stream("test-chat")
    desc = cs.describe()
    assert "messages" in desc["nodes"]
    assert "latest" in desc["nodes"]
    assert "messageCount" in desc["nodes"]


# ---------------------------------------------------------------------------
# tool_registry
# ---------------------------------------------------------------------------


def test_tool_registry_creates_graph() -> None:
    tr = tool_registry("test-tools")
    assert isinstance(tr, ToolRegistryGraph)
    assert tr.schemas.get() == ()


def test_tool_registry_register_unregister() -> None:
    tr = tool_registry("test-tools")
    tool = ToolDefinition(
        name="add",
        description="Adds two numbers",
        parameters={"type": "object"},
        handler=lambda args: args["a"] + args["b"],
    )
    tr.register(tool)
    assert tr.get_definition("add") is not None

    schemas = tr.schemas.get()
    assert len(schemas) == 1
    assert schemas[0].name == "add"

    tr.unregister("add")
    assert tr.get_definition("add") is None
    assert len(tr.schemas.get()) == 0


def test_tool_registry_execute() -> None:
    tr = tool_registry("test-tools")
    tr.register(
        ToolDefinition(
            name="greet",
            description="Greet",
            parameters={},
            handler=lambda args: f"Hello, {args['name']}!",
        )
    )
    result = tr.execute("greet", {"name": "world"})
    assert result == "Hello, world!"


def test_tool_registry_execute_unknown_raises() -> None:
    tr = tool_registry("test-tools")
    try:
        tr.execute("missing", {})
        assert False, "Should have raised"  # noqa: B011
    except ValueError as e:
        assert "unknown tool" in str(e)


def test_tool_registry_execute_awaitable_handler() -> None:
    tr = tool_registry("test-tools")

    async def _double() -> int:
        return 84

    tr.register(
        ToolDefinition(
            name="async_val",
            description="coroutine",
            parameters={},
            handler=lambda _args: _double(),
        )
    )
    assert tr.execute("async_val", {}) == 84


def test_tool_registry_execute_node_handler() -> None:
    tr = tool_registry("test-tools")
    n = state(42)
    tr.register(
        ToolDefinition(
            name="node_val",
            description="node",
            parameters={},
            handler=lambda _args: n,
        )
    )
    assert tr.execute("node_val", {}) == 42


# ---------------------------------------------------------------------------
# system_prompt_builder
# ---------------------------------------------------------------------------


def test_system_prompt_builder_assembles_sections() -> None:
    prompt = system_prompt_builder(
        [
            "You are a helpful assistant.",
            "Be concise.",
        ]
    )
    assert prompt.get() == "You are a helpful assistant.\n\nBe concise."


def test_system_prompt_builder_reacts_to_changes() -> None:
    role = state("You are an assistant.")
    prompt = system_prompt_builder([role, "Be concise."])
    assert prompt.get() == "You are an assistant.\n\nBe concise."

    role.down([(MessageType.DATA, "You are a coding expert.")])
    assert prompt.get() == "You are a coding expert.\n\nBe concise."


def test_system_prompt_builder_filters_empty() -> None:
    prompt = system_prompt_builder(["hello", "", "world"])
    assert prompt.get() == "hello\n\nworld"


def test_system_prompt_builder_custom_separator() -> None:
    prompt = system_prompt_builder(["a", "b"], separator=" | ")
    assert prompt.get() == "a | b"


# ---------------------------------------------------------------------------
# from_llm
# ---------------------------------------------------------------------------


def test_from_llm_invokes_adapter() -> None:
    resp = LLMResponse(content="Hello!")
    adapter = MockAdapter([resp])
    msgs = state([ChatMessage(role="user", content="hi")])
    result = from_llm(adapter, msgs)
    # switchMap nodes need a subscriber to activate
    unsub = result.subscribe(lambda _: None)
    assert result.get() == resp
    unsub()


# ---------------------------------------------------------------------------
# agent_loop
# ---------------------------------------------------------------------------


def test_agent_loop_creates_graph() -> None:
    resp = LLMResponse(content="done", finish_reason="end_turn")
    adapter = MockAdapter([resp])
    loop = agent_loop("test-agent", adapter=adapter)
    assert isinstance(loop, AgentLoopGraph)
    assert loop.status.get() == "idle"
    assert loop.turn_count.get() == 0


def test_agent_loop_simple_conversation() -> None:
    resp = LLMResponse(content="Hello, human!", finish_reason="end_turn")
    adapter = MockAdapter([resp])
    loop = agent_loop("test-agent", adapter=adapter)

    result = loop.run("Hi!")
    assert result is not None
    assert result.content == "Hello, human!"
    assert loop.status.get() == "done"
    assert loop.turn_count.get() == 1


def test_agent_loop_tool_execution() -> None:
    tool_call_resp = LLMResponse(
        content="",
        tool_calls=(ToolCall(id="tc1", name="calc", arguments={"x": 5}),),
    )
    final_resp = LLMResponse(content="The result is 10", finish_reason="end_turn")
    adapter = MockAdapter([tool_call_resp, final_resp])

    tool = ToolDefinition(
        name="calc",
        description="Double a number",
        parameters={},
        handler=lambda args: args["x"] * 2,
    )

    loop = agent_loop("test-agent", adapter=adapter, tools=[tool])
    result = loop.run("Double 5 for me")

    assert result is not None
    assert result.content == "The result is 10"
    assert loop.turn_count.get() == 2
    msgs = loop.chat.all_messages()
    assert len(msgs) == 4
    assert msgs[2].role == "tool"


def test_agent_loop_max_turns() -> None:
    resp = LLMResponse(
        content="",
        tool_calls=(ToolCall(id="tc1", name="noop", arguments={}),),
    )
    adapter = MockAdapter([resp])
    tool = ToolDefinition(
        name="noop",
        description="No-op",
        parameters={},
        handler=lambda _args: None,
    )

    loop = agent_loop("test-agent", adapter=adapter, tools=[tool], max_turns=2)
    loop.run("loop forever")

    assert loop.turn_count.get() == 2
    assert loop.status.get() == "done"


def test_agent_loop_custom_stop_when() -> None:
    resp = LLMResponse(content="STOP_HERE")
    adapter = MockAdapter([resp])
    loop = agent_loop(
        "test-agent",
        adapter=adapter,
        stop_when=lambda r: r.content == "STOP_HERE",
    )
    result = loop.run("test")
    assert result is not None
    assert result.content == "STOP_HERE"
    assert loop.status.get() == "done"


class CoroutineLLMAdapter:
    """Adapter that returns a coroutine resolving to LLMResponse (from_any + first_value_from)."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self._idx = 0

    def invoke(
        self,
        messages: list[ChatMessage],
        opts: LLMInvokeOptions | None = None,
    ) -> Any:
        r = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1

        async def _c() -> LLMResponse:
            return r

        return _c()


def test_agent_loop_async_adapter_invoke() -> None:
    resp = LLMResponse(content="async-ok", finish_reason="end_turn")
    adapter = CoroutineLLMAdapter([resp])
    loop = agent_loop("test-agent", adapter=adapter)
    result = loop.run("hi")
    assert result is not None
    assert result.content == "async-ok"


# ---------------------------------------------------------------------------
# llm_extractor / llm_consolidator
# ---------------------------------------------------------------------------


def test_llm_extractor_returns_callable() -> None:
    resp = LLMResponse(content='{"upsert": [{"key": "k1", "value": "v1"}]}')
    adapter = MockAdapter([resp])
    fn = llm_extractor("Extract memories.", adapter=adapter)
    assert callable(fn)


def test_llm_consolidator_returns_callable() -> None:
    resp = LLMResponse(content='{"upsert": [{"key": "merged", "value": "combined"}]}')
    adapter = MockAdapter([resp])
    fn = llm_consolidator("Consolidate memories.", adapter=adapter)
    assert callable(fn)


# ---------------------------------------------------------------------------
# agent_memory
# ---------------------------------------------------------------------------


def test_agent_memory_creates_graph() -> None:
    source = state("test input")
    mem = agent_memory(
        "test-mem",
        source,
        extract_fn=lambda raw, _existing: Extraction(upsert=[{"key": "k1", "value": str(raw)}]),
        score=lambda _mem, _ctx: 1.0,
        cost=lambda _mem: 10.0,
        budget=100,
    )

    assert mem is not None
    desc = mem.describe()
    assert "store" in desc["nodes"]
    assert "compact" in desc["nodes"]
    assert "size" in desc["nodes"]


def test_agent_memory_requires_extract() -> None:
    try:
        agent_memory(
            "bad",
            state(None),
            score=lambda _m, _c: 1.0,
            cost=lambda _m: 1.0,
        )
        assert False, "Should have raised"  # noqa: B011
    except ValueError as e:
        assert "extract_fn or adapter" in str(e)


def test_agent_memory_optional_features_null() -> None:
    mem = agent_memory(
        "test-mem",
        state("x"),
        extract_fn=lambda _r, _e: Extraction(upsert=[]),
        score=lambda _m, _c: 1.0,
        cost=lambda _m: 1.0,
    )
    assert mem.vectors is None
    assert mem.kg is None
    assert mem.memory_tiers is None
    assert mem.retrieval is None
    assert mem.retrieval_trace is None
    assert mem.retrieve is None
    mem.destroy()


def test_agent_memory_vector_index() -> None:
    source = state("hello")
    mem = agent_memory(
        "vec-mem",
        source,
        extract_fn=lambda raw, _e: Extraction(upsert=[{"key": "k1", "value": str(raw)}]),
        score=lambda _m, _c: 1.0,
        cost=lambda _m: 10.0,
        budget=100,
        vector_dimensions=3,
        embed_fn=lambda _mem: (0.1, 0.2, 0.3),
    )
    assert mem.vectors is not None
    desc = mem.describe()
    assert "vectorIndex" in desc["nodes"]
    mem.destroy()


def test_agent_memory_knowledge_graph() -> None:
    source = state("hello")
    mem = agent_memory(
        "kg-mem",
        source,
        extract_fn=lambda raw, _e: Extraction(upsert=[{"key": "k1", "value": str(raw)}]),
        score=lambda _m, _c: 1.0,
        cost=lambda _m: 10.0,
        enable_knowledge_graph=True,
        entity_fn=lambda key, _mem: {"entities": [{"id": key, "value": {"name": key}}]},
    )
    assert mem.kg is not None
    mem.destroy()


def test_agent_memory_3_tier_storage() -> None:
    source = state("hello")
    mem = agent_memory(
        "tier-mem",
        source,
        extract_fn=lambda raw, _e: Extraction(upsert=[{"key": "core-profile", "value": str(raw)}]),
        score=lambda _m, _c: 1.0,
        cost=lambda _m: 10.0,
        tiers={
            "permanent_filter": lambda key, _mem: key.startswith("core-"),
            "max_active": 100,
        },
    )
    assert mem.memory_tiers is not None
    assert mem.memory_tiers.permanent is not None
    assert callable(mem.memory_tiers.tier_of)
    assert callable(mem.memory_tiers.mark_permanent)
    mem.destroy()


def test_agent_memory_retrieval_pipeline() -> None:
    source = state("test")
    mem = agent_memory(
        "retr-mem",
        source,
        extract_fn=lambda raw, _e: Extraction(upsert=[{"key": "m1", "value": f"mem-{raw}"}]),
        score=lambda _m, _c: 0.8,
        cost=lambda _m: 10.0,
        budget=100,
        vector_dimensions=3,
        embed_fn=lambda _mem: (1.0, 0.0, 0.0),
        retrieval_opts={"top_k": 5},
    )
    assert mem.retrieve is not None
    assert mem.retrieval_trace is not None
    results = mem.retrieve(RetrievalQuery(vector=(1.0, 0.0, 0.0)))
    assert isinstance(results, tuple)
    mem.destroy()


def test_agent_memory_retrieval_trace() -> None:
    source = state("input")
    mem = agent_memory(
        "trace-mem",
        source,
        extract_fn=lambda raw, _e: Extraction(upsert=[{"key": "k1", "value": str(raw)}]),
        score=lambda _m, _c: 1.0,
        cost=lambda _m: 5.0,
        budget=100,
        vector_dimensions=3,
        embed_fn=lambda _mem: (0.5, 0.5, 0.0),
    )
    mem.retrieve(RetrievalQuery(vector=(0.5, 0.5, 0.0)))
    trace = mem.retrieval_trace.get()
    if trace is not None:
        assert hasattr(trace, "vector_candidates")
        assert hasattr(trace, "graph_expanded")
        assert hasattr(trace, "ranked")
        assert hasattr(trace, "packed")
    mem.destroy()


# ---------------------------------------------------------------------------
# admission_filter_3d
# ---------------------------------------------------------------------------


def test_admission_filter_3d_admits() -> None:
    f = admission_filter_3d(
        score_fn=lambda _r: AdmissionScores(persistence=0.8, structure=0.5, personal_value=0.7),
    )
    assert f("test") is True


def test_admission_filter_3d_rejects_persistence() -> None:
    f = admission_filter_3d(
        score_fn=lambda _r: AdmissionScores(persistence=0.1, structure=0.5, personal_value=0.7),
        persistence_threshold=0.3,
    )
    assert f("test") is False


def test_admission_filter_3d_rejects_personal_value() -> None:
    f = admission_filter_3d(
        score_fn=lambda _r: AdmissionScores(persistence=0.8, structure=0.5, personal_value=0.1),
        personal_value_threshold=0.3,
    )
    assert f("test") is False


def test_admission_filter_3d_rejects_unstructured() -> None:
    f = admission_filter_3d(
        score_fn=lambda _r: AdmissionScores(persistence=0.8, structure=0, personal_value=0.7),
        require_structured=True,
    )
    assert f("test") is False


def test_admission_filter_3d_default_scorer() -> None:
    f = admission_filter_3d()
    assert f("anything") is True


def test_admission_filter_3d_integrates_with_agent_memory() -> None:
    filt = admission_filter_3d(
        score_fn=lambda raw: AdmissionScores(
            persistence=0.8 if raw == "keep" else 0.1,
            structure=0.5,
            personal_value=0.5,
        ),
    )
    source = state("keep")
    mem = agent_memory(
        "3d-mem",
        source,
        extract_fn=lambda raw, _e: Extraction(upsert=[{"key": "k", "value": str(raw)}]),
        score=lambda _m, _c: 1.0,
        cost=lambda _m: 1.0,
        admission_filter=filt,
    )
    assert mem is not None
    mem.destroy()


# ---------------------------------------------------------------------------
# knobs_as_tools (5.4)
# ---------------------------------------------------------------------------


def test_knobs_as_tools_generates_schemas() -> None:
    g = Graph("test")
    temp = state(
        72,
        meta={
            "description": "Room temperature",
            "type": "number",
            "range": [60, 90],
            "unit": "°F",
            "access": "both",
        },
    )
    mode = state(
        "auto",
        meta={
            "description": "HVAC mode",
            "type": "enum",
            "values": ["auto", "cool", "heat", "off"],
            "access": "llm",
        },
    )
    g.add("temperature", temp)
    g.add("mode", mode)

    result = knobs_as_tools(g)

    assert isinstance(result, KnobsAsToolsResult)
    assert len(result.openai) == 2
    assert len(result.mcp) == 2
    assert len(result.definitions) == 2

    # Check OpenAI schema shape
    temp_tool = next((t for t in result.openai if t.function["name"] == "temperature"), None)
    assert temp_tool is not None
    assert temp_tool.type == "function"
    assert temp_tool.function["description"] == "Room temperature"
    assert temp_tool.function["parameters"]["properties"]["value"]["type"] == "number"
    assert temp_tool.function["parameters"]["properties"]["value"]["minimum"] == 60
    assert temp_tool.function["parameters"]["properties"]["value"]["maximum"] == 90

    # Check MCP schema
    mode_mcp = next((t for t in result.mcp if t.name == "mode"), None)
    assert mode_mcp is not None
    assert mode_mcp.description == "HVAC mode"
    assert mode_mcp.input_schema["properties"]["value"]["enum"] == ["auto", "cool", "heat", "off"]

    # Handler calls graph.set
    temp_def = next((d for d in result.definitions if d.name == "temperature"), None)
    assert temp_def is not None
    temp_def.handler({"value": 80})
    assert temp.get() == 80

    g.destroy()


def test_knobs_as_tools_excludes_human_access() -> None:
    g = Graph("test")
    secret = state("pw", meta={"description": "Human-only secret", "access": "human"})
    g.add("secret", secret)

    result = knobs_as_tools(g)
    assert len(result.openai) == 0
    g.destroy()


def test_knobs_as_tools_includes_v0_version_metadata() -> None:
    g = Graph("versioned-knobs")
    knob = state(1, versioning=0, meta={"description": "Versioned knob", "access": "both"})
    g.add("knob", knob)
    result = knobs_as_tools(g)
    defn = next((d for d in result.definitions if d.name == "knob"), None)
    assert defn is not None
    assert defn.version == {"id": knob.v.id, "version": knob.v.version}
    g.destroy()


# ---------------------------------------------------------------------------
# gauges_as_context (5.4)
# ---------------------------------------------------------------------------


def test_gauges_as_context_formats_values() -> None:
    g = Graph("dashboard")
    revenue = state(
        1234.5,
        meta={
            "description": "Monthly revenue",
            "format": "currency",
            "tags": ["finance"],
        },
    )
    growth = state(
        0.15,
        meta={
            "description": "Growth rate",
            "format": "percentage",
            "tags": ["finance"],
        },
    )
    status_node = state(
        "healthy",
        meta={
            "description": "System status",
            "format": "status",
        },
    )
    g.add("revenue", revenue)
    g.add("growth", growth)
    g.add("status", status_node)

    ctx = gauges_as_context(g)

    assert "Monthly revenue: $1234.50" in ctx
    assert "Growth rate: 15.0%" in ctx
    assert "System status: healthy" in ctx
    assert "[finance]" in ctx

    g.destroy()


def test_gauges_as_context_empty_when_no_gauges() -> None:
    g = Graph("empty")
    plain = state(42)
    g.add("plain", plain)

    assert gauges_as_context(g) == ""
    g.destroy()


def test_gauges_as_context_since_version_filters_unchanged_nodes() -> None:
    g = Graph("delta")
    metric = state(1, versioning=0, meta={"description": "Metric", "access": "both"})
    g.add("metric", metric)
    since = {"metric": {"id": metric.v.id, "version": metric.v.version}}
    assert gauges_as_context(g, since_version=since) == ""
    metric.down([(MessageType.DATA, 2)])
    ctx = gauges_as_context(g, since_version=since)
    assert "Metric: 2" in ctx
    g.destroy()


# ---------------------------------------------------------------------------
# validate_graph_def (5.4)
# ---------------------------------------------------------------------------


def test_validate_graph_def_accepts_valid() -> None:
    defn = {
        "name": "test",
        "nodes": {
            "input": {"type": "state", "status": "settled", "deps": [], "meta": {}},
            "compute": {"type": "derived", "status": "settled", "deps": ["input"], "meta": {}},
        },
        "edges": [{"from": "input", "to": "compute"}],
        "subgraphs": [],
    }
    result = validate_graph_def(defn)
    assert result.valid is True
    assert len(result.errors) == 0


def test_validate_graph_def_rejects_missing_name() -> None:
    result = validate_graph_def({"nodes": {}, "edges": []})
    assert result.valid is False
    assert any("name" in e for e in result.errors)


def test_validate_graph_def_rejects_invalid_type() -> None:
    result = validate_graph_def(
        {
            "name": "test",
            "nodes": {"a": {"type": "unknown_type", "deps": [], "meta": {}}},
            "edges": [],
        }
    )
    assert result.valid is False
    assert any("invalid type" in e for e in result.errors)


def test_validate_graph_def_rejects_bad_edge_ref() -> None:
    result = validate_graph_def(
        {
            "name": "test",
            "nodes": {"a": {"type": "state", "deps": [], "meta": {}}},
            "edges": [{"from": "a", "to": "missing"}],
        }
    )
    assert result.valid is False
    assert any("missing" in e for e in result.errors)


def test_validate_graph_def_detects_duplicate_edge() -> None:
    result = validate_graph_def(
        {
            "name": "test",
            "nodes": {
                "a": {"type": "state", "deps": [], "meta": {}},
                "b": {"type": "derived", "deps": ["a"], "meta": {}},
            },
            "edges": [
                {"from": "a", "to": "b"},
                {"from": "a", "to": "b"},
            ],
        }
    )
    assert result.valid is False
    assert any("duplicate" in e for e in result.errors)


def test_validate_graph_def_rejects_non_dict() -> None:
    assert validate_graph_def(None).valid is False
    assert validate_graph_def("string").valid is False
    assert validate_graph_def(42).valid is False


def test_validate_graph_def_rejects_bad_dep_ref() -> None:
    result = validate_graph_def(
        {
            "name": "test",
            "nodes": {"a": {"type": "derived", "deps": ["nonexistent"], "meta": {}}},
            "edges": [],
        }
    )
    assert result.valid is False
    assert any("nonexistent" in e for e in result.errors)


# ---------------------------------------------------------------------------
# graph_from_spec (5.4)
# ---------------------------------------------------------------------------

import json  # noqa: E402


def test_graph_from_spec_constructs_graph() -> None:
    graph_def = {
        "name": "calculator",
        "nodes": {
            "a": {"type": "state", "value": 10, "deps": [], "meta": {"description": "Input A"}},
            "b": {"type": "state", "value": 20, "deps": [], "meta": {"description": "Input B"}},
        },
        "edges": [],
        "subgraphs": [],
    }
    adapter = MockAdapter([LLMResponse(content=json.dumps(graph_def), finish_reason="end_turn")])

    g = graph_from_spec("Build a calculator with two inputs", adapter)
    assert g.name == "calculator"
    assert g.get("a") == 10
    assert g.get("b") == 20
    g.destroy()


def test_graph_from_spec_strips_markdown_fences() -> None:
    graph_def = {
        "name": "simple",
        "nodes": {"x": {"type": "state", "value": 1, "deps": [], "meta": {"description": "X"}}},
        "edges": [],
        "subgraphs": [],
    }
    fenced = "```json\n" + json.dumps(graph_def) + "\n```"
    adapter = MockAdapter(
        [
            LLMResponse(content=fenced, finish_reason="end_turn"),
        ]
    )

    g = graph_from_spec("simple graph", adapter)
    assert g.name == "simple"
    g.destroy()


def test_graph_from_spec_raises_on_invalid_json() -> None:
    adapter = MockAdapter([LLMResponse(content="not json at all!", finish_reason="end_turn")])

    import pytest

    with pytest.raises(ValueError, match="not valid JSON"):
        graph_from_spec("bad", adapter)


def test_graph_from_spec_raises_on_validation_failure() -> None:
    adapter = MockAdapter(
        [
            LLMResponse(content=json.dumps({"nodes": {}, "edges": []}), finish_reason="end_turn"),
        ]
    )

    import pytest

    with pytest.raises(ValueError, match="invalid graph definition"):
        graph_from_spec("missing name", adapter)


# ---------------------------------------------------------------------------
# suggest_strategy (5.4)
# ---------------------------------------------------------------------------


def test_suggest_strategy_returns_plan() -> None:
    plan = {
        "summary": "Add a rate limiter node",
        "reasoning": "The API calls node has no rate limiting, which could cause throttling.",
        "operations": [
            {
                "type": "add_node",
                "name": "rate_limiter",
                "nodeType": "derived",
                "meta": {"description": "Rate limiter"},
            },
            {"type": "connect", "from": "rate_limiter", "to": "api_calls"},
            {"type": "set_value", "name": "max_rate", "value": 100},
        ],
    }
    adapter = MockAdapter([LLMResponse(content=json.dumps(plan), finish_reason="end_turn")])

    g = Graph("api")
    max_rate = state(50, meta={"description": "Max rate"})
    g.add("max_rate", max_rate)

    result = suggest_strategy(g, "API calls are being throttled", adapter)

    assert isinstance(result, StrategyPlan)
    assert result.summary == "Add a rate limiter node"
    assert "rate limiting" in result.reasoning
    assert len(result.operations) == 3
    assert result.operations[0].type == "add_node"
    assert result.operations[0].name == "rate_limiter"

    g.destroy()


def test_suggest_strategy_raises_on_invalid_json() -> None:
    adapter = MockAdapter([LLMResponse(content="just some text", finish_reason="end_turn")])

    import pytest

    g = Graph("test")
    with pytest.raises(ValueError, match="not valid JSON"):
        suggest_strategy(g, "problem", adapter)
    g.destroy()


def test_suggest_strategy_raises_on_missing_summary() -> None:
    adapter = MockAdapter(
        [
            LLMResponse(content=json.dumps({"operations": []}), finish_reason="end_turn"),
        ]
    )

    import pytest

    g = Graph("test")
    with pytest.raises(ValueError, match="missing 'summary'"):
        suggest_strategy(g, "problem", adapter)
    g.destroy()


# ---------------------------------------------------------------------------
# prompt_node
# ---------------------------------------------------------------------------


def test_prompt_node_calls_adapter_with_assembled_messages() -> None:
    """prompt_node assembles messages from deps and the prompt template."""
    captured: list[list[ChatMessage]] = []

    class CapturingAdapter:
        def invoke(
            self,
            messages: Any,
            opts: LLMInvokeOptions | None = None,
        ) -> LLMResponse:
            captured.append(list(messages))
            return LLMResponse(content="reply-1")

    adapter = CapturingAdapter()
    dep = state("world")
    pn = prompt_node(
        adapter,
        [dep],
        lambda val: f"Hello, {val}!",
        name="greeter",
        system_prompt="You are helpful.",
    )
    unsub = pn.subscribe(lambda _: None)

    assert pn.get() == "reply-1"
    assert len(captured) == 1
    msgs = captured[0]
    # System prompt comes first, then user message with the assembled prompt.
    assert msgs[0].role == "system"
    assert msgs[0].content == "You are helpful."
    assert msgs[1].role == "user"
    assert msgs[1].content == "Hello, world!"
    unsub()


def test_prompt_node_re_invokes_on_dep_change() -> None:
    """Changing a dependency triggers a new LLM invocation."""
    call_count = [0]

    class CountingAdapter:
        def invoke(
            self,
            messages: Any,
            opts: LLMInvokeOptions | None = None,
        ) -> LLMResponse:
            call_count[0] += 1
            # Extract user content to echo it back.
            user_msg = [m for m in messages if m.role == "user"][0]
            return LLMResponse(content=f"echo:{user_msg.content}")

    adapter = CountingAdapter()
    dep = state("alpha")
    pn = prompt_node(adapter, [dep], lambda v: f"say {v}")
    unsub = pn.subscribe(lambda _: None)

    assert pn.get() == "echo:say alpha"
    assert call_count[0] == 1

    dep.down([(MessageType.DATA, "beta")])
    assert pn.get() == "echo:say beta"
    assert call_count[0] == 2
    unsub()


def test_prompt_node_format_json_parses_response() -> None:
    """format='json' parses the LLM response content as JSON."""
    adapter = MockAdapter([LLMResponse(content='{"key": "value", "n": 42}')])
    dep = state("input")
    pn = prompt_node(adapter, [dep], "parse this", format="json")
    unsub = pn.subscribe(lambda _: None)

    result = pn.get()
    assert isinstance(result, dict)
    assert result == {"key": "value", "n": 42}
    unsub()


def test_prompt_node_retries_on_error() -> None:
    """prompt_node retries the configured number of times before succeeding."""
    attempts = [0]

    class FlakyAdapter:
        def invoke(
            self,
            messages: Any,
            opts: LLMInvokeOptions | None = None,
        ) -> LLMResponse:
            attempts[0] += 1
            if attempts[0] < 3:
                raise RuntimeError("transient failure")
            return LLMResponse(content="recovered")

    adapter = FlakyAdapter()
    dep = state("x")
    pn = prompt_node(adapter, [dep], "do it", retries=3)
    unsub = pn.subscribe(lambda _: None)

    assert pn.get() == "recovered"
    assert attempts[0] == 3  # 2 failures + 1 success
    unsub()


def test_prompt_node_cache_deduplicates_identical_calls() -> None:
    call_count = [0]

    class CountingAdapter:
        def invoke(
            self,
            messages: Any,
            opts: LLMInvokeOptions | None = None,
        ) -> LLMResponse:
            call_count[0] += 1
            return LLMResponse(content="result")

    adapter = CountingAdapter()
    dep = state("hello")
    pn = prompt_node(adapter, [dep], lambda v: f"summarize: {v}", cache=True)
    unsub = pn.subscribe(lambda _: None)

    assert pn.get() == "result"
    assert call_count[0] == 1

    # Re-emit same value — should hit cache
    dep.down([(MessageType.DATA, "hello")])
    assert pn.get() == "result"
    assert call_count[0] == 1  # no additional call

    # Change dep — different prompt text → different cache key
    dep.down([(MessageType.DATA, "world")])
    assert pn.get() == "result"
    assert call_count[0] == 2

    unsub()


# ---------------------------------------------------------------------------
# Mock adapter with streaming support
# ---------------------------------------------------------------------------


class StreamingMockAdapter:
    """Mock adapter with async stream — matches real LLM SDK behavior.

    ``stream()`` returns an ``AsyncIterable[str]``. Real LLM adapters (OpenAI,
    Anthropic) always return async iterables because token delivery requires
    network I/O. Tests MUST use reactive subscribe patterns or
    ``_wait_for_result`` instead of synchronous ``.get()`` after subscribe.
    """

    def __init__(
        self,
        responses: list[LLMResponse] | None = None,
        stream_chunks: list[list[str]] | None = None,
    ) -> None:
        self._responses = responses or []
        self._stream_chunks = stream_chunks or []
        self._invoke_idx = 0
        self._stream_idx = 0

    def invoke(
        self,
        messages: Any,
        opts: LLMInvokeOptions | None = None,
    ) -> LLMResponse:
        resp = self._responses[min(self._invoke_idx, len(self._responses) - 1)]
        self._invoke_idx += 1
        return resp

    async def stream(  # type: ignore[override]
        self,
        messages: Any,
        opts: LLMInvokeOptions | None = None,
    ) -> AsyncIterator[str]:
        chunks = (
            self._stream_chunks[min(self._stream_idx, len(self._stream_chunks) - 1)]
            if self._stream_chunks
            else []
        )
        self._stream_idx += 1
        for chunk in chunks:
            yield chunk


def _wait_for_result(
    node: Any,
    *,
    timeout: float = 2.0,
    predicate: Any = None,
) -> Any:
    """Block until a DATA value satisfying *predicate* arrives from *node*.

    Uses ``threading.Event`` — no polling.  Handles push-on-subscribe
    correctly (cached value may fire during ``subscribe()`` before the
    teardown ref is assigned).

    Args:
        node: Subscribable node to wait on.
        timeout: Max wait in seconds.
        predicate: Optional ``(value) -> bool``. Defaults to ``is not None``.
    """
    import threading as _threading

    check = predicate or (lambda v: v is not None)
    result_box: list[Any] = [None]
    done = _threading.Event()

    def _sink(msgs: Any) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA and check(m[1]):
                result_box[0] = m[1]
                done.set()

    teardown = node.subscribe(_sink)
    # Value may already have arrived via push-on-subscribe
    if done.is_set():
        teardown()
        return result_box[0]
    if not done.wait(timeout):
        teardown()
        msg = f"_wait_for_result: timed out after {timeout}s"
        raise TimeoutError(msg)
    teardown()
    return result_box[0]


# ---------------------------------------------------------------------------
# streaming_prompt_node
# ---------------------------------------------------------------------------


def test_streaming_prompt_node_emits_final_result() -> None:
    """Output node emits final accumulated text after stream completes."""
    adapter = StreamingMockAdapter(stream_chunks=[["Hello", " ", "world", "!"]])
    dep = state("greet")

    handle = streaming_prompt_node(adapter, [dep], lambda v: f"say {v}")
    assert isinstance(handle, StreamingPromptNodeHandle)

    result = _wait_for_result(handle.output)
    assert result == "Hello world!"

    handle.dispose()


def test_streaming_prompt_node_publishes_stream_chunks() -> None:
    """StreamChunk objects are published to the stream topic."""
    adapter = StreamingMockAdapter(stream_chunks=[["A", "B", "C"]])
    dep = state("go")

    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    # Wait for output to settle (stream done) before checking retained chunks
    _wait_for_result(handle.output)

    retained = handle.stream.retained()
    assert len(retained) >= 3
    assert retained[-3] == StreamChunk(source="llm", token="A", accumulated="A", index=0)
    assert retained[-2] == StreamChunk(source="llm", token="B", accumulated="AB", index=1)
    assert retained[-1] == StreamChunk(source="llm", token="C", accumulated="ABC", index=2)

    handle.dispose()


def test_streaming_prompt_node_sentinel_gate() -> None:
    """Nullish deps → empty messages → switch_map returns state(None) → null output."""
    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state(None)

    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    # Sentinel gate returns state(None) synchronously, so push-on-subscribe
    # delivers None immediately — use predicate that accepts None.
    result = _wait_for_result(handle.output, predicate=lambda _: True)
    assert result is None

    handle.dispose()


def test_streaming_prompt_node_json_format() -> None:
    """JSON format parses accumulated text as JSON."""
    adapter = StreamingMockAdapter(stream_chunks=[['{"key":', '"value"}']])
    dep = state("go")

    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v), format="json")

    result = _wait_for_result(handle.output)
    assert result == {"key": "value"}

    handle.dispose()


def test_streaming_prompt_node_json_with_fences() -> None:
    """JSON format strips markdown fences before parsing."""
    adapter = StreamingMockAdapter(stream_chunks=[["```json\n", '{"ok": true}', "\n```"]])
    dep = state("go")

    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v), format="json")

    result = _wait_for_result(handle.output)
    assert result == {"ok": True}

    handle.dispose()


def test_streaming_prompt_node_switchmap_cancels_on_new_input() -> None:
    """New dep value triggers a new stream; previous result is replaced."""
    adapter = StreamingMockAdapter(stream_chunks=[["slow", "stream"], ["fast"]])
    dep = state("first")

    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    # Wait for first stream to complete
    first = _wait_for_result(handle.output)
    assert first == "slowstream"

    # Trigger second input and wait for new result
    dep.down([(MessageType.DATA, "second")])
    second = _wait_for_result(handle.output, predicate=lambda v: v == "fast")
    assert second == "fast"

    handle.dispose()


# ---------------------------------------------------------------------------
# stream_extractor
# ---------------------------------------------------------------------------


def test_streaming_prompt_node_async_iterable_adapter() -> None:
    """Async iterable from adapter.stream() routes through from_async_iter."""

    class AsyncStreamAdapter:
        def invoke(self, messages: Any, opts: Any = None) -> LLMResponse:
            return LLMResponse(content="")

        async def stream(self, messages: Any, opts: Any = None) -> Any:
            for tok in ["async", "-", "chunk"]:
                yield tok

    adapter = AsyncStreamAdapter()
    dep = state("go")

    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    result = _wait_for_result(handle.output)
    assert result == "async-chunk"

    retained = handle.stream.retained()
    assert len(retained) >= 3
    assert retained[-1] == StreamChunk(
        source="llm", token="chunk", accumulated="async-chunk", index=2
    )

    handle.dispose()


def test_streaming_prompt_node_error_in_stream_settles_cleanly() -> None:
    """Exception during streaming settles output to partial accumulated text."""

    class ErrorStreamAdapter:
        def invoke(self, messages: Any, opts: Any = None) -> LLMResponse:
            return LLMResponse(content="")

        def stream(self, messages: Any, opts: Any = None) -> list[str]:
            raise RuntimeError("network error")

    adapter = ErrorStreamAdapter()
    dep = state("go")

    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    results: list[Any] = []
    handle.output.subscribe(
        lambda msgs: results.extend(m[1] for m in msgs if m[0] is MessageType.DATA)
    )

    # adapter.stream() raises before iteration → output settles to None
    assert None in results

    handle.dispose()


def test_streaming_prompt_node_mid_stream_error_settles_partial() -> None:
    """Error mid-iteration settles to partial accumulated text."""

    class MidErrorStreamAdapter:
        def invoke(self, messages: Any, opts: Any = None) -> LLMResponse:
            return LLMResponse(content="")

        def stream(self, messages: Any, opts: Any = None) -> Iterable[str]:
            yield "partial"
            raise RuntimeError("mid-stream failure")

    adapter = MidErrorStreamAdapter()
    dep = state("go")

    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    results: list[Any] = []
    handle.output.subscribe(
        lambda msgs: results.extend(m[1] for m in msgs if m[0] is MessageType.DATA)
    )

    non_null = [r for r in results if r is not None]
    assert len(non_null) >= 1
    assert non_null[-1] == "partial"

    handle.dispose()


def test_stream_extractor_extracts_from_topic() -> None:
    """Extractor derives values from stream topic chunks."""
    adapter = StreamingMockAdapter(stream_chunks=[["hello", " world"]])
    dep = state("go")

    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    import re

    extracted: list[Any] = []
    ext = stream_extractor(
        handle.stream,
        lambda acc: re.search(r"hello", acc).group(0) if re.search(r"hello", acc) else None,
        name="hello-detector",
    )
    ext.subscribe(lambda msgs: extracted.extend(m[1] for m in msgs if m[0] is MessageType.DATA))

    # Manually publish to test extractor in isolation
    handle.stream.publish(StreamChunk(source="test", token="hel", accumulated="hel", index=0))
    handle.stream.publish(StreamChunk(source="test", token="lo", accumulated="hello", index=1))

    assert None in extracted
    assert "hello" in extracted

    handle.dispose()


def test_stream_extractor_null_when_no_chunks() -> None:
    """Extractor returns None when no chunks have been published."""
    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state("go")

    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    ext = stream_extractor(handle.stream, lambda _acc: "found")
    ext.subscribe(lambda _: None)

    assert ext.get() is None

    handle.dispose()


# ---------------------------------------------------------------------------
# keyword_flag_extractor
# ---------------------------------------------------------------------------


def test_keyword_flag_extractor_detects_matches() -> None:
    """Keyword flag extractor detects configured patterns in stream."""
    adapter = StreamingMockAdapter(stream_chunks=[["a"]])
    dep = state("go")
    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    flags: list[tuple[KeywordFlag, ...]] = []
    ext = keyword_flag_extractor(
        handle.stream,
        patterns=[
            (r"setTimeout", "invariant-violation"),
            (r"\bSSN\b", "pii"),
        ],
    )
    ext.subscribe(lambda msgs: flags.extend(m[1] for m in msgs if m[0] is MessageType.DATA))

    handle.stream.publish(StreamChunk(source="test", token="use ", accumulated="use ", index=0))
    handle.stream.publish(
        StreamChunk(
            source="test",
            token="setTimeout and SSN",
            accumulated="use setTimeout and SSN",
            index=1,
        )
    )

    last = flags[-1]
    assert len(last) == 2
    assert last[0].label == "invariant-violation"
    assert last[0].match == "setTimeout"
    assert last[1].label == "pii"
    assert last[1].match == "SSN"

    handle.dispose()


def test_keyword_flag_extractor_empty_on_no_matches() -> None:
    """Returns empty tuple when no patterns match."""
    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state("go")
    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    ext = keyword_flag_extractor(
        handle.stream,
        patterns=[(r"setTimeout", "violation")],
    )
    ext.subscribe(lambda _: None)

    handle.stream.publish(
        StreamChunk(source="test", token="clean code", accumulated="clean code", index=0)
    )
    assert ext.get() == ()

    handle.dispose()


def test_keyword_flag_extractor_multiple_matches_same_pattern() -> None:
    """Finds multiple matches of the same pattern."""
    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state("go")
    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    ext = keyword_flag_extractor(
        handle.stream,
        patterns=[(r"TODO", "todo")],
    )
    ext.subscribe(lambda _: None)

    handle.stream.publish(
        StreamChunk(
            source="test",
            token="TODO fix TODO later",
            accumulated="TODO fix TODO later",
            index=0,
        )
    )

    result = ext.get()
    assert result is not None
    assert len(result) == 2
    assert result[0].position == 0
    assert result[1].position == 9

    handle.dispose()


# ---------------------------------------------------------------------------
# tool_call_extractor
# ---------------------------------------------------------------------------


def test_tool_call_extractor_extracts_tool_calls() -> None:
    """Extracts tool call JSON from stream."""
    import json as _json

    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state("go")
    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    calls: list[tuple[ExtractedToolCall, ...]] = []
    ext = tool_call_extractor(handle.stream)
    ext.subscribe(lambda msgs: calls.extend(m[1] for m in msgs if m[0] is MessageType.DATA))

    tool_json = _json.dumps({"name": "get_weather", "arguments": {"city": "NYC"}})
    handle.stream.publish(
        StreamChunk(
            source="test",
            token=tool_json,
            accumulated=f"Sure, let me check. {tool_json}",
            index=0,
        )
    )

    last = calls[-1]
    assert len(last) == 1
    assert last[0].name == "get_weather"
    assert last[0].arguments == {"city": "NYC"}
    assert last[0].start_index == 20  # after "Sure, let me check. "

    handle.dispose()


def test_tool_call_extractor_empty_for_partial_json() -> None:
    """Returns empty tuple for incomplete JSON."""
    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state("go")
    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    ext = tool_call_extractor(handle.stream)
    ext.subscribe(lambda _: None)

    handle.stream.publish(
        StreamChunk(source="test", token='{"name": "run', accumulated='{"name": "run', index=0)
    )
    assert ext.get() == ()

    handle.dispose()


def test_tool_call_extractor_ignores_non_tool_json() -> None:
    """Ignores JSON objects without name+arguments shape."""
    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state("go")
    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    ext = tool_call_extractor(handle.stream)
    ext.subscribe(lambda _: None)

    handle.stream.publish(
        StreamChunk(source="test", token='{"foo": "bar"}', accumulated='{"foo": "bar"}', index=0)
    )
    assert ext.get() == ()

    handle.dispose()


def test_tool_call_extractor_handles_braces_in_strings() -> None:
    """Handles braces inside JSON string values."""
    import json as _json

    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state("go")
    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    ext = tool_call_extractor(handle.stream)
    ext.subscribe(lambda _: None)

    tool_json = _json.dumps({"name": "run_code", "arguments": {"code": 'if (x) { return "}" }'}})
    handle.stream.publish(
        StreamChunk(source="test", token=tool_json, accumulated=tool_json, index=0)
    )

    result = ext.get()
    assert result is not None
    assert len(result) == 1
    assert result[0].name == "run_code"
    assert result[0].arguments == {"code": 'if (x) { return "}" }'}

    handle.dispose()


def test_tool_call_extractor_multiple_calls() -> None:
    """Extracts multiple tool calls from one stream."""
    import json as _json

    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state("go")
    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    ext = tool_call_extractor(handle.stream)
    ext.subscribe(lambda _: None)

    call1 = _json.dumps({"name": "a", "arguments": {"x": 1}})
    call2 = _json.dumps({"name": "b", "arguments": {"y": 2}})
    handle.stream.publish(
        StreamChunk(
            source="test",
            token=f"{call1} then {call2}",
            accumulated=f"{call1} then {call2}",
            index=0,
        )
    )

    result = ext.get()
    assert result is not None
    assert len(result) == 2
    assert result[0].name == "a"
    assert result[1].name == "b"

    handle.dispose()


# ---------------------------------------------------------------------------
# cost_meter_extractor
# ---------------------------------------------------------------------------


def test_cost_meter_extractor_tracks_counts() -> None:
    """Tracks chunk count, char count, and estimated tokens."""
    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state("go")
    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    ext = cost_meter_extractor(handle.stream)
    ext.subscribe(lambda _: None)

    handle.stream.publish(StreamChunk(source="test", token="hello", accumulated="hello", index=0))

    reading = ext.get()
    assert reading is not None
    assert reading.chunk_count == 1
    assert reading.char_count == 5
    assert reading.estimated_tokens == 2  # ceil(5/4)

    handle.dispose()


def test_cost_meter_extractor_accumulates() -> None:
    """Accumulates across chunks."""
    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state("go")
    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    ext = cost_meter_extractor(handle.stream)
    ext.subscribe(lambda _: None)

    handle.stream.publish(StreamChunk(source="test", token="hello", accumulated="hello", index=0))
    handle.stream.publish(
        StreamChunk(source="test", token=" world", accumulated="hello world", index=1)
    )

    reading = ext.get()
    assert reading is not None
    assert reading.chunk_count == 2
    assert reading.char_count == 11
    assert reading.estimated_tokens == 3  # ceil(11/4)

    handle.dispose()


def test_cost_meter_extractor_custom_chars_per_token() -> None:
    """Uses custom chars_per_token."""
    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state("go")
    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    ext = cost_meter_extractor(handle.stream, chars_per_token=2)
    ext.subscribe(lambda _: None)

    handle.stream.publish(StreamChunk(source="test", token="hello", accumulated="hello", index=0))

    assert ext.get() is not None
    assert ext.get().estimated_tokens == 3  # ceil(5/2)

    handle.dispose()


def test_cost_meter_extractor_zero_reading() -> None:
    """Returns zero reading when no chunks."""
    adapter = StreamingMockAdapter(stream_chunks=[])
    dep = state("go")
    handle = streaming_prompt_node(adapter, [dep], lambda v: str(v))

    ext = cost_meter_extractor(handle.stream)
    ext.subscribe(lambda _: None)

    assert ext.get() == CostMeterReading(chunk_count=0, char_count=0, estimated_tokens=0)

    handle.dispose()


# ---------------------------------------------------------------------------
# gated_stream
# ---------------------------------------------------------------------------


def _collect_non_null(target: list[Any]) -> Any:
    """Subscriber that collects non-null DATA values."""
    def _sub(msgs: Any) -> None:
        for m in msgs:
            if m[0] is MessageType.DATA and m[1] is not None:
                target.append(m[1])
    return _sub


def test_gated_stream_gates_and_allows_approval() -> None:
    """Output is gated; approve forwards the final result."""
    adapter = StreamingMockAdapter(stream_chunks=[["hello", " world"]])
    graph = Graph("test")
    dep = state("go")

    handle = gated_stream(graph, "review", adapter, [dep], lambda v: f"say {v}")
    results: list[Any] = []
    handle.output.subscribe(_collect_non_null(results))

    # Wait for async stream to complete and enqueue in gate
    _wait_for_result(handle.gate.count, predicate=lambda v: v >= 1)
    handle.gate.approve()
    assert len(results) == 1
    assert results[0] == "hello world"
    handle.dispose()


def test_gated_stream_reject_discards_pending() -> None:
    """Reject discards the pending value and toggles the cancel signal."""
    adapter = StreamingMockAdapter(stream_chunks=[["token1", " token2"]])
    graph = Graph("test")
    dep = state("go")

    handle = gated_stream(graph, "review", adapter, [dep], lambda v: f"say {v}")
    handle.output.subscribe(lambda _: None)

    _wait_for_result(handle.gate.count, predicate=lambda v: v >= 1)
    handle.gate.reject()
    assert handle.gate.count.get() == 0
    handle.dispose()


def test_gated_stream_modify_transforms_pending() -> None:
    """Modify transforms the pending value before forwarding."""
    adapter = StreamingMockAdapter(stream_chunks=[["original"]])
    graph = Graph("test")
    dep = state("go")

    handle = gated_stream(graph, "review", adapter, [dep], lambda v: f"say {v}")
    results: list[Any] = []
    handle.output.subscribe(_collect_non_null(results))

    _wait_for_result(handle.gate.count, predicate=lambda v: v >= 1)
    handle.gate.modify(lambda v, _i, _p: f"{v} [reviewed]")
    assert len(results) == 1
    assert results[0] == "original [reviewed]"
    handle.dispose()


def test_gated_stream_publishes_chunks_while_pending() -> None:
    """Stream topic publishes chunks even while the gate is pending."""
    adapter = StreamingMockAdapter(stream_chunks=[["a", "b", "c"]])
    graph = Graph("test")
    dep = state("go")

    handle = gated_stream(graph, "review", adapter, [dep], lambda v: str(v))
    chunks: list[StreamChunk] = []
    handle.stream.latest.subscribe(_collect_non_null(chunks))
    handle.output.subscribe(lambda _: None)

    # Wait for stream to complete (gate enqueues value)
    _wait_for_result(handle.gate.count, predicate=lambda v: v >= 1)
    assert len(chunks) > 0
    assert chunks[-1].accumulated == "abc"
    handle.gate.approve()
    handle.dispose()


def test_gated_stream_start_open_auto_approves() -> None:
    """With start_open=True, values flow through without gating."""
    adapter = StreamingMockAdapter(stream_chunks=[["auto"]])
    graph = Graph("test")
    dep = state("go")

    handle = gated_stream(
        graph, "review", adapter, [dep], lambda v: str(v), start_open=True
    )
    result = _wait_for_result(handle.output)
    assert result == "auto"
    handle.dispose()
