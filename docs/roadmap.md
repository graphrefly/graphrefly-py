# Roadmap

> **Spec:** [GRAPHREFLY-SPEC.md](GRAPHREFLY-SPEC.md)
>
> **Contributing docs:** [docs-guidance.md](docs-guidance.md), [test-guidance.md](test-guidance.md)
>
> **Predecessor:** callbag-recharge-py (Phase 0-1 complete: 6 core primitives, 18 operators,
> utils/resilience, 100+ tests, per-subgraph concurrency). Key patterns and lessons carried
> forward — see `archive/docs/DESIGN-ARCHIVE-INDEX.md` for lineage.

---

## Phase 0: Foundation

### 0.1 — Project scaffold

- [x] Repository setup: uv, mise, ruff, pytest, mypy
- [x] `GRAPHREFLY-SPEC.md` in docs (done)
- [x] Folder structure: `src/graphrefly/core/`, `src/graphrefly/extra/`, `src/graphrefly/graph/`
- [x] Package config: `graphrefly-py` on PyPI, `graphrefly` import name

### 0.2 — Message protocol

- [x] Message type enum: DATA, DIRTY, RESOLVED, INVALIDATE, PAUSE, RESUME, TEARDOWN, COMPLETE, ERROR
- [x] `Messages` type: `list[tuple[Type, Any] | tuple[Type]]` — always list of tuples
- [x] `batch()` context manager — defers DATA, not DIRTY
- [x] Protocol invariant tests

### 0.3 — Node primitive

- [ ] `node(deps?, fn?, opts?)` — single primitive
- [ ] Node interface: `.get()`, `.status`, `.down()`, `.up()`, `.unsubscribe()`
- [ ] Output slot: None → single sink → set (optimization)
- [ ] Two-phase push: DIRTY propagation (phase 1), DATA/RESOLVED propagation (phase 2)
- [ ] Diamond resolution via bitmask (unlimited-precision Python int)
- [ ] `equals` option for RESOLVED check
- [ ] Lazy connect/disconnect on subscribe/unsubscribe
- [ ] Error handling: fn throws → `[[ERROR, err]]` downstream
- [ ] `resubscribable` and `reset_on_teardown` options

### 0.4 — Concurrency

- [ ] Lock-free `get()` — any thread, any time
- [ ] Per-subgraph write locks via Union-Find (port from callbag-recharge-py)
- [ ] Weak-ref registry auto-cleanup
- [ ] `defer_set()` for safe cross-subgraph writes
- [ ] Thread-local batch isolation
- [ ] Free-threaded Python 3.14 compatibility

### 0.5 — Meta (companion stores)

- [ ] `meta` option: each key becomes a subscribable node
- [ ] Meta nodes participate in describe() output
- [ ] Meta nodes independently observable

### 0.6 — Sugar constructors

- [ ] `state(initial, opts?)` — no deps, no fn
- [ ] `producer(fn, opts?)` — no deps, with fn
- [ ] `derived(deps, fn, opts?)` — deps, fn returns value
- [ ] `operator(deps, fn, opts?)` — deps, fn uses down()
- [ ] `effect(deps, fn)` — deps, fn returns nothing
- [ ] `subscribe(dep, callback)` — single dep shorthand, context manager support
- [ ] `pipe(source, op1, op2)` + `|` operator overload

### 0.7 — Tests & validation

- [ ] Core node tests (state, derived, effect patterns)
- [ ] Diamond resolution tests
- [ ] Lifecycle signal tests (INVALIDATE, PAUSE, RESUME, TEARDOWN)
- [ ] Batch tests
- [ ] Meta companion store tests
- [ ] Concurrency stress tests (port from callbag-recharge-py)
- [ ] Benchmarks vs callbag-recharge-py (regression guard)

---

## Phase 1: Graph Container

### 1.1 — Graph core

- [ ] `Graph(name, opts?)` constructor
- [ ] `graph.add(name, node)` / `graph.remove(name)`
- [ ] `graph.get(name)` / `graph.set(name, value)` / `graph.node(name)`
- [ ] `graph.connect(from_name, to_name)` / `graph.disconnect(from_name, to_name)`

### 1.2 — Composition

- [ ] `graph.mount(name, child_graph)` — subgraph embedding
- [ ] Colon-delimited namespace: `"parent:child:node"`
- [ ] `graph.resolve(path)` — node lookup by qualified path
- [ ] Lifecycle signal propagation through mount hierarchy

### 1.3 — Introspection

- [ ] `graph.describe()` → JSON dict (nodes, edges, subgraphs, meta)
- [ ] `graph.observe(name?)` → live message stream
- [ ] Type inference in describe output

### 1.4 — Lifecycle & persistence

- [ ] `graph.signal(messages)` — broadcast to all nodes
- [ ] `graph.destroy()` — TEARDOWN all
- [ ] `graph.snapshot()` / `graph.restore(data)` / `Graph.from_snapshot(data)`
- [ ] `graph.to_json()` — deterministic serialization

### 1.5 — Tests

