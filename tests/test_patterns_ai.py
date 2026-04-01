"""AI surface pattern tests (roadmap §4.4)."""

from __future__ import annotations

from typing import Any

from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import state
from graphrefly.patterns.ai import (
    AdmissionScores,
    AgentLoopGraph,
    ChatMessage,
    ChatStreamGraph,
    LLMInvokeOptions,
    LLMResponse,
    RetrievalQuery,
    ToolCall,
    ToolDefinition,
    ToolRegistryGraph,
    admission_filter_3d,
    agent_loop,
    agent_memory,
    chat_stream,
    from_llm,
    llm_consolidator,
    llm_extractor,
    system_prompt_builder,
    tool_registry,
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
    tr.register(ToolDefinition(
        name="greet",
        description="Greet",
        parameters={},
        handler=lambda args: f"Hello, {args['name']}!",
    ))
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
    prompt = system_prompt_builder([
        "You are a helpful assistant.",
        "Be concise.",
    ])
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
        extract_fn=lambda raw, _existing: {"upsert": [{"key": "k1", "value": str(raw)}]},
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
        extract_fn=lambda _r, _e: {"upsert": []},
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
        extract_fn=lambda raw, _e: {"upsert": [{"key": "k1", "value": str(raw)}]},
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
        extract_fn=lambda raw, _e: {"upsert": [{"key": "k1", "value": str(raw)}]},
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
        extract_fn=lambda raw, _e: {"upsert": [{"key": "core-profile", "value": str(raw)}]},
        score=lambda _m, _c: 1.0,
        cost=lambda _m: 10.0,
        tiers={
            "permanent_filter": lambda key, _mem: key.startswith("core-"),
            "max_active": 100,
        },
    )
    assert mem.memory_tiers is not None
    assert mem.memory_tiers["permanent"] is not None
    assert callable(mem.memory_tiers["tier_of"])
    assert callable(mem.memory_tiers["mark_permanent"])
    mem.destroy()


def test_agent_memory_retrieval_pipeline() -> None:
    source = state("test")
    mem = agent_memory(
        "retr-mem",
        source,
        extract_fn=lambda raw, _e: {"upsert": [{"key": "m1", "value": f"mem-{raw}"}]},
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
        extract_fn=lambda raw, _e: {"upsert": [{"key": "k1", "value": str(raw)}]},
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
        extract_fn=lambda raw, _e: {"upsert": [{"key": "k", "value": str(raw)}]},
        score=lambda _m, _c: 1.0,
        cost=lambda _m: 1.0,
        admission_filter=filt,
    )
    assert mem is not None
    mem.destroy()
