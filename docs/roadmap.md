# Roadmap

> **Spec:** `~/src/graphrefly/GRAPHREFLY-SPEC.md` (canonical; not vendored in this repo)
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
- [x] Behavioral spec read from `~/src/graphrefly/GRAPHREFLY-SPEC.md` only (no `docs/` copy)
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
- [x] `reachable(described, from_path, direction, *, max_depth=None)` — standalone utility: BFS over `describe()` output, walks `deps` (inverted for downstream) + explicit `edges` transitively. Returns sorted path list. Supports `max_depth` limit.

### 1.4 — Lifecycle & persistence

- [x] `graph.signal(messages)` — broadcast to all nodes
- [x] `graph.destroy()` — TEARDOWN all
- [x] `graph.snapshot()` / `graph.restore(data)` / `Graph.from_snapshot(data)`
- [x] `graph.to_json()` — deterministic serialization

### 1.4b — Seamless persistence

- [x] `graph.auto_checkpoint(adapter, opts?)` — debounced reactive persistence wired to `observe()`; trigger gate uses `message_tier` (`>=2`)
- [x] Incremental snapshots — diff-based persistence via `Graph.diff()`, periodic full snapshot compaction
- [x] Selective checkpoint filter — `{ filter: (name, described) => boolean }` to control which nodes trigger saves
- [x] `Graph.register_factory(pattern, factory)` — register node factory by name glob pattern for `from_snapshot` reconstruction
- [x] `Graph.unregister_factory(pattern)` — remove registered factory
- [x] `Graph.from_snapshot(data)` registry integration — auto-reconstruct dynamic graphs (runtime-added nodes) without `build` callback; topological dep resolution
- [x] `restore(data, only=...)` — selective restore (partial hydration by node name pattern)
- [x] Guard reconstruction from data — `policy_from_rules()` pattern for rebuilding guard functions from persisted policy rules

### 1.5 — Actor & Guard (access control)

Built-in ABAC at the node level. Replaces external authz libraries (e.g. CASL) — the graph is the single enforcement point.

- [x] `Actor` type: `TypedDict` with `type` (`"human" | "llm" | "wallet" | "system" | str`), `id` (`str`), and extensible claims
- [x] Actor context parameter on `down()`, `set()`, `signal()` — optional, defaults to `Actor(type="system")`
- [x] `guard` node option: `(actor: Actor, action: Literal["write", "signal", "observe"]) -> bool` — checked on `down()`/`set()`/`signal()`; raises `GuardDenied` on rejection
- [x] `policy()` declarative builder — CASL-style ergonomics without the dependency:
  ```python
  policy(lambda allow, deny: [
      allow("write",  where=lambda actor: actor["role"] == "admin"),
      allow("signal", where=lambda actor: actor["type"] == "wallet"),
      allow("observe"),  # open by default
      deny("write",  where=lambda actor: actor["type"] == "llm"),
  ])
  ```
- [x] Scoped `describe(actor=)` / `observe(name=, actor=)` — filters output to nodes the actor may observe
- [x] Attribution: each mutation records `{ actor, timestamp }` on the node (accessible via `node.last_mutation`)
- [x] `meta.access` derived from guard when present (backward compat)
- [x] `GuardDenied` exception with `actor`, `node`, `action` for diagnostics

### 1.6 — Tests

- [x] Graph add/remove/connect/disconnect (`tests/test_graph.py`)
- [x] Mount and namespace resolution (`tests/test_graph.py`)
- [x] describe() output validation (`tests/test_graph.py` — shape + `test_describe_includes_*`)
- [x] observe() message stream tests (`tests/test_graph.py` — single, graph-wide, mount prefix)
- [x] Snapshot round-trip tests (`tests/test_graph.py` — `from_snapshot`, `restore`, JSON identity)
- [x] Guard enforcement: allowed/denied writes, signals, observe filtering (`tests/test_guard.py`)
- [x] Policy builder: allow/deny precedence, wildcard, composed policies
- [x] Actor attribution: `Graph.set` + qualified mount paths (`tests/test_graph.py`); direct `down` (`tests/test_guard.py`)
- [x] Scoped describe: filtered output matches guard permissions
- [x] GuardDenied exception: correct actor/node/action in diagnostics

