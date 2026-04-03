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

- [x] `chat_stream()` → Graph
- [x] `agent_loop()` → Graph
- [x] `from_llm()` (adapter)
- [x] `tool_registry()` → Graph
- [x] `agent_memory()` → Graph — `distill()` + store/compact/size; optional LLM hooks; in-factory composition of `knowledge_graph` / `vector_index` / `light_collection` / `decay` / `auto_checkpoint`
- [x] In-factory composition: opt-in via `vector_dimensions`/`embed_fn`, `enable_knowledge_graph`/`entity_fn`, `tiers` dict
- [x] 3D admission filter: `admission_filter_3d(score_fn, persistence_threshold, personal_value_threshold, require_structured)` — pluggable into `admission_filter`
- [x] 3-tier storage: permanent (`light_collection`, `permanent_filter`), active (with `decay()` scoring + `max_active`), archived (`auto_checkpoint` adapter)
- [x] Default retrieval pipeline: vector search → knowledge_graph adjacency expansion → decay ranking → budget packing — reactive derived node via `retrieve(query)`
- [x] Default reflection: periodic LLM consolidation via built-in `consolidate_trigger` from `from_timer(interval)` when `consolidate_fn` provided
- [x] Memory observability: `retrieval_trace` node captures pipeline stages (vector_candidates, graph_expanded, ranked, packed)
- [x] `llm_extractor(system_prompt, opts)` → `extract_fn` for `distill()` — handles structured and unstructured LLM output, deduplicates against existing memories
- [x] `llm_consolidator(system_prompt, opts)` → `consolidate_fn` for `distill()` — clusters and merges related memories via LLM
- [x] `system_prompt_builder()`

### 4.5 — CQRS

Composition layer over 3.2 (`reactive_log`), 4.1 (sagas), 4.2 (event bus), 4.3 (projections). Guards (1.5) enforce command/query boundary.

- [x] `cqrs(name, definition)` → Graph — top-level factory
- [x] `command(name, handler)` — write-only node; guard rejects `observe`
- [x] `event(name)` — backed by `reactive_log`; append-only, immutable
- [x] `projection(events, reducer)` — read-only derived node; guard rejects `write`
- [x] `saga(events, handler)` — event-driven side effects
- [x] `event_store` adapter interface — pluggable persistence (in-memory default)
- [x] Projection rebuilding: replay events to reconstruct read models
- [x] `describe()` output distinguishes command / event / projection / saga node roles

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
- [x] `from_webhook`, `to_sse`
- [x] `from_mcp` (Model Context Protocol)
- [x] `from_fs_watch(paths, opts?)` — file system watcher as reactive source; debounced, glob include/exclude, recursive. Uses `watchdog`. Cleanup closes watchers on unsubscribe.
- [x] `from_git_hook(repo_path, opts?)` — git change detection as reactive source; emits structured `GitEvent` (commit, files, message, author). Default: polling via `git log --since`; opt-in hook script installation.

### 5.3b — Ingest adapters (universal source layer)

Connectors for the universal reduction layer (Phase 8). Each wraps an external protocol/system as a reactive `producer` node.

- [ ] `from_otel(opts)` — OTLP/HTTP receiver; accepts traces, metrics, logs as nodes. Bridges `opentelemetry-sdk` SpanProcessor/MetricReader/LogEmitterProvider.
- [ ] `from_syslog(opts)` — RFC 5424 syslog receiver (UDP/TCP)
- [ ] `from_statsd(opts)` — StatsD/DogStatsD UDP receiver
- [ ] `from_prometheus(endpoint, opts)` — scrape Prometheus /metrics as reactive source
- [ ] `from_kafka(topic, opts)` / `to_kafka(topic, opts)` — Kafka consumer/producer (via `confluent-kafka` or `aiokafka`)
- [ ] `from_redis_stream(key, opts)` / `to_redis_stream(key, opts)` — Redis Streams
- [ ] `from_csv(path, opts)` / `from_ndjson(stream)` — file/stream ingest for batch replay
- [ ] `from_clickhouse_watch(query, opts)` — live materialized view as reactive source (via `clickhouse-connect`)

### 5.3c — Storage & sink adapters

