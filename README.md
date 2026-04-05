# GraphReFly

**Describe what matters. It watches, filters, and explains — persistently.**

You're buried under emails, alerts, feeds, and messages. You can't process it all. GraphReFly lets you describe automations in plain language, review them visually, run them persistently, and trace every decision back to its source.

[![PyPI](https://img.shields.io/pypi/v/graphrefly?color=blue)](https://pypi.org/project/graphrefly/)
[![license](https://img.shields.io/github/license/graphrefly/graphrefly-py)](./LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/graphrefly)](https://pypi.org/project/graphrefly/)

[Docs](https://py.graphrefly.dev) | [Spec](https://py.graphrefly.dev/spec/) | [TypeScript](https://graphrefly.dev) | [API Reference](https://py.graphrefly.dev/api/)

---

<!-- TODO: Demo 0 GIF/video — NL → flow view → running → "why was this flagged?" -->

## What can you do with it?

**Email triage** — "Watch my inbox. Urgent emails from my team go to a priority list. Newsletters get summarized weekly. Everything else, count by sender." It watches, classifies, and alerts — and when you ask "why was this flagged?", it walks you through the reasoning.

**Spending alerts** — Connect bank transactions to budget categories. Get a push notification when monthly dining exceeds your target. No polling, no manual checks — changes propagate the moment data arrives.

**Knowledge management** — Notes, bookmarks, highlights flow in. Contradictions surface automatically. Related ideas link themselves. Your second brain stays current without you maintaining it.

---

## Quick start

```bash
pip install graphrefly
```

```python
from graphrefly import state, derived, effect

count = state(0)
doubled = derived([count], lambda deps, _: deps[0] * 2)

effect([doubled], lambda deps, _: print("doubled:", deps[0]))
# → doubled: 0

count.push(3)
# → doubled: 6
```

## How it works

You describe what you need — an LLM composes a reactive graph (like SQL for data flows). The graph runs persistently, checkpoints its state, and traces every decision through a causal chain. Ask "why?" at any point and get a human-readable explanation from source to conclusion.

## Why GraphReFly?

|  | Redux / Zustand | RxPY | Pydantic AI | LangGraph | TC39 Signals | **GraphReFly** |
|--|-----------------|------|-------------|-----------|-------------|---------------|
| Simple store API | yes | no | no | no | yes | **yes** |
| Streaming operators | no | yes | no | no | no | **yes** |
| Diamond resolution | no | n/a | n/a | n/a | partial | **glitch-free** |
| Graph introspection | no | no | no | checkpoints | no | **describe / observe / diagram** |
| Causal tracing | no | no | no | no | no | **explain every decision** |
| Durable checkpoints | no | no | no | yes | no | **file / SQLite / IndexedDB** |
| LLM orchestration | no | no | partial | yes | no | **agent_loop / chat_stream / tool_registry** |
| NL → graph composition | no | no | no | no | no | **graph_from_spec / llm_compose** |
| Async runners | n/a | asyncio | asyncio | asyncio | n/a | **asyncio / trio** |
| Dependencies | varies | 0 | many | many | n/a | **0** |

## One primitive

Everything is a `node`. Sugar constructors give you the right shape:

```python
from graphrefly import state, derived, producer, effect
from graphrefly.core.messages import DATA

# Writable state
name = state("world")

# Computed (re-runs when deps change)
greeting = derived([name], lambda deps, _: f"Hello, {deps[0]}!")

# Push source (timers, events, async streams)
clock = producer(lambda emit, _: emit([(DATA, time.time())]))

# Side effect
effect([greeting], lambda deps, _: print(deps[0]))
```

## Streaming & operators

70+ operators — transform, combine, buffer, window, rate-limit, retry, circuit-break:

```python
from graphrefly.extra.tier1 import map_op, filter_op, scan
from graphrefly.extra.tier2 import switch_map, debounce_time
from graphrefly.extra.resilience import retry
from graphrefly import pipe

search = pipe(
    user_input,
    debounce_time(0.3),
    switch_map(lambda q: from_promise(fetch(f"/api?q={q}"))),
    retry(strategy="exponential", max_attempts=3),
)
```

## Graph container

Register nodes in a `Graph` for introspection, snapshot, and persistence:

```python
from graphrefly import Graph, state, derived

g = Graph("pricing")
price = g.register("price", state(100))
tax   = g.register("tax", derived([price], lambda d, _: d[0] * 0.1))
total = g.register("total", derived([price, tax], lambda d, _: d[0] + d[1]))

g.describe()   # → full graph topology as dict
g.diagram()    # → Mermaid diagram string
g.observe(lambda e: print(e))  # → live change stream
```

## AI & orchestration

First-class patterns for LLM streaming, agent loops, and human-in-the-loop workflows:

```python
from graphrefly.patterns.ai import chat_stream, agent_loop, tool_registry
from graphrefly.patterns.memory import collection, decay

# Streaming chat with tool use
chat = chat_stream("assistant", model="claude-sonnet-4-20250514",
                   tools=tool_registry("tools", search=search_fn))

# Full agent loop: observe → think → act → memory
agent = agent_loop("researcher", llm=chat,
                   memory=agent_memory(decay="openviking"))
```

## Async runners

Native asyncio and trio support for async sources and long-running graphs:

```python
from graphrefly.compat.asyncio_runner import AsyncioRunner
from graphrefly.extra.sources import from_async_iter

# Wrap an async generator as a reactive node
async def sse_events():
    async for event in httpx_client.stream("GET", "/events"):
        yield event.data

events = from_async_iter(sse_events())

# Run the graph in an asyncio event loop
runner = AsyncioRunner(graph)
await runner.run()
```

## FastAPI integration

Drop-in integration for reactive backends:

```python
from graphrefly.integrations.fastapi import GraphReflyRouter

router = GraphReflyRouter(graph)
app.include_router(router, prefix="/graph")
# GET /graph/describe  → graph topology
# GET /graph/snapshot  → current state
# WS  /graph/observe   → live change stream
```

## Resilience & checkpoints

Built-in retry, circuit breakers, rate limiters, and persistent checkpoints:

```python
from graphrefly.extra.resilience import retry, circuit_breaker, rate_limiter
from graphrefly.extra.checkpoint import FileCheckpointAdapter, save_graph_checkpoint

# Retry with exponential backoff
resilient = pipe(source, retry(strategy="exponential"))

# Circuit breaker
breaker = circuit_breaker(threshold=5, reset_timeout=30.0)

# Checkpoint to file system
adapter = FileCheckpointAdapter("./checkpoints")
save_graph_checkpoint(graph, adapter)
```

## Project layout

| Path | Contents |
|------|----------|
| `src/graphrefly/core/` | Message protocol, `node` primitive, batch, sugar constructors |
| `src/graphrefly/extra/` | Operators, sources, data structures, resilience, checkpoints |
| `src/graphrefly/graph/` | `Graph` container, describe/observe, snapshot, persistence |
| `src/graphrefly/patterns/` | Orchestration, messaging, memory, AI, CQRS, reactive layout |
| `src/graphrefly/compat/` | Async runners (asyncio, trio) |
| `src/graphrefly/integrations/` | Framework integrations (FastAPI) |
| `docs/` | Roadmap, guidance, benchmarks |
| `website/` | Astro + Starlight docs site ([py.graphrefly.dev](https://py.graphrefly.dev)) |

## Scripts

```bash
uv run pytest              # run tests
uv run ruff check .        # lint
uv run mypy src/           # type check
uv run pytest --benchmark  # benchmarks
```

## Requirements

Python 3.12 or later. Zero runtime dependencies.

## Acknowledgments

GraphReFly builds on ideas from many projects and papers:

**Protocol & predecessor:**
- **[Callbag](https://github.com/callbag/callbag)** (Andre Staltz) — the original reactive protocol spec. GraphReFly's message-based node communication descends from callbag's function-calling-function model.
- **[callbag-recharge](https://github.com/Callbag-Recharge/callbag-recharge)** & **[callbag-recharge-py](https://github.com/Callbag-Recharge/callbag-recharge-py)** — GraphReFly's direct predecessors. The Python port (6 primitives, 18 operators, 100+ tests) established cross-language parity patterns carried forward.

**Reactive design patterns:**
- **[SolidJS](https://github.com/solidjs/solid)** — two-phase execution (DIRTY propagation + value flow), automatic caching, and effect batching. Closest philosophical neighbor.
- **[Preact Signals](https://github.com/preactjs/signals)** — fine-grained reactivity and cached-flag optimization patterns that informed RESOLVED signal design.
- **[TC39 Signals Proposal](https://github.com/tc39/proposal-signals)** — the `.get()/.set()` contract and the push toward language-level reactivity.
- **[RxJS](https://github.com/ReactiveX/rxjs)** / **[RxPY](https://github.com/ReactiveX/RxPY)** — operator naming conventions and the DevTools observability philosophy that inspired the Inspector pattern.

**AI & memory:**
- **[OpenViking](https://github.com/volcengine/openviking)** (Volcengine) — the memory decay formula (`sigmoid(log1p(count)) * exp_decay(age, 7d)`) and L0/L1/L2 progressive loading strategy used in `agent_memory()`.
- **[FadeMem](https://arxiv.org/abs/2501.09399)** (Wei et al., ICASSP 2026) — biologically-inspired dual-layer memory with adaptive exponential decay.
- **[MAGMA](https://arxiv.org/abs/2501.13920)** (Jiang et al., 2026) — four-parallel-graph model (semantic/temporal/causal/entity) that informed `knowledge_graph()` design.
- **[Letta/MemGPT](https://github.com/letta-ai/letta)**, **[Mem0](https://github.com/mem0ai/mem0)**, **[Zep/Graphiti](https://github.com/getzep/graphiti)**, **[Cognee](https://github.com/topoteretes/cognee)** — production memory architectures surveyed during `agent_memory()` design.

**Layout & other:**
- **[Pretext](https://github.com/chenglou/pretext)** (Cheng Lou) — inspired the reactive layout engine's DOM-free text measurement pipeline.
- **[CASL](https://github.com/stalniy/casl)** — declarative `allow()`/`deny()` policy builder DX that inspired `policy()`.
- **[Nanostores](https://github.com/nanostores/nanostores)** — tiny framework-agnostic API with `.get()/.set()/.subscribe()` mapping that validated the store ergonomics.

## License

[MIT](./LICENSE)
