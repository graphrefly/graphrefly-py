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

- [x] `node(deps?, fn?, opts?)` — single primitive
- [x] Node interface: `.get()`, `.status`, `.down()`, `.up()`, `.unsubscribe()`
- [x] Output slot: None → single sink → set (optimization)
- [x] Two-phase push: DIRTY propagation (phase 1), DATA/RESOLVED propagation (phase 2)
- [x] Diamond resolution via bitmask (unlimited-precision Python int)
- [x] `equals` option for RESOLVED check
- [x] Lazy connect/disconnect on subscribe/unsubscribe
- [x] Error handling: fn throws → `[[ERROR, err]]` downstream
- [x] `resubscribable` and `reset_on_teardown` options

### 0.4 — Concurrency

- [x] Thread-safe `get()` — any thread, any time (`_cache_lock` on `_cached`; independent of subgraph `RLock`)
- [x] Per-subgraph write locks via Union-Find (port from callbag-recharge-py)
- [x] Weak-ref registry auto-cleanup
- [x] `defer_set()` / `defer_down()` for safe cross-subgraph writes
- [x] Thread-local batch isolation
- [x] Free-threaded Python 3.14 compatibility (suite passes; optional `test_free_threaded_build_detected` when GIL disabled)

### 0.5 — Meta (companion stores)

- [x] `meta` option: each key becomes a subscribable node
- [x] Meta nodes participate in describe() output (via `describe_node` / `meta_snapshot`; full `Graph.describe()` in Phase 1.3)
- [x] Meta nodes independently observable

### 0.6 — Sugar constructors

- [x] `state(initial, opts?)` — no deps, no fn
- [x] `producer(fn, opts?)` — no deps, with fn
- [x] `derived(deps, fn, opts?)` — deps + fn (alias over `node`; spec “operator” pattern is the same primitive)
- [x] `effect(deps, fn)` — deps, fn returns nothing
- [x] `pipe(source, op1, op2)` — linear composition; `|` on `Node` per GRAPHREFLY-SPEC §6.1 (Python)
- [ ] `subscribe(dep, callback)` — omitted (match graphrefly-ts): use `node([dep], fn)` or `effect([dep], fn)`; instance `Node.subscribe` covers sink attachment
- [ ] `operator(deps, fn, opts?)` — omitted (match graphrefly-ts); use `derived`

### 0.7 — Tests & validation

- [x] Core node tests — source/initial, derived, wire (no-fn deps), producer-style fn, errors, resubscribe, two-phase ordering (`tests/test_core.py`)
- [x] Diamond resolution tests — classic diamond + many-deps bitmask stress
- [x] Batch tests — `tests/test_protocol.py` (deferral, nesting, terminals) + core batch exception path
- [x] Meta companion store tests — `test_meta_*`, `meta_snapshot`, TEARDOWN to meta (B3)
- [x] Lifecycle signal tests — TEARDOWN, COMPLETE, ERROR, unknown forward; INVALIDATE / PAUSE / RESUME through multi-node chains (`tests/test_core.py`)
- [x] Concurrency stress tests — `tests/test_concurrency.py` (get under write, independent subgraphs, merged serialization, defer, batch + lock)
- [x] Benchmarks / perf smoke — `tests/bench_core.py` (pytest-benchmark; parity with graphrefly-ts / callbag `compare.bench.ts` shapes); `tests/test_perf_smoke.py` (loose wall-time when `CI` unset; twin: graphrefly-ts `perf-smoke.test.ts`)

**Sugar tests:** `tests/test_sugar.py` (constructors, `pipe`, `|`, single-dep wire via `node([dep], fn)`).

---

## Phase 1: Graph Container

### 1.1 — Graph core

- [x] `Graph(name, opts?)` constructor
- [x] `graph.add(name, node)` / `graph.remove(name)`
- [x] `graph.get(name)` / `graph.set(name, value)` / `graph.node(name)`
- [x] `graph.connect(from_name, to_name)` / `graph.disconnect(from_name, to_name)`

### 1.2 — Composition