---

## Phase 2: Extra (Operators & Sources)

Port proven operators from callbag-recharge-py + new ones from TS.

### 2.1 — Tier 1 operators (sync, static deps)

- [x] `map`, `filter`, `scan`, `reduce`
- [x] `take`, `skip`, `take_while`, `take_until`
- [x] `first`, `last`, `find`, `element_at`
- [x] `start_with`, `tap`, `distinct_until_changed`, `pairwise`
- [x] `combine`, `merge`, `with_latest_from`, `zip`
- [x] `concat`, `race`

### 2.2 — Tier 2 operators (async, dynamic)

- [x] `switch_map`, `concat_map`, `exhaust_map`, `flat_map`
- [x] `debounce`, `throttle`, `sample`, `audit`
- [x] `delay`, `timeout`
- [x] `buffer`, `buffer_count`, `buffer_time`
- [x] `window`, `window_count`, `window_time`
- [x] `interval`, `repeat`
- [x] `pausable`, `rescue`

### 2.3 — Sources & sinks

- [x] `from_timer`, `from_cron`, `from_iter`, `from_any`
- [x] `from_awaitable`, `from_async_iter`
- [x] `first_value_from` (sync bridge)
- [x] `of`, `empty`, `never`, `throw_error`
- [x] `for_each`, `to_list`
- [x] `share`, `cached`, `replay` (`replay(..., buffer_size)` reserved; multicast replay TBD)

---

## Phase 3: Resilience & Data

### 3.1 — Utils (resilience)

- [x] `retry`, `backoff` (exponential, linear, fibonacci) — `graphrefly.extra.{backoff,resilience}`
- [x] `with_breaker` (circuit breaker) — `CircuitBreaker`, `WithBreakerBundle`
- [x] `rate_limiter`, `token_tracker` — sliding window + `TokenBucket`
- [x] `with_status` — `WithStatusBundle` (companion state nodes)
- [x] `checkpoint` + adapters — `save_graph_checkpoint` / `restore_graph_checkpoint`, memory / dict / file / SQLite adapters

### 3.1b — Reactive output consistency (no Promise/Future in public APIs)

Design invariant: every public function returns `Node[T]`, `Graph`, `None`, or a plain synchronous value — never `Awaitable` / `Future` for library-surface reactive coordination.

- [x] Public APIs avoid `async def` / `Awaitable` return types; async boundaries are wrapped as reactive sources (`from_awaitable`, `from_async_iter`, `from_any`)
- [x] Higher-order callback parameters accept sync scalar, `Node`, `Awaitable`, `Iterable`, and `AsyncIterable` via `from_any` coercion
- [x] `first_value_from` remains an end-user sync escape hatch only (not used internally in production code)
- [x] Checkpoint adapters are synchronous (`save -> None`, `load -> dict | None`)

### 3.2 — Data structures

- [x] `reactive_map` (KV with TTL, eviction) — `graphrefly.extra.reactive_map` / `ReactiveMapBundle`
- [x] `reactive_log` (append-only, reactive tail/slice) — `reactive_log`, `ReactiveLogBundle.tail`, `log_slice`
- [x] `reactive_index` (dual-key sorted index) — `reactive_index` / `ReactiveIndexBundle`
- [x] `reactive_list` (positional operations) — `reactive_list` / `ReactiveListBundle`
- [x] `pubsub` (lazy topic stores) — `pubsub` / `PubSubHub`

### 3.2b — Composite data patterns