- [ ] `to_clickhouse(table, opts)` — buffered batch insert sink
- [ ] `to_s3(bucket, opts)` — object storage sink (Parquet/NDJSON, partitioned; via `boto3`)
- [ ] `to_postgres(table, opts)` / `to_mongo(collection, opts)` — document/relational sink
- [ ] `to_loki(opts)` / `to_tempo(opts)` — Grafana stack sinks
- [ ] `checkpoint_to_s3(bucket, opts)` — graph snapshot persistence to object storage
- [ ] `checkpoint_to_redis(prefix, opts)` — fast checkpoint for ephemeral infra

### 5.4 — LLM tool integration

- [x] `knobs_as_tools(graph, actor=)` → OpenAI/MCP tool schemas from scoped describe()
- [x] `gauges_as_context(graph, actor=)` → formatted gauge values for system prompts
- [x] Graph builder validation
- [x] `graph_from_spec(natural_language, adapter, opts)` → LLM composes a Graph from natural language; validates topology; returns runnable graph
- [x] `suggest_strategy(graph, problem, adapter)` → LLM analyzes current graph + problem, suggests operator/topology changes

---

## Phase 6: Node Versioning

Design reference: `archive/docs/SESSION-serialization-memory-footprint.md`, `~/src/graphrefly-ts/archive/docs/SESSION-serialization-memory-footprint.md`.

### 6.0 — V0: id + version (done)

- [x] Wire `create_versioning(0, ...)` into `node()` when `versioning` provided
- [x] `advance_version()` on every DATA handled in local lifecycle (derived skips bump via RESOLVED when unchanged)
- [x] `describe_node()` includes `{ id, version }` when V0 active
- [x] `graph.snapshot()` / describe path includes per-node `v` when versioning enabled
- [x] `Graph.diff()` uses version counters to skip unchanged nodes — O(changes) not O(graph size)
- [x] `graph.set_versioning(level)` — default versioning level for new nodes in this graph

#### 6.0b — V0 backfill (post-implementation)

- [x] **Phase 5.4** (LLM tool integration): `gauges_as_context()` delta by version; `knobs_as_tools()` include version for conflict detection — plus Appendix B / describe schema updates for optional `v` as needed

### 6.1 — V1: + cid + prev (content addressing, linked history)

- [x] V1: + cid + prev (content addressing, linked history)
- [ ] Lazy CID computation — computed on first access after value change, not on every DATA

### 6.2 — V2: + schema (type validation)

- [ ] V2: + schema (type validation at node boundaries)

### 6.3 — V3: + caps + refs (serialized capabilities, cross-graph references)

- [ ] V3: + caps + refs — runtime enforcement already in Phase 1.5; V3 adds serialization/transport format
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

### 7.1 — Reactive layout engine (Pretext-on-GraphReFly)