- [x] `graph.mount(name, child_graph)` — subgraph embedding
- [x] Colon-delimited namespace: `"parent:child:node"`
- [x] `graph.resolve(path)` — node lookup by qualified path
- [x] Lifecycle signal propagation through mount hierarchy

### 1.3 — Introspection

- [x] `graph.describe()` → JSON dict (nodes, edges, subgraphs, meta)
- [x] `graph.observe(name?)` → live message stream (`GraphObserveSource`)
- [x] Type inference in describe output (`describe_node` / `_infer_describe_type`; optional `describe_kind` still open in `docs/optimizations.md`)

### 1.4 — Lifecycle & persistence

- [x] `graph.signal(messages)` — broadcast to all nodes
- [x] `graph.destroy()` — TEARDOWN all
- [x] `graph.snapshot()` / `graph.restore(data)` / `Graph.from_snapshot(data)`
- [x] `graph.to_json()` — deterministic serialization

### 1.5 — Actor & Guard (access control)

Built-in ABAC at the node level. Replaces external authz libraries (e.g. CASL) — the graph is the single enforcement point.

- [ ] `Actor` type: `TypedDict` with `type` (`"human" | "llm" | "wallet" | "system" | str`), `id` (`str`), and extensible claims
- [ ] Actor context parameter on `down()`, `set()`, `signal()` — optional, defaults to `Actor(type="system")`
- [ ] `guard` node option: `(actor: Actor, action: Literal["write", "signal", "observe"]) -> bool` — checked on `down()`/`set()`/`signal()`; raises `GuardDenied` on rejection
- [ ] `policy()` declarative builder — CASL-style ergonomics without the dependency:
  ```python
  policy(lambda allow, deny: [
      allow("write",  where=lambda actor: actor["role"] == "admin"),
      allow("signal", where=lambda actor: actor["type"] == "wallet"),
      allow("observe"),  # open by default
      deny("write",  where=lambda actor: actor["type"] == "llm"),
  ])
  ```
- [ ] Scoped `describe(actor=)` / `observe(name=, actor=)` — filters output to nodes the actor may observe
- [ ] Attribution: each mutation records `{ actor, timestamp }` on the node (accessible via `node.last_mutation`)
- [ ] `meta.access` derived from guard when present (backward compat)
- [ ] `GuardDenied` exception with `actor`, `node`, `action` for diagnostics

### 1.6 — Tests

- [ ] Graph add/remove/connect/disconnect
- [ ] Mount and namespace resolution
- [ ] describe() output validation
- [ ] observe() message stream tests
- [ ] Snapshot round-trip tests
- [ ] Guard enforcement: allowed/denied writes, signals, observe filtering
- [ ] Policy builder: allow/deny precedence, wildcard, composed policies
- [ ] Actor attribution: mutation records, actor propagation through subgraphs
- [ ] Scoped describe: filtered output matches guard permissions
- [ ] GuardDenied exception: correct actor/node/action in diagnostics

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

### 5.2 — ORM Adapters

- [ ] SQLAlchemy ORM integration
- [ ] Django ORM integration
- [ ] Tortoise ORM integration

### 5.3 — Adapters

- [ ] `from_http`, `from_websocket` / `to_websocket`
- [ ] `from_webhook`, `to_sse`
- [ ] `from_mcp` (Model Context Protocol)

### 5.4 — LLM tool integration

- [ ] `knobs_as_tools(graph, actor=)` → OpenAI/MCP tool schemas from scoped describe()
- [ ] `gauges_as_context(graph, actor=)` → formatted gauge values for system prompts
- [ ] Graph builder validation

---

## Phase 6: Node Versioning

- [ ] V0: id + version (recommended minimum)
- [ ] V1: + cid + prev (content addressing, linked history)
- [ ] V2: + schema (type validation)
- [ ] V3: + caps (serialized guard policy) + refs (cross-graph references) — runtime enforcement already in Phase 1.5; V3 adds the serialization/transport format
- [ ] ~~Attribution~~ → Phase 1.5 (`node.last_mutation`)

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