Higher-order patterns composing Phase 0–3.2 primitives. No new core concepts — these wire `state`, `derived`, `effect`, `switch_map`, `reactive_map`, `dynamic_node`, and `from_any` into reusable shapes.

Design reference: `archive/docs/SKETCH-reactive-tracker-factory.md` (TS repo — canonical design)

- [x] `verifiable(source, verify_fn, opts?)` — value node + verification companion; fully reactive (no imperative trigger). `source: NodeInput[T]`, `verify_fn: (value: T) -> NodeInput[VerifyResult]`, trigger via `opts.trigger: NodeInput[Any]` or `opts.auto_verify`. Uses `switch_map` internally to cancel stale verifications.
- [x] `distill(source, extract_fn, opts)` — budget-constrained reactive memory store. Watches source stream, extracts via `extract_fn: (raw, existing) -> NodeInput[Extraction[TMem]]`, stores in `reactive_map`, evicts stale entries (reactive eviction via `dynamic_node`), optional consolidation, produces budgeted compact view ranked by caller-provided `score`/`cost` functions. LLM-agnostic — extraction and consolidation functions are pluggable.

---

## Phase 4: Domain Layers (Graph Factories)

Each returns a `Graph` — uniform introspection, lifecycle, persistence.

### 4.1 — Orchestration

- [x] `pipeline()` → Graph
- [x] `task()`, `branch()`, `gate()`, `approval()`
- [x] `for_each()`, `join()`, `loop()`, `sub_pipeline()`
- [x] `sensor()`, `wait()`, `on_failure()`
- [x] Mermaid/D2 diagram export

### 4.2 — Messaging

Pulsar-inspired messaging features for topic retention, cursor consumers, and queue workers.

- [x] `topic()` → Graph
- [x] `subscription()` (cursor-based consumer)
- [x] `job_queue()` → Graph
- [x] `job_flow()` → Graph
- [x] `topic_bridge()` (distributed sync)

### 4.3 — Memory

- [x] `collection()` → Graph
- [x] `light_collection()` (FIFO/LRU)
- [x] `vector_index()` (HNSW)
- [x] `knowledge_graph()` → Graph
- [x] `decay()` scoring

### 4.4 — AI surface

- [ ] `chat_stream()` → Graph
- [ ] `agent_loop()` → Graph
- [ ] `from_llm()` (adapter)
- [ ] `tool_registry()` → Graph
- [ ] `agent_memory()` → Graph — composes `distill()` (3.2b) + tracker-specific scoring/eviction
- [ ] `llm_extractor(system_prompt, opts)` → `extract_fn` for `distill()` — handles structured and unstructured LLM output, deduplicates against existing memories
- [ ] `llm_consolidator(system_prompt, opts)` → `consolidate_fn` for `distill()` — clusters and merges related memories via LLM
- [ ] `system_prompt_builder()`

### 4.5 — CQRS

Composition layer over 3.2 (`reactive_log`), 4.1 (sagas), 4.2 (event bus), 4.3 (projections). Guards (1.5) enforce command/query boundary.

- [ ] `cqrs(name, definition)` → Graph — top-level factory
- [ ] `command(name, handler)` — write-only node; guard rejects `observe`
- [ ] `event(name)` — backed by `reactive_log`; append-only, immutable
- [ ] `projection(events, reducer)` — read-only derived node; guard rejects `write`
- [ ] `saga(events, handler)` — event-driven side effects (delegates to `pipeline()`)
- [ ] `event_store` adapter interface — pluggable persistence (in-memory, SQLite, Postgres)
- [ ] Projection rebuilding: replay events to reconstruct read models
- [ ] `describe()` output distinguishes command / event / projection / saga node roles

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