Reactive text measurement and layout without DOM thrashing. Inspired by [Pretext](https://github.com/chenglou/pretext) but rebuilt as a GraphReFly graph — the layout is inspectable (`describe()`), snapshotable, and debuggable. Standalone reusable pattern; graphrefly-ts wires the same engine into the three-pane demo shell (TS roadmap §7.2). Python uses it for server/CLI and Pyodide/docs demos (§7.2 below). Design reference: `graphrefly-ts/docs/demo-and-test-strategy.md` §2b.

Two-tier DX: out-of-the-box `reactive_layout(adapter, *, text=..., font=..., line_height=..., max_width=...)` for common cases; advanced `MeasurementAdapter` protocol for custom content types and environments.

#### Text layout (Pretext parity)

- [x] `MeasurementAdapter` protocol: `measure_segment(text, font) → {"width": float}`, optional `clear_cache()` — pluggable measurement backend; tests use deterministic mock adapters
- [x] `state("text")` → `derived("segments")` — text segmentation; adapter `measure_segment()` for segment widths, cached per `dict[font, dict[segment, width]]` — **TS:** `Intl.Segmenter` word granularity; **Py:** Unicode `\w` word-token runs + grapheme merging (same merge/break pipeline; rare boundary differences vs ECMA-402)
- [x] Text analysis pipeline (ported from Pretext): whitespace normalization, word segmentation, punctuation merging, CJK per-grapheme splitting, URL/numeric runs as word tokens (Py `\w` / TS `Intl` word-like segments), soft-hyphen/hard-break support
- [x] `derived("line-breaks")` — segments + max-width → greedy line breaking (no DOM): trailing-space hang, `overflow-wrap: break-word` via grapheme widths, soft hyphens, hard breaks
- [x] `derived("height")`, `derived("char-positions")` — total height, per-character `{x, y, width, height}` for hit testing
- [x] Measurement cache with RESOLVED optimization — unchanged text/font → no re-measure
- [x] `meta: { cache-hit-rate, segment-count, layout-time-ns }` for observability
- [x] `reactive_layout(adapter, *, text, font, line_height, max_width)` → `ReactiveLayoutBundle` — convenience factory

#### MeasurementAdapter implementations (pluggable backends)

- [x] `PillowMeasureAdapter` (default, server) — Pillow `ImageFont.getlength()`, font cache; optional dep
- [x] `PrecomputedAdapter` (server/snapshot) — reads from pre-computed metrics dict, zero measurement at runtime; per-char fallback or strict error mode
- [x] `CliMeasureAdapter` (terminal) — monospace cell counting via `unicodedata.east_asian_width()` (CJK/fullwidth = 2 cells), configurable `cell_px`, no external deps

#### Multi-content blocks (SVG, images, mixed)

- [x] `reactive_block_layout(adapters, *, blocks=..., max_width=..., gap=...)` — mixed content layout: text + image + SVG blocks with per-type measurement (**TS:** `reactiveBlockLayout({ adapters, ... })` options object)
- [x] `SvgBoundsAdapter` — viewBox/width/height parsing from SVG string (pure regex, no DOM)
- [x] `ImageSizeAdapter` — pre-registered dimensions by src key (sync lookup)
- [x] Block flow algorithm: vertical stacking with configurable gap, purely arithmetic over child sizes

#### Standalone extraction

- [ ] Extractable as standalone pattern (`reactive-layout`) independent of demo shell

### 7.2 — Showcase demos (Pyodide/WASM lab)

Python demos run in Pyodide/WASM lab on the graphrefly-py docs site. Each mirrors the TS demo's graph logic (same topology, Python APIs) with a simplified visual layer. Detailed ACs in `graphrefly-ts/docs/demo-and-test-strategy.md`.

- [ ] **Demo 1: Order Processing Pipeline** — 4.1 + 4.2 + 4.5 + 1.5 (Pyodide, headless + text output)
- [ ] **Demo 2: Multi-Agent Task Board** — 4.1 + 4.3 + 4.4 + 3.2b + 1.5 (Pyodide, mock LLM)
- [ ] **Demo 3: Real-Time Monitoring Dashboard** — 4.1 + 4.2 + 4.3 + 3.1 + 3.2 (Pyodide)
- [ ] **Demo 4: AI Documentation Assistant** — 4.3 + 4.4 + 3.2b + 3.2 + 3.1 (Pyodide, mock LLM)

### 7.2b — Universal reduction demos

Demos exercising Phase 8 reduction layer patterns. Design reference: `~/src/graphrefly-ts/archive/docs/SESSION-universal-reduction-layer.md`.

- [ ] **Demo 5: Observability Pipeline** — 5.3b + 8.1 + 8.4 + 3.2b (from_otel → stratify → LLM correlation → SLO verifiable → Grafana sink)
- [ ] **Demo 6: AI Agent Observatory** — 4.4 + 8.1 + 8.4 (instrument agent_loop with full tracing → LLM distills "why agent went off-track")
- [ ] **Demo 7: Log Reduction Pipeline** — 5.3b + 8.1 + 8.2 (from_syslog 10K lines/sec → 4-layer reduction → 5 prioritized items/minute)

### 7.3 — Scenario tests (headless demo logic)

Each demo has a headless scenario test mirroring TS demo AC lists — no UI, mock LLM.

- [ ] `tests/scenarios/test_order_pipeline.py`
- [ ] `tests/scenarios/test_agent_task_board.py`
- [ ] `tests/scenarios/test_monitoring_dashboard.py`
- [ ] `tests/scenarios/test_docs_assistant.py`

### 7.4 — Inspection stress & adversarial tests

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

### 7.5 — Foreseen building blocks (to be exposed by demos)

Items expected to emerge during demo implementation. Validate need, then add to the appropriate phase.

- [ ] **Reactive cursor** (shared by `subscription()` + `job_queue()`) — cursor advancing through `reactive_log`; likely 3.2 primitive
- [ ] **Streaming node convention** — partial value emission for `chat_stream()`/`from_llm()` token-by-token output
- [ ] **Factory composition helper** — shared pattern for 4.x graph factory boilerplate
- [ ] **Guard-aware describe for UI** — `describe(show_denied=True)` variant showing hidden nodes with `{ denied: True, reason }` for UI display
- [ ] **Mock LLM fixture system** — `mock_llm(responses)` adapter for `from_llm()` replaying deterministic canned responses
- [ ] **Time simulation** — `monotonic_ns()` test-mode override for integration with `from_timer`/`from_cron`/`wait`

---

## Phase 8: Universal Reduction Layer (Info → Action)

Reusable patterns for taking heterogeneous massive inputs and producing prioritized, auditable, human-actionable output. Every pattern is a Graph factory — uniform introspection, lifecycle, persistence. Design reference: `~/src/graphrefly-ts/archive/docs/SESSION-universal-reduction-layer.md` (canonical), `archive/docs/SESSION-universal-reduction-layer.md` (Python companion).

### 8.1 — Reduction primitives

Composable building blocks between sources and sinks.

- [ ] `stratify(source, rules)` → Graph — route input to different reduction branches based on classifier fn. Each branch gets independent operator chains. Rules are reactive — an LLM can rewrite them at runtime.
- [ ] `funnel(sources, stages)` → Graph — multi-source merge with sequential reduction stages. Each stage is a named subgraph. Stages are pluggable — swap a stage by graph composition.
- [ ] `feedback(graph, condition, reentry)` → Graph — introduce a cycle: when condition node fires, route output back to reentry point. Bounded by max iterations + budget constraints.
- [ ] `budget_gate(source, constraints)` → Node — pass-through respecting reactive constraint nodes (token budget, network IO, cost ceiling). Backpressure via PAUSE/RESUME.
- [ ] `scorer(sources, weights)` → Node — reactive multi-signal scoring. Weights are nodes (LLM or human can adjust live). Output: sorted, prioritized items with full score breakdown in meta.

### 8.2 — Domain templates (opinionated Graph factories)

Pre-wired graphs for common "info → action" domains. Users fork/extend.

- [ ] `observability_graph(opts)` → Graph — OTel ingest → stratified reduction → correlation → SLO verification → alert prioritization → sink
- [ ] `issue_tracker_graph(opts)` → Graph — findings → extraction → verifiable assertions → regression detection → distillation → prioritized queue
- [ ] `content_moderation_graph(opts)` → Graph — ingest → LLM classification → human review → feedback → policy refinement
- [ ] `data_quality_graph(opts)` → Graph — DB/API ingest → schema validation → anomaly detection → drift alerting → remediation suggestions

### 8.3 — LLM graph composition

- [ ] `GraphSpec` schema — JSON schema for declarative graph topology. Serializable, diffable.
- [ ] `compile_spec(spec)` → Graph — instantiate from spec
- [ ] `decompile_graph(graph)` → GraphSpec — extract spec from running graph
- [ ] `llm_compose(problem, adapter, opts)` → GraphSpec — LLM generates topology from natural language
- [ ] `llm_refine(graph, feedback, adapter)` → GraphSpec — LLM modifies existing topology
- [ ] `spec_diff(spec_a, spec_b)` — structural diff between specs

### 8.4 — Audit & accountability

- [ ] `audit_trail(graph, opts)` → Graph — wraps graph with reactive_log recording every mutation, actor, timestamp, causal chain
- [ ] `explain_path(graph, from_node, to_node)` — walk backward to explain derivation. Human + LLM readable.
- [ ] `policy_enforcer(graph, policies)` — reactive constraint enforcement. Policies are nodes. Violations → alert subgraph.
- [ ] `compliance_snapshot(graph)` — point-in-time export for regulatory archival

### 8.5 — Performance & scale

- [ ] Backpressure protocol — formalize PAUSE/RESUME for throughput control across graph boundaries
- [ ] `peer_graph(transport, opts)` — federate graphs across processes/services (WebSocket, gRPC, NATS, Redis pub/sub)
- [ ] Benchmark suite: 10K nodes, 100K msgs/sec. Target: <1ms p99 per hop.
- [ ] `sharded_graph(shard_fn, opts)` — partition across `multiprocessing` workers. Transparent to consumers.
- [ ] Adaptive sampling — adjusts sample rate from downstream backpressure + budget constraints
- [ ] Free-threaded Python 3.14 benchmark for parallel reduction branches

---

## Effort Key

| Size | Meaning |
|------|---------|
| **S** | Half day or less |
| **M** | 1-2 days |
| **L** | 3-4 days |
| **XL** | 5+ days |