- [ ] Graph add/remove/connect/disconnect
- [ ] Mount and namespace resolution
- [ ] describe() output validation
- [ ] observe() message stream tests
- [ ] Snapshot round-trip tests

---

## Phase 2: Extra (Operators & Sources)

Port proven operators from callbag-recharge-py + new ones from TS.

### 2.1 — Tier 1 operators (sync, static deps)

- [ ] `map`, `filter`, `scan`, `reduce`
- [ ] `take`, `skip`, `take_while`, `take_until`
- [ ] `first`, `last`, `find`, `element_at`
- [ ] `start_with`, `tap`, `distinct_until_changed`, `pairwise`
- [ ] `combine`, `merge`, `with_latest_from`, `zip`
- [ ] `concat`, `race`

### 2.2 — Tier 2 operators (async, dynamic)

- [ ] `switch_map`, `concat_map`, `exhaust_map`, `flat_map`
- [ ] `debounce`, `throttle`, `sample`, `audit`
- [ ] `delay`, `timeout`
- [ ] `buffer`, `buffer_count`, `buffer_time`
- [ ] `interval`, `repeat`
- [ ] `pausable`, `rescue`

### 2.3 — Sources & sinks

- [ ] `from_timer`, `from_iter`, `from_any`
- [ ] `from_awaitable`, `from_async_iter`
- [ ] `first_value_from` (the ONE bridge to sync/Future)
- [ ] `of`, `empty`, `never`, `throw_error`
- [ ] `for_each`, `to_list`
- [ ] `share`, `cached`, `replay`

---

## Phase 3: Resilience & Data

### 3.1 — Utils (resilience)

- [ ] `retry`, `backoff` (exponential, linear, fibonacci)
- [ ] `with_breaker` (circuit breaker)
- [ ] `rate_limiter`, `token_tracker`
- [ ] `with_status` (sugar for meta companion stores)
- [ ] `checkpoint` + adapters

### 3.2 — Data structures

- [ ] `reactive_map` (KV with TTL, eviction)
- [ ] `reactive_log` (append-only, reactive tail/slice)
- [ ] `reactive_index` (dual-key sorted index)
- [ ] `reactive_list` (positional operations)
- [ ] `pubsub` (lazy topic stores)

---

## Phase 4: Domain Layers (Graph Factories)

Each returns a `Graph` — uniform introspection, lifecycle, persistence.

### 4.1 — Orchestration

- [ ] `pipeline()` → Graph
- [ ] `task()`, `branch()`, `gate()`, `approval()`
- [ ] `for_each()`, `join()`, `loop()`, `sub_pipeline()`
- [ ] `sensor()`, `wait()`, `on_failure()`
- [ ] Mermaid/D2 diagram export

### 4.2 — Messaging

- [ ] `topic()` → Graph
- [ ] `subscription()` (cursor-based consumer)
- [ ] `job_queue()` → Graph
- [ ] `job_flow()` → Graph
- [ ] `topic_bridge()` (distributed sync)

### 4.3 — Memory

- [ ] `collection()` → Graph
- [ ] `light_collection()` (FIFO/LRU)
- [ ] `vector_index()` (HNSW)
- [ ] `knowledge_graph()` → Graph
- [ ] `decay()` scoring

### 4.4 — AI surface

- [ ] `chat_stream()` → Graph
- [ ] `agent_loop()` → Graph
- [ ] `from_llm()` (adapter)
- [ ] `tool_registry()` → Graph
- [ ] `agent_memory()` → Graph
- [ ] `system_prompt_builder()`

---

## Phase 5: Framework & Distribution

### 5.1 — Framework compat

- [ ] FastAPI integration
- [ ] Django integration
- [ ] asyncio / trio Runner protocol

### 5.2 — Adapters

- [ ] `from_http`, `from_websocket` / `to_websocket`
- [ ] `from_webhook`, `to_sse`
- [ ] `from_mcp` (Model Context Protocol)

### 5.3 — LLM tool integration

- [ ] `knobs_as_tools(graph)` → OpenAI/MCP tool schemas from describe()
- [ ] `gauges_as_context(graph)` → formatted gauge values for system prompts
- [ ] Graph builder validation

---

## Phase 6: Node Versioning

- [ ] V0: id + version (recommended minimum)
- [ ] V1: + cid + prev (content addressing, linked history)
- [ ] V2: + schema (type validation)
- [ ] V3: + caps + refs (access control, cross-graph references)
- [ ] Attribution: mutation records with actor (human/llm/system)

---

## Phase 7: Polish & Launch

- [ ] README
- [ ] `llms.txt` for AI agent discovery
- [ ] PyPI publish: `graphrefly-py`
- [ ] Docs site
- [ ] Benchmarks vs RxPY, manual state, asyncio patterns
- [ ] Free-threaded Python 3.14 benchmark suite
- [ ] Community launch

---

## Effort Key

| Size | Meaning |
|------|---------|
| **S** | Half day or less |
| **M** | 1-2 days |
| **L** | 3-4 days |
| **XL** | 5+ days |