- [x] `from_http`, `from_websocket` / `to_websocket`
- [ ] `from_webhook`, `to_sse`
- [ ] `from_mcp` (Model Context Protocol)
- [ ] `from_fs_watch(paths, opts?)` — file system watcher as reactive source; debounced, glob include/exclude, recursive. Uses `watchfiles` (or `watchdog`). Cleanup closes watchers on unsubscribe.
- [ ] `from_git_hook(repo_path, opts?)` — git change detection as reactive source; emits structured `GitEvent` (commit, files, message, author). Default: polling via `git log --since`; opt-in hook script installation.

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

- [x] README
- [ ] `llms.txt` for AI agent discovery
- [ ] PyPI publish: `graphrefly-py`
- [ ] Docs site
- [ ] Benchmarks vs RxPY, manual state, asyncio patterns
- [ ] Free-threaded Python 3.14 benchmark suite
- [ ] Community launch

### 7.1 — Showcase demos (Pyodide/WASM lab)

Python demos run in Pyodide/WASM lab on the graphrefly-py docs site. Each mirrors the TS demo's graph logic (same topology, Python APIs) with a simplified visual layer. Detailed ACs in `graphrefly-ts/docs/demo-and-test-strategy.md`.

- [ ] **Demo 1: Order Processing Pipeline** — 4.1 + 4.2 + 4.5 + 1.5 (Pyodide, headless + text output)
- [ ] **Demo 2: Multi-Agent Task Board** — 4.1 + 4.3 + 4.4 + 3.2b + 1.5 (Pyodide, mock LLM)
- [ ] **Demo 3: Real-Time Monitoring Dashboard** — 4.1 + 4.2 + 4.3 + 3.1 + 3.2 (Pyodide)
- [ ] **Demo 4: AI Documentation Assistant** — 4.3 + 4.4 + 3.2b + 3.2 + 3.1 (Pyodide, mock LLM)

### 7.2 — Scenario tests (headless demo logic)

Each demo has a headless scenario test mirroring TS demo AC lists — no UI, mock LLM.

- [ ] `tests/scenarios/test_order_pipeline.py`
- [ ] `tests/scenarios/test_agent_task_board.py`
- [ ] `tests/scenarios/test_monitoring_dashboard.py`
- [ ] `tests/scenarios/test_docs_assistant.py`

### 7.3 — Inspection stress & adversarial tests

- [ ] `describe()` consistency during batch drain
- [ ] `observe()` structured/causal/timeline correctness under concurrent updates
- [ ] `Graph.diff()` performance on 500-node graphs
- [ ] `to_mermaid()` output validity
- [ ] `trace_log()` ring buffer wrap correctness
- [ ] Cross-factory composition: mounted subgraphs don't interfere
- [ ] Guard bypass attempts (`.down()` without actor)
- [ ] `snapshot()` during batch drain (consistent, never partial)
- [ ] `subscription()` added mid-drain (correct offset)
- [ ] `collection()` eviction during derived read (no stale refs)
- [ ] Thread-safety: concurrent factory composition under per-subgraph locks

### 7.4 — Foreseen building blocks (to be exposed by demos)

Items expected to emerge during demo implementation. Validate need, then add to the appropriate phase.

- [ ] **Reactive cursor** (shared by `subscription()` + `job_queue()`) — cursor advancing through `reactive_log`; likely 3.2 primitive
- [ ] **Streaming node convention** — partial value emission for `chat_stream()`/`from_llm()` token-by-token output
- [ ] **Factory composition helper** — shared pattern for 4.x graph factory boilerplate
- [ ] **Guard-aware describe for UI** — `describe(show_denied=True)` variant showing hidden nodes with `{ denied: True, reason }` for UI display
- [ ] **Mock LLM fixture system** — `mock_llm(responses)` adapter for `from_llm()` replaying deterministic canned responses
- [ ] **Time simulation** — `monotonic_ns()` test-mode override for integration with `from_timer`/`from_cron`/`wait`

---

## Effort Key

| Size | Meaning |
|------|---------|
| **S** | Half day or less |
| **M** | 1-2 days |
| **L** | 3-4 days |
| **XL** | 5+ days |
