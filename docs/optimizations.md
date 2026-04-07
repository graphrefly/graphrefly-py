# Optimizations and Open Decisions

## Resolved design decisions (gateway + SSE, 2026-04-03)

- **`ObserveGateway._default_send` removed (noted 2026-04-02, resolved 2026-04-03 — option (a)):** `_default_send` silently dropped unawaited coroutines for Starlette/FastAPI WebSocket (`send_text` is a coroutine). Resolved: remove `_default_send`; `send` is now a required parameter on `ObserveGateway`. A `starlette_send(ws)` convenience helper is provided that correctly wraps the coroutine via the channel-based architecture below. Usage: `ObserveGateway(graph, send=starlette_send(ws))`. Pre-1.0 — no backward compat.

- **`ObserveGateway` thread safety + `WatermarkController` cross-thread SSE (noted 2026-04-02, resolved 2026-04-03 — option (c) channel-based):** `sink()` fires from the graph propagation thread; `handle_message`/`handle_disconnect` from the async event loop thread. Resolved: channel-based architecture — graph thread pushes to a `queue.SimpleQueue`, event loop thread drains. No shared mutable dicts. The `WatermarkController` moves entirely to the consumer (event loop) side, eliminating cross-thread state. Solves both gateway thread safety and watermark cross-thread issues in one pass. No polling — queue drain uses `call_soon_threadsafe` or equivalent async bridge.

- **`_sse_frame` made public (noted 2026-04-02, resolved 2026-04-03):** Renamed `_sse_frame` → `sse_frame` and exported from `graphrefly.extra`. The integration module now imports the public symbol. Pre-1.0, no backward compat.

- **Graph-wide SSE per-node COMPLETE semantics (noted 2026-04-02, resolved 2026-04-03 — option (b) explicit done signal):** Add optional `done: Node[bool]` to graph-wide SSE options. When the done node emits `DATA(True)`, the SSE stream sends a final `complete:*` frame and terminates. Reactive (no polling), explicit (no heuristic), composable — the user derives the done signal from any graph state. Fallback: `TEARDOWN` via `graph.destroy()` still terminates as before.

- **`_normalize_graphs` key naming (noted 2026-04-02, resolved 2026-04-03):** Single `Graph` maps to `"default"` (unchanged). Multiple positional `Graph` arguments now raise `TypeError` with guidance to use an explicit dict instead. Eliminates the ambiguous `"0"`, `"1"` keys and silent `get_graph()` failures.

## Resolved design decisions (streaming + AI lifecycle)

- **Backpressure guard integration (resolved 2026-04-02):** `GraphObserveSource.up()` now catches `GuardDenied` and silently drops flow-control messages (PAUSE/RESUME) rather than raising into the watermark controller callback. Aligned with TS `GraphObserveOne.up()` / `GraphObserveAll.up()` which both catch `GuardDenied` and return silently. Non-`GuardDenied` exceptions still propagate.

- **WatermarkController factory pattern (resolved 2026-04-02):** `create_watermark_controller(send_up, opts)` factory function returns a `WatermarkController` (Protocol). Lock IDs use `object()` (unforgeable identity, like TS `Symbol`). Messages are `list` to match the `Messages` type alias. Cross-language parity: TS `createWatermarkController`.

- **Gateway backpressure (resolved 2026-04-02):** `_observe_sse_response` accepts `high_water_mark` / `low_water_mark` options. `ObserveGateway` class manages per-client WebSocket subscriptions with backpressure via client `ack` command (clamped to [0, 1024]). Default `low_water_mark = high_water_mark // 2`. Cross-language parity: TS `observeSSE`, `observeSubscription`, `ObserveGateway`.

- **`from_llm_stream` / `fromLLMStream` teardown (Phase 4.4, resolved 2026-04-02):** TS `fromLLMStream` now returns `LLMStreamHandle { node, dispose }`. PY `from_llm_stream` (when implemented) should follow the same bundle pattern `{ node, dispose }`.

- **CQRS terminal state handling (Phase 4.5, resolved 2026-04-02):** `dispatch()` / `_append_event` now raises `RuntimeError` if the event stream node is terminal (`completed`/`errored`). Fail-fast guard prevents silent data loss.

- **CQRS dispatch-time persistence (Phase 4.5, resolved 2026-04-02):** `persist()` is now sync-only. `MemoryEventStore.persist_sync` removed (persist is sync). Adapters with async I/O buffer internally and expose optional `async flush()`. Cross-language: TS `persist` is also sync-only.

- **CQRS saga vs projection event ordering (Phase 4.5, resolved 2026-04-02):** Keep the intentional split. Projections globally sort by `(timestamp_ns, seq)`. Sagas use per-stream causal ordering (new tail entries since last run). Different semantics for read models vs side-effect delivery.

- **CQRS event store `since` / replay cursor (Phase 4.5, resolved 2026-04-02):** Opaque `EventStoreCursor` (dict). `load_events` returns `LoadEventsResult(events, cursor)`. `MemoryEventStore` uses `(timestamp_ns, seq)` tuple comparison for filtering. Cross-language: same API shape and cursor key names.

- **Cross-language V1 cid parity (Phase 6, resolved 2026-04-02):** `canonicalize_for_hash` normalizer applied before JSON serialization. Integer-valued floats to ints. Non-finite rejected. Integers outside the safe range (`|n| > 2^53-1`) are rejected — JS and Python serialize large integers differently (`1e+21` vs decimal), so cid parity is only guaranteed within safe integer bounds. No external deps.

- **`_versioning_level` field (Phase 6, resolved 2026-04-02):** Field deleted from `NodeImpl` `__slots__`. Inlined as local in `__init__`. `Graph._default_versioning_level` retained (actively used).

- **Streaming token delivery (Phase 4.4, resolved 2026-03-31 — option (a) `reactive_log` internally):** `from_llm_stream(adapter, messages)` returns `Node[ReactiveLogSnapshot[str]]`, accumulating tokens via `reactive_log` internally. This reuses the existing Phase 3.2 data structure; `tail()` / `log_slice()` give natural windowed views; fully reactive (no polling); `describe()` / `observe()` / inspector work out of the box. Rejected alternatives: (b) `DATA` with `{ partial, chunk }` — loses composability and version dedup; (c) `stream_from` pattern — premature abstraction for a single use case.

- **Retrieval pipeline reactivity model (Phase 4.4, resolved 2026-03-31 — option (b) persistent derived node):** `agent_memory`'s retrieval is a persistent derived node that re-runs when store, query, or context change. The `query` input is a `state` node updated via `retrieve(query)`. This fits GraphReFly's reactive model: memory context auto-updates as conversation evolves. Rejected: (a) method that creates a derived node on-demand — doesn't compose reactively, loses `observe()` introspection.

- **`agent_memory` in-factory composition scope (Phase 4.4, resolved 2026-03-31):** All primitives (`vector_index`, `knowledge_graph`, `light_collection`, `decay`, `auto_checkpoint`) are opt-in via options. Vector + KG indexing happens in a reactive `effect` that observes the distill store. Tier classification runs in a separate `effect`. This avoids monolithic coupling while keeping the factory ergonomic. Cross-language parity: TS uses `fromTimer(ms)` for reflection trigger, PY uses `from_timer(seconds)`.

- **Reactive layout (roadmap §7.1, resolved 2026-03-31):** Cross-language parity target is **same graph shape + shared tests for ASCII/Latin** (not full ICU / `Intl` segmentation parity). Whitespace normalization matches TypeScript: collapse `[\t\n\r\f ]+` to a single ASCII space, then strip **at most one** leading and one trailing ASCII space (Python does **not** use full-Unicode :meth:`str.strip`). The ``segments`` meta field **``cache-hit-rate``** is ``hits / (hits + misses)`` over ``measure_segment`` / ``measureSegment`` lookups in that recompute (``1.0`` when there were zero lookups). Companion meta **``DATA``** for layout metrics is delivered **after** the parent ``segments`` node settles via batch phase-3 (TS: ``emitWithBatch(..., 3)``, PY: ``emit_with_batch(..., phase=3)``). **``MeasurementAdapter``:** ``clear_cache`` / ``clearCache`` is optional on both sides.
- **Reactive layout meta timing parity (Group 3 — FYI):** Both ports defer meta companion emissions to **batch phase-3** (TS: `emitWithBatch(..., 3)`, PY: `emit_with_batch(..., phase=3)`). Phase-3 drains only after all phase-2 work (parent node settlements) completes, guaranteeing meta values arrive after the parent `segments` DATA has propagated.
- **Reactive layout intentional divergences (Group 4 — FYI):** TS CLI width heuristics use hard-coded codepoint ranges and a terminal-like approximation of East Asian width/combining marks, while Py uses `unicodedata.east_asian_width()` + Unicode `category()` (so rare/ambiguous codepoints may differ). TS feeds the layout pipeline using `Intl.Segmenter` word segmentation, while Py seeds it via regex tokenization (full ICU parity is not guaranteed; current parity tests target ASCII/Latin). Runtime backend sets differ by environment: TS canvas/Node-canvas adapters vs Python Pillow-based measurement.
- **Reactive layout max-width clamp (Phase 7.1, resolved 2026-04-02):** ``reactive_layout`` / ``reactive_block_layout`` (PY) and ``reactiveLayout`` / ``reactiveBlockLayout`` (TS) clamp the ``max-width`` state to ≥ 0 on factory init and on ``set_max_width`` / ``setMaxWidth``, so negative values cannot break image/SVG scaling.

- **Default node versioning ``id`` (Phase 6, resolved 2026-03-31):** Auto-generated V0/V1 ids are **RFC 4122 UUID strings with hyphens**, matching ``crypto.randomUUID()`` (TypeScript) and ``str(uuid.uuid4())`` (Python). For stable cross-session ids, set ``versioningId`` / ``versioning_id`` explicitly.

## Implementation anti-patterns

Cross-cutting rules for reactive/async integration (especially `patterns.ai`, LLM adapters, and tool handlers). **Keep this table identical in both repos’ `docs/optimizations.md`.**

| Anti-pattern | Do this instead | Spec ref |
|--------------|-----------------|----------|
| **Polling** | Do not busy-loop on `node.get()` or use ad-hoc timers to poll for completion. Wait for protocol delivery: `subscribe` / `first_value_from` / `firstValueFrom` patterns on the node produced by `fromAny` / `from_any`. | §5.8 |
| **Imperative triggers** | Do not use event emitters, callbacks, or `threading.Timer` + manual `set()` to trigger graph behavior. All coordination uses reactive `NodeInput` signals and message flow through topology. If you need a trigger, create a reactive source node. | §5.9 |
| **Raw Promises / microtasks (TS)** | Do not use bare `Promise`, `queueMicrotask`, `setTimeout`, or `process.nextTick` to schedule reactive work. Async boundaries belong in sources (`fromPromise`, `fromAsyncIter`) and the runner layer. | §5.10 |
| **Raw async primitives (PY)** | Do not use bare `asyncio.ensure_future`, `asyncio.create_task`, `threading.Timer`, or raw coroutines for reactive work. Async boundaries belong in sources (`from_awaitable`, `from_async_iter`) and the runner layer. | §5.10 |
| **Non-central time** | Do not schedule periodic work with raw `setTimeout` / `setInterval` / `time.sleep` for graph-aligned sampling. Use `fromTimer` / `from_timer` (or other documented `extra` time sources) and compose reactively. Use `monotonic_ns()` for event ordering, `wall_clock_ns()` for attribution. | §5.11 |
| **Hardcoded message type checks** | Do not hardcode `if type == MessageType.DATA` for checkpoint or batch gating. Use `message_tier` utilities for tier classification. | §5.11 |
| **Bypassing `fromAny` / `from_any` for async** | Do not one-off `asyncio.run`, bare `.then` chains, or manual thread sleeps to bridge coroutines / async iterables / Promises into the graph. Route unknown shapes through `fromAny` / `from_any` so `DATA` / `ERROR` / `COMPLETE` stay consistent end-to-end. | §5.10 |
| **Leaking protocol internals in Phase 4+ APIs** | Domain-layer APIs (orchestration, messaging, memory, AI, CQRS) must never expose `DIRTY`, `RESOLVED`, bitmask, or settlement internals in their primary surface. Use domain language. Protocol access available via `.node()` or `inner`. | §5.12 |
| **`Node` resolution without `get()`** | When blocking until first `DATA`, prefer `node.get()` when it already holds a settled value, then subscribe only if still pending — avoids hangs when the node does not replay `DATA` to new subscribers. | — |
| **Passing plain strings through `fromAny` (TypeScript)** | `fromAny` treats strings as iterables (one `DATA` per character). For tool handlers that return plain strings, return the string directly; use `fromAny` only for `Node` / `AsyncIterable` / Promise-like after await. | — |

## Resolved design decisions (compat adapters)

- **Compat adapter write semantics (resolved 2026-03-31):** Framework adapter setters **must forward** `DATA` when payload is `None`/`undefined`-equivalent (for optional payload nodes). `None` is a valid `T`; dropping writes silently loses data.
- **Compat adapter lifecycle semantics (resolved 2026-03-31):** Subscriptions **remain mounted until framework teardown**, not auto-disposed on terminal messages (`COMPLETE`/`ERROR`). The framework owns lifecycle, not the protocol.
- **Compat adapter record-sync semantics (resolved 2026-03-31):** Keyed record subscriptions **ignore phase-1 `DIRTY` waves** and only resubscribe on settled phase-2 key updates (`DATA`/`RESOLVED`). `DIRTY` is transient; acting on it causes unnecessary churn.
# Optimizations

`graphrefly-py` prioritizes protocol correctness and parity with `graphrefly-ts`, with Python-specific encoding (e.g. `StrEnum` message tags, unlimited `int` bitmasks). This document tracks built-in optimizations and cross-language notes in a format aligned with `graphrefly-ts/docs/optimizations.md`.

---

## Built-in optimizations

### 1. Output slot model (`None` → single sink → `set`)

Node subscriptions use tiered storage:

- `None` when no downstream subscribers
- a single callback for one subscriber
- a `set` only when fan-out exceeds one

### 2. Batch phase split (`DIRTY` immediate, `DATA` / `RESOLVED` deferred)

`batch()` and `emit_with_batch()` preserve two-phase semantics. Delivery supports two **strategies** (see §2 in cross-language notes):

- **`strategy="partition"`** — TS-style split of one payload into immediate vs phase-2 blocks (used by `NodeImpl.down`).
- **`strategy="sequential"`** — ordered per-tuple walk with terminal flush between messages (default; used by `dispatch_messages` alias).

### 3. Diamond settlement via bitmask

Multi-dep nodes use a Python `int` bitmask (unlimited precision; TS uses `int` + `Uint32Array` for large fan-in).

### 4. Lazy upstream connect / disconnect

Same as TS: subscribe on first downstream sink; disconnect when the last sink leaves.

### 5. Single-dependency DIRTY skip

Aligned with TS: `SubscribeHints(single_dep=True)` and sole-subscriber detection.

### 6. Connect-order guard (`_connecting`)

While subscribing upstream deps, `run_fn` is suppressed for re-entrant dep emissions until **all** deps are wired, then one initial `run_fn` runs. Prevents `dep.get()` being `None` mid-connect (see cross-language §6).

### 7. Per-subgraph write locks (roadmap 0.4)

Union-find over node identity merges components when nodes list dependencies at construction (`union_nodes(self, dep)` and meta companions). A per-component `RLock` serializes `down`, recompute (`_run_fn`), and subscribe/unsubscribe topology changes. `get()` uses a **per-node** `threading.Lock` (`_cache_lock`) for all reads and writes of `_cached`, so the cached value is thread-safe under free-threaded Python (nogil) without holding the subgraph write lock (callers can `get()` from any thread without participating in graph write serialization). Weak references remove GC’d nodes from registry maps; a reverse-children map (`_children`) keeps GC cleanup O(1) per node (no linear scan of `_parent`). `lock_for` retries up to `_MAX_LOCK_RETRIES` (100) if concurrent unions invalidate the acquired lock, then raises `RuntimeError`. `defer_set` / `defer_down` queue cross-subgraph mutations until the current `acquire_subgraph_write_lock_with_defer` exits; errors use `ExceptionGroup` when multiple callbacks fail. Deferred batch phase-2 work re-acquires the emitter’s lock via `emit_with_batch(..., subgraph_lock=node)` so `batch()` drains do not deliver DATA/RESOLVED without serialization.

### 8. Actor & guard (roadmap 1.5)

- **`policy()` precedence:** any matching **deny** blocks the action even if a broader **allow** also matches.
- **Scoped `describe(actor=…)`:** omit nodes the actor cannot **observe**; drop **edges** touching a hidden endpoint; keep a **subgraph** mount prefix only if some visible path equals it or starts with `mount::` (roadmap D).
- **`Graph.signal`:** `GuardDenied` aborts the walk on first rejection; nodes visited earlier in the traversal may already have received the message (non-transactional). Prefer guards that allow **system** `signal` when using `destroy()` / lifecycle if you need guaranteed completion.
- **`Graph.remove` / `_teardown_mounted_graph`:** after the registry drops a name, primary `TEARDOWN` uses `Node.down(..., internal=True)` so a guard cannot reject teardown **after** unregister (would orphan the node instance).

## Open design decisions

- **~~Sink adapter silent error swallowing (Phase 5.2b–5.2d, noted 2026-04-04, resolved 2026-04-04):~~** All per-record sinks now return a `SinkHandle` (or `BufferedSinkHandle`) with a reactive `errors` companion node. The `_create_sink_error_handler()` factory always writes to the errors node; if a user callback is also provided, it fires first. `dispose()` sends `TEARDOWN` to the errors node. Mirrors TS implementation. Re-entrant batch drain protection applied via `contextlib.suppress(Exception)` around `errors_node.down()`.
- **~~TS buffered sinks missing ERROR flush (Phase 5.3c, noted 2026-04-04, resolved 2026-04-04):~~** Both TS and PY buffered sinks now use `messageTier(msg[0]) >= 3` / `message_tier(msg[0]) >= 3` to flush on any terminal (COMPLETE, ERROR, TEARDOWN). Previously both used hardcoded `COMPLETE || TEARDOWN` checks and silently dropped buffered data on upstream ERROR.
- **~~Synchronous SQLite blocking in `to_sqlite` sink (Phase 5.2b, noted 2026-04-04, resolved 2026-04-04):~~** Both TS and PY now accept `batch_insert` / `batchInsert` (default `False`) and `max_batch_size` / `maxBatchSize` (default `1000`). DATA values are buffered and flushed inside a `BEGIN`/`COMMIT` transaction on terminal messages (`message_tier >= 3`), at `max_batch_size` threshold, or on `dispose()`. On insert error, the first error triggers `break` + `ROLLBACK`. BEGIN failure preserves pending data for retry via manual `flush()`. Both return `BufferedSinkHandle` when batch mode is enabled.
- **CheckpointAdapter refactored to key-value (Phase 3.1c, noted 2026-04-05, resolved 2026-04-05):** `CheckpointAdapter` changed from blob-based (`save(data)` / `load()`) to key-value (`save(key, data)` / `load(key)` / `clear(key)`). Unifies checkpoint persistence and cache storage under one interface. Both TS and PY updated. `save_graph_checkpoint` / `restore_graph_checkpoint` pass `graph.name` as key. `auto_checkpoint` passes `self.name` as key. `FileCheckpointAdapter` now takes a directory (one file per key, sanitized filenames). `DictCheckpointAdapter` no longer takes an internal key in constructor. `SqliteCheckpointAdapter` no longer takes a fixed key — caller provides it. Pre-1.0, all downstream consumers updated, no legacy shims.
- **`tiered_storage` uses `CheckpointAdapter[]` directly (Phase 3.1c, noted 2026-04-05, resolved 2026-04-05):** With key-value `CheckpointAdapter`, `tiered_storage` wraps adapters as `CacheTier`s naturally. No separate `CacheTier` interface needed for end users — `CheckpointAdapter` serves both checkpoint and cache use cases.
- **~~`equals` must never see undefined/None (all phases, noted 2026-04-05, resolved 2026-04-05):~~** Superseded by `_SENTINEL` / `NO_VALUE` sentinel below. Original fix used `_has_emitted_data` flag; the sentinel approach replaced both.
- **~~`_cached is None` sentinel is ambiguous with `[(DATA, None)]` and INVALIDATE (noted 2026-04-05, resolved 2026-04-05):~~** Superseded by `_SENTINEL` / `NO_VALUE` sentinel below.
- **~~Derived node error observability gap (all phases, noted 2026-04-05, resolved 2026-04-05):~~** `fn`/`equals`/`on_message` errors now wrapped with `RuntimeError(f'Node "{name}": fn threw')` / `equals threw` / `on_message threw` with `__cause__`. Both TS and PY.
- **~~PY `retry` lock scope around `connect()` (Phase 3.1, noted 2026-04-05, resolved 2026-04-05):~~** Verified that `connect()` already runs outside the lock. Added clarifying comment.
- **~~PY `SqliteCheckpointAdapter` default `check_same_thread=True` (Phase 3.1, noted 2026-04-05, resolved 2026-04-06):~~** Applied `check_same_thread=False` + `threading.Lock` wrapping all `execute` / `commit` calls. Safe for `auto_checkpoint` debounce timer threads calling `save()` while main thread calls `load()`.
- **~~Payload-less DATA spec note (Phase 5.2–5.3, noted 2026-04-05, resolved 2026-04-05):~~** Spec clarified: payload-less DATA (`(DATA,)`) is not valid per protocol — all DATA tuples must carry a payload. Sink `msg[1] if len(msg) > 1 else None` coercion retained as defensive guard; upstream producers are the source of truth.
- **~~Sink `dispose()` error routing (Phase 5.2–5.3, noted 2026-04-05, resolved 2026-04-05):~~** All 20 PY sink `dispose()` functions now route errors to the `errors` companion node before suppressing.
- **~~`from_django_orm` and `from_tortoise` have identical implementations (Phase 5.2, noted 2026-04-05, resolved 2026-04-05):~~** Extracted `_from_sync_rows` helper. Both entry points kept with distinct validation.
- **~~Auto-edge registration from constructor deps (all phases, noted 2026-04-05, resolved 2026-04-05):~~** `Graph.add()` now auto-registers edges from constructor deps. Eliminates dual-bookkeeping bug surface.
- **~~ResettableTimer primitive (Phase 3.1, noted 2026-04-05, resolved 2026-04-05):~~** `ResettableTimer` in `core/timer.py`. `retry`, `rate_limiter`, `timeout` refactored to use it.
- **~~`to_json()` → `to_dict()` + `to_json_string()` (Phase 1.4, noted 2026-04-05, resolved 2026-04-05):~~** PY: `to_json()` renamed to `to_json_string()`; `to_dict()` added as alias of `snapshot()`; `to_json` alias removed (pre-1.0, no backward compat needed). TS: `toJSON()` renamed to `toObject()`; `toJSON()` kept as ECMAScript hook. Spec §3.8 updated.
- **~~Initial value, cached state, and equals interaction (all phases, noted 2026-04-05, resolved 2026-04-05):~~** Resolved with `_SENTINEL` / `NO_VALUE` sentinel (TS: `Symbol.for("graphrefly/NO_VALUE")`, PY: `_SENTINEL = object()`). Replaces the `_has_emitted_data` boolean flag entirely. One field (`_cached`) instead of two — impossible to desync. Key semantics: (1) When `initial` option is present (even as `None`), `_cached = initial` — `equals` IS called on first emission. (2) When `initial` is absent, `_cached = _SENTINEL` — first emission always DATA. (3) INVALIDATE / `reset_on_teardown` set `_cached = _SENTINEL`. (4) Resubscribable: terminal reset now also sets `_cached = _SENTINEL` — new subscriber always gets DATA. (5) Reconnect: cache retained → same-value emits RESOLVED — correct. (6) `get()` returns `None` when `_cached is _SENTINEL`. Spec §2.5 updated.
- **Auto-edge registration is local-only (Phase 1.1, noted 2026-04-05 — design note, not an open decision):** `Graph.add()` auto-registers edges for deps within the same `Graph` instance only. Cross-subgraph deps still require explicit `connect()`. Consistent with spec: cross-subgraph edges are explicit wiring.
- **Core `bridge()` helper (Phase 8.2, noted 2026-04-06, QA 2026-04-06):** Both TS and PY export `bridge(from, to, opts?)` / `bridge(from_node, to_node, *, name, down)` from `core/bridge`. Creates a graph-visible effect node that intercepts messages from `from` via `on_message` and forwards to `to.down()`. TS updated in QA: default forwards **all standard types** (DATA, DIRTY, RESOLVED, COMPLETE, ERROR, TEARDOWN, PAUSE, RESUME, INVALIDATE). Terminal types always cause bridge self-termination regardless of `down` filter. `funnel()` explicitly excludes TEARDOWN from inter-stage bridges. `DEFAULT_DOWN` exported for callers. **PY needs update** to match: (1) `DEFAULT_DOWN` should include all standard types, (2) terminal self-termination regardless of `down`, (3) `funnel()` should exclude TEARDOWN explicitly.
- **GraphSpec cross-language parity (Phase 8.3, noted 2026-04-06, QA 2026-04-06):** Both TS and PY implement `compileSpec`/`compile_spec`, `decompileGraph`/`decompile_graph`, `llmCompose`/`llm_compose`, `llmRefine`/`llm_refine`, `specDiff`/`spec_diff`, and `validateSpec`/`validate_spec` in `patterns/graphspec`. Key alignment: (1) `GraphSpec` schema is identical JSON shape — `nodes`, `templates`, `feedback` top-level keys. TS uses TypeScript types; PY uses TypedDict. (2) `compile_spec` resolves nodes in dependency order (state/producer first, then derived/effect/operator). Catalog is passed explicitly (`GraphSpecCatalog`) — no global registry. (3) Template instantiation creates mounted subgraphs via `graph.mount()`. `$param` bindings resolve to top-level nodes. (4) Feedback edges wire via §8.1 `feedback()` primitive. (5) `decompile_graph` uses `describe(detail="standard")`, skips `__meta__` and `__feedback_*` internal nodes. Template detection via meta-based recovery (primary) + structural fingerprinting (fallback; includes dep names for accuracy). (6) `spec_diff` is pure JSON comparison — template-aware, feedback-aware. (7) LLM APIs (`llm_compose`/`llm_refine`) share identical system prompt and validation pipeline. (8) `validate_spec` checks bind targets exist in outer nodes, rejects feedback self-cycles, validates template param completeness. QA fixes: idempotent unsub in feedback(), deterministic output node selection in decompile, `contextlib.suppress` for connect dedup. Both repos: 35 tests each.
- **~~Feedback bare DATA to reentry/counter — deferred to 8.2 (Phase 8.1, noted 2026-04-06, resolved 2026-04-06):~~** Resolved in 8.2. `feedback()` now uses a graph-registered effect node (`__feedback_effect_<condition>`) that intercepts condition messages via `on_message` and forwards to reentry/counter. The effect participates in two-phase push naturally. Old subscribe-based bridge removed. Both TS and PY.
- **`llm_compose`/`llm_refine` sync (PY) vs async (TS) — intentional divergence (Phase 8.3, noted 2026-04-06):** PY: synchronous, adapter must return `LLMResponse` directly. TS: `async function` returning `Promise<GraphSpec>`. PY design invariant: no `async def` / `Awaitable` in public APIs. TS spec §5.10 allows `await` at system boundaries (LLM adapter is external I/O, not reactive scheduling). Both are correct for their language idiom. Adapter contracts differ: PY adapters must be sync; TS adapters return Promise-compatible values.
- **Domain templates cross-language parity (Phase 8.2, noted 2026-04-06):** Both TS and PY implement `observability_graph`/`observabilityGraph`, `issue_tracker_graph`/`issueTrackerGraph`, `content_moderation_graph`/`contentModerationGraph`, `data_quality_graph`/`dataQualityGraph` in `patterns/domain_templates` / `patterns/domain-templates`. Key alignment: (1) All four use source injection (option B) — user passes a source node, template wires the topology. (2) Same well-known node names across both. (3) Null guard on derived fns: PY returns `None`, TS returns `undefined` — both skip computation when source hasn't emitted real data. (4) PY `derived` fn signature is `(vals, actions)` vs TS `(vals) => ...`. (5) Options: PY uses `@dataclass`, TS uses interface types. (6) Meta keys: `domain_template: True`, `template_type: "<name>"`. Both repos: TS 24 tests, PY 21 tests.
- **Intentional cross-language divergences (Phase 8.2, noted 2026-04-06 parity):** (A) Bridge `down` option type: TS `readonly symbol[]`, PY `Sequence[MessageType] | None` — idiomatic to each language. (B) `BridgeOptions`: TS exports a named type alias, PY uses keyword args on the function — Pythonic kwargs pattern. (C) `batch()` syntax: TS callback `batch(() => { ... })`, PY context manager `with batch(): ...` — per spec §6.1. (E) `ScoredItem`: TS is a plain object / type alias, PY is a class with `__slots__`, `__eq__`, `__repr__` — PY more ergonomic for debugging; runtime payloads identical.
- **~~`content_moderation_graph` review accumulator read-modify-write (Phase 8.2, noted 2026-04-06 QA, resolved 2026-04-06):~~** Replaced `state([])` + read-modify-write accumulator with `reactive_log([], max_size=opts.max_queue_size)`. O(1) `append()` replaces O(n) copy-on-write. Optional `max_queue_size` option bounds queue growth. Feedback condition reads from `Versioned` snapshot entries. `data_quality_graph` baseline updater retains `batch()` wrapping (single value, not append-only). Both TS and PY.
- **`data_quality_graph` baseline updater re-entrant `down()` (Phase 8.2, noted 2026-04-06 QA, patched 2026-04-06):** The `__baseline_updater` effect calls `baseline.down()` during its own compute, which triggers synchronous downstream propagation into `drift_node` and `output_node` while the source update cycle is still in progress. QA patch: wrapped `baseline.down()` in `with batch():` to defer propagation. Same pattern as review accumulator above. TS baseline updater also patched for parity.
- **~~`budget_gate` `buffer.pop(0)` is O(n) (Phase 8.1, noted 2026-04-06 QA, resolved 2026-04-06):~~** Replaced `list` with `collections.deque`; `pop(0)` → `popleft()`.
- **~~`stratify` `pending_dirty` not reset on TEARDOWN (Phase 8.1, noted 2026-04-06 QA, resolved 2026-04-06):~~** Added `MessageType.TEARDOWN` to the terminal handler in `on_message` that clears `pending_dirty`.
- **~~Bridge `_DEFAULT_DOWN` excludes PAUSE/RESUME (Phase 8.2, noted 2026-04-06 QA, resolved 2026-04-06 parity):~~** Resolved: `DEFAULT_DOWN` now includes all 9 standard types (DATA, DIRTY, RESOLVED, COMPLETE, ERROR, TEARDOWN, PAUSE, RESUME, INVALIDATE), matching TS. `funnel()` explicitly excludes TEARDOWN via `down` filter. Renamed from `_DEFAULT_DOWN` to `DEFAULT_DOWN` (public export).
- **~~Unbounded review queue growth in `content_moderation_graph` (Phase 8.2, noted 2026-04-06 QA, resolved 2026-04-06):~~** Resolved by replacing `state` with `reactive_log`. See review accumulator entry above.
- **~~Bridge/feedback node detection in `decompile_graph` uses naming convention (Phase 8.2, noted 2026-04-06 QA, resolved 2026-04-06):~~** `bridge()` and `feedback()` effect nodes now carry `meta={"_internal": True}`. `decompile_graph` checks `meta._internal` first (robust, convention-independent), with legacy name-prefix fallback. Both TS and PY.
- **Feedback effect perpetually dirty status (Phase 8.1/8.2, noted 2026-04-06 QA — documented, accepted):** The `__feedback_effect_<condition>` node forwards DIRTY via default dispatch but consumes DATA. The effect stays in "dirty" status between emissions. **By design** — the status is technically correct. Effects don't need to settle. These nodes now carry `meta._internal: True` and can be filtered from `describe()` output when undesired.
- **Reduction primitives cross-language parity (Phase 8.1, noted 2026-04-06, QA 2026-04-06, updated 8.2 2026-04-06):** Both TS and PY implement `stratify`, `funnel`, `feedback`, `budget_gate`/`budgetGate`, and `scorer` in `patterns/reduction`. All five follow the orchestration factory pattern (`_base_meta`, `_register_step`). Key alignment: (1) `stratify` buffers DIRTY until DATA arrives — on classifier miss, emits `[DIRTY, RESOLVED]` to preserve spec §1.3.1 (both). (2) `funnel` now uses `bridge()` from `core/bridge` for inter-stage wiring — graph-visible effect nodes (`__bridge_<from>→<stage>_input`) replace the old subscribe-based bridges. Visible in `describe()`, participates in two-phase push. (3) `feedback` counter node is source of truth (resettable via `graph.set()`); feedback wiring now uses a graph-registered effect node (`__feedback_effect_<condition>`) instead of raw subscribe. Counter name is `__feedback_<condition>` to support multiple loops per graph. (4) `budget_gate`/`budgetGate` force-flushes all buffered items on terminal regardless of budget; sends RESUME before terminal if paused; forwards constraint ERROR downstream, silences constraint COMPLETE, forwards unknown constraint types via default. (5) `scorer` coerces `None`/`undefined` to 0 before multiplication (no TypeError/NaN divergence). TS `ScoredItem` is a plain object; PY `ScoredItem` is a class with `__slots__` and `__eq__`. Meta keys: `reduction: True`, `reduction_type: "<name>"`. Both repos: 22 tests each.

- **`stratify` two-dep gating (Phase 8.1/9.0, noted roadmap, resolved 2026-04-06, QA 2026-04-06):** `stratify` `on_message` previously intercepted only source messages (dep 0) and let rules messages (dep 1) fall through to default bitmask handling. When both source and rules updated in the same `batch()`, source DATA was classified against stale rules (rules DATA hadn't drained yet), and a spurious second emission occurred from default settlement of dep 1. Fix: `on_message` now intercepts **all** messages from both deps, implementing manual two-dep gating (same diamond-resolution pattern as `budget_gate`). Classification deferred until all dirty deps settle. Rules-only changes produce no downstream emission ("future items only"). QA hardening: (1) PAUSE/RESUME/INVALIDATE from rules dep swallowed (internal impl detail, not leaked downstream). (2) `classify` exceptions caught and treated as non-match (branch not killed). (3) `complete_when_deps_complete=False` explicit (branch lifecycle driven by source terminal, not auto-complete from both deps). Both TS and PY.

- **Cleanup wrapper `{"cleanup": fn, "value"?: v}` (all phases, noted 2026-04-06, resolved 2026-04-06):** Node fn can now return `{"cleanup": callable, "value": v}` to explicitly separate cleanup from data. When returned: `cleanup` is registered; if `"value"` key is present, it's emitted as data. Plain callable returns remain cleanup for backward compat. Exported as `CleanupResult<T>` (TS) / documented dict pattern (PY). Resolves the documented limitation where returning a callable as data was silently consumed as cleanup.

---

## Cross-language parity fixes (2026-04-05)

- **~~PY error wrapping missing original message (resolved 2026-04-05):~~** `RuntimeError(f'Node "{name}": fn threw')` omitted the original error message. TS included it: `fn threw: ${errMsg}`. Fixed all 3 PY sites (`fn threw`, `equals threw`, `on_message threw`) to include `{err}`. TS `onMessage` error was also not wrapped — now wraps as `Node "${name}": onMessage threw: ${errMsg}` with `{ cause }`.
- **~~PY `Graph.add()` missing reverse edge scan (resolved 2026-04-05):~~** PY only did forward auto-edge registration. TS did both forward + reverse (existing nodes whose deps include the new node). Fixed: PY now does the reverse scan, matching TS.
- **~~PY `_emit_sequential` terminal routing (resolved 2026-04-05):~~** PY appended terminals to the same queue as DATA/RESOLVED. TS routed them to `pendingPhase3`. Fixed: PY terminals now go to `bs.pending_phase3`.
- **~~PY `emit_with_batch` default strategy (resolved 2026-04-05):~~** PY defaulted to `"sequential"`, TS to `"partition"`. Fixed: PY default changed to `"partition"`.
- **~~PY `retry` sink missing `stopped` guard (resolved 2026-04-05):~~** TS checked `if (stopped) return;` at top of retry subscribe callback. PY had no such guard. Fixed: added `if stopped[0]: return`.
- **~~PY `fallback` plain-value path used `down()` (resolved 2026-04-05):~~** TS used `a.emit(fb)` (goes through equals + DIRTY/DATA wrapping). PY used `actions.down([(DATA, fb), (COMPLETE,)])` (bypassed equals). Fixed: PY now uses `actions.emit(fb)` then `actions.down([(COMPLETE,)])`.

## Cross-language implementation notes

**Keep this section in sync with `graphrefly-ts/docs/optimizations.md` § Cross-language implementation notes** so you can open both files side by side.

- **~~Dual-bookkeeping of derived deps + connect edges (all phases, noted 2026-04-05, resolved 2026-04-05):~~** `Graph.add()` now auto-registers edges from constructor deps in both TS and PY. Eliminates the divergence risk between node-level deps and graph-level edge registry. All graph factories no longer need explicit `connect()` calls for constructor deps.

- **Resilience composition parity (Phase 3.1c, 2026-04-05):** Both TS and PY implement `fallback(source, fb)`, `timeout(source, timeoutNs/timeout_ns)`, `cache(source, ttlNs/ttl_ns)`, `cascadingCache`/`cascading_cache`, and `tieredStorage`/`tiered_storage`. `CacheEvictionPolicy`/`EvictionPolicy` use aligned method names: `insert`/`touch`/`delete`/`evict(count)`/`size()`. `CacheTier`: `load` is required; `save`/`clear` are optional (TS `?` modifier; PY uses `hasattr` checks). `tieredStorage`/`tiered_storage` returns a wrapper with `.cache` property exposing the inner `CascadingCache` in both languages. Eviction demotes to deepest tier with `save` before removing — value is preserved in cold storage. Cache miss sentinel: TS `undefined`, PY `None`. `TimeoutError`: TS extends `Error`; PY subclasses `builtins.TimeoutError` so `except TimeoutError` catches both. Validation: TS `RangeError`, PY `ValueError` — language convention. `cache()` replay emits raw `DATA` (no DIRTY/RESOLVED) on both sides. §5.10 timer exception: `timeout`, `retry`, `rateLimiter`/`rate_limiter` now use `ResettableTimer` (`core/timer.py` in PY, `core/timer.ts` in TS) — a reusable primitive extracted from the repeated `setTimeout`/`threading.Timer` pattern.
- **Progressive disclosure parity (Phase 3.3b, 2026-04-04):** Both TS and PY `describe()` support `detail` (`"minimal"` / `"standard"` / `"full"`) and `fields` (GraphQL-style field selection with dotted meta paths like `"meta.label"`). Default changed from returning all fields to `"minimal"` (type + deps only) in both languages — pre-1.0, no backward compat concern. `format: "spec"` / `format="spec"` forces minimal fields. Both return an `expand()` method on the result for re-reading with higher detail. TS `expand` is a property on the result object; PY `describe()` returns a `DescribeResult` (dict subclass) with `expand()` as a method — `json.dumps(graph.describe())` works safely in both languages (TS `JSON.stringify` drops functions; PY `expand` is not a dict key). `observe()` supports `detail` with the same three levels: `"minimal"` (DATA events only in events list, counts still tracked), `"standard"` (current behavior), `"full"` (implies structured + timeline + causal + derived). `ObserveResult` has `expand()` in both languages. TS `expand` is a method on the result object; PY `expand` is a method on the `ObserveResult` dataclass. Internal callers (`snapshot`, `auto_checkpoint`, `dump_graph`, AI pattern functions) explicitly pass the detail level they need. Diagram methods (`to_mermaid`/`toMermaid`, `to_d2`/`toD2`) only use paths/edges so minimal default is fine — intentional, no detail option needed. `"standard"` includes versioning (`v`); `"full"` adds `guard` and `last_mutation`/`lastMutation` (runtime attribution, not restored by `restore()`). `snapshot()` / `auto_checkpoint` strip `last_mutation`/`guard` from persisted nodes so snapshot → restore → snapshot is idempotent — use `describe(detail="full")` for audit snapshots that include attribution. Unrecognized `detail` strings silently fall back to `"minimal"` — no runtime validation; we'll revisit if real-world usage shows this causes confusion. Dict/object filters (`meta_has`, `status`, etc.) operate on whatever fields the chosen detail level provides; at `"minimal"`, fields like `meta` and `status` are absent, so filters that depend on them silently exclude all nodes. Users should pass `detail="standard"` or higher when using these filters. Spec Appendix B `status` field changed from `required` to optional (schema applies at `detail >= "standard"`).
- **SQLite adapter parity (Phase 5.2b, 2026-04-04):** Both TS and PY use duck-typed `SqliteDbLike` with a `query(sql, params)` method — matching the `PostgresClientLike`/`ClickHouseClientLike` convention. TS `SqliteDbLike.query()` returns `unknown[]`; PY `SqliteDbLike.query()` returns `list[Any]`. Both are fully synchronous (no Promises/async). `from_sqlite`/`fromSqlite` is one-shot (DATA per row, then COMPLETE); compose with `switch_map` + `from_timer` for periodic re-query. `to_sqlite`/`toSqlite` follows per-record sink pattern (same as `to_postgres`/`toPostgres`). Default insert SQL uses JSON column; custom `to_sql` override available. TS uses `node:sqlite` `DatabaseSync` or `better-sqlite3`; PY uses stdlib `sqlite3` — both zero-dep from GraphReFly's perspective (user provides instance).
- **Storage & sink adapter pattern parity (Phase 5.2d, 2026-04-04):** All 5.2d sinks follow the same pattern in both TS and PY: duck-typed client protocols, `on_message` intercepting `DATA`, `SinkTransportError` for serialize/send failures. All sinks return a `SinkHandle` with `dispose()` + `errors: Node[SinkTransportError | None]`. `dispose()` sends `TEARDOWN` to the errors node. Buffered sinks (`to_clickhouse`, `to_s3`, `to_file`, `to_csv`) return a `BufferedSinkHandle` adding `flush()`. TS `SinkHandle` has optional `flush?: ...`; PY uses separate `BufferedSinkHandle` dataclass (intentional — more Pythonic). Checkpoint adapters (`checkpoint_to_s3`, `checkpoint_to_redis`) wire `graph.auto_checkpoint()`. PY uses `threading.Timer` for flush timers; TS uses `setTimeout`. PY `to_postgres` calls `client.execute(sql, params)` (psycopg2/3 style); TS calls `client.query(sql, params)` (pg style). PY `json.dumps` includes spaces after separators; TS `JSON.stringify` does not — NDJSON output is semantically equivalent but not byte-identical across languages.

### 1. Message type wire encoding

| | |
|--|--|
| **Python** | `StrEnum` string tags (`"DATA"`, …) — JSON/interop friendly. |
| **TypeScript** | `Symbol.for("graphrefly/…")` — avoids string collisions. |

Same logical protocol; encoding differs by language.

### 2. Unified batch delivery (`emit_with_batch` / `emitWithBatch`)

| | |
|--|--|
| **Python** | One implementation: `emit_with_batch(sink, messages, *, strategy=..., defer_when=...)`. `dispatch_messages(messages, sink)` is a thin alias for sequential delivery with `defer_when="batching"`. Node uses `strategy="partition"`, `defer_when="depth"`. |
| **TypeScript** | `emitWithBatch` matches Python **`partition` + `defer_when="depth"`** (defer only while `batchDepth > 0`). There is no separate sequential/terminal-interleaved mode in TS today. |

### 3. What “batching” means (`is_batching` / `isBatching`)

| | |
|--|--|
| **Python** | `is_batching()` is true while inside `batch()` **or** while deferred phase-2 work is draining (`flush_in_progress`). The **`defer_when=”batching”`** path defers DATA/RESOLVED in both cases — needed for nested-batch-inside-drain QA (same lesson as `callbag-recharge-py` batch + defer ordering). |
| **TypeScript** | `isBatching()` is true while `batchDepth > 0` **or** while `flushInProgress` (draining deferred work). Aligned with Python semantics. |

Both languages now defer phase-2 messages during the drain loop, preventing ordering issues when deferred callbacks trigger further emissions.

**Nested-batch error + drain:** see §7 — do not clear the global phase-2 queue on a nested `batch` throw while the outer drain is active.

### 4. `up` / `unsubscribe` on source nodes

| | |
|--|--|
| **Spec** | Source nodes have no upstream. |
| **TypeScript** | `up` / `unsubscribe` are absent on sources (`?` optional on the type). |
| **Python** | Same methods exist but are **no-ops** when there are no deps (single concrete type / ergonomics). |

### 5. Cleanup vs return value from `fn` (callable detection)

Both ports treat “`fn` returned a callable” as a **cleanup** (TS: `typeof out === "function"`). Returning a non-cleanup callable as a normal computed value remains ambiguous in both.

### 6. Re-entrant recompute while wiring upstream (multi-dep connect)

| | |
|--|--|
| **Python** | `_connecting` flag around the upstream `subscribe` loop: `run_fn` is not run from dep-driven handlers until wiring finishes, then one explicit `run_fn`. Fixes ordering where the first dep emits before the second subscription is installed (`dep.get()` still `None`). |
| **TypeScript** | `connecting` flag mirrors Python's `_connecting`. `runFn` bails early while `connecting` is true; the flag is set/cleared with try/finally around the subscribe loop. One explicit `runFn()` runs after all deps are wired. Root cause class matches lessons from **`callbag-recharge-py`** connect/batch ordering. |

### 7. Nested `batch` throw while draining — queue ownership (**decision A4**)

**Decision:** When a nested `batch()` exits with an error and `batch_depth` returns to **0** while deferred phase-2 work is **still draining** (`flush_in_progress` / `flushInProgress`), implementations **must not** discard the **global** pending phase-2 backlog. Only clear that backlog for a `batch` frame that owns it **outside** an in-flight outer drain.

| | |
|--|--|
| **Rationale** | A `batch(() => …)` invoked from inside a drain callback must not wipe deferrals registered by the outer batch pass (ordering bug + lost `DATA`/`RESOLVED`). |
| **TypeScript** | In the `batchDepth === 0 && threw` branch: run `pendingPhase2.length = 0` (or equivalent) **only if** `!flushInProgress`. |
| **Python** | Same invariant: never clear the process-global phase-2 queue solely because a nested `batch` failed while the outer drain is active. Verify `batch()` / `emit_with_batch` teardown matches. |

### 8. Concurrency model (**Python vs TypeScript**)

| | |
|--|--|
| **Python** | Per-subgraph `RLock` + union-find + TLS defer queue; thread-local `batch()` state (spec §6.1); per-node `_cache_lock` for `get()` / `_cached`. |
| **TypeScript** | Single-threaded assumption; no subgraph lock layer in core today. |

### 9. `TEARDOWN` / `INVALIDATE` after terminal (`COMPLETE` / `ERROR`) — pass-through (**decision B3**)

**Decision:** The terminal gate on `down()` **does not apply** to **`TEARDOWN`** or **`INVALIDATE`**. For a non-resubscribable node that has already reached `COMPLETE` or `ERROR`, filter the incoming batch to **only** `TEARDOWN` and/or `INVALIDATE` tuples (drop co-delivered `DATA`, etc.); then:

1. Run **local lifecycle** for those tuples (`TEARDOWN`: meta, upstream disconnect, producer stop, etc.; `INVALIDATE`: cache clear, dep memo clear, optional `fn` cleanup — see §12).
2. **Forward the filtered tuples to downstream sinks**.

| | |
|--|--|
| **Rationale** | Same control-plane pattern as B3: `graph.destroy()` and post-terminal cache/UI invalidation must not be swallowed after `COMPLETE`/`ERROR`. |
| **TypeScript** | If `terminal && !resubscribable`, filter to `TEARDOWN` or `INVALIDATE` tuples only before early return. |
| **Python** | `NodeImpl.down`: `terminal_passthrough` = `TEARDOWN` or `INVALIDATE` only. |

### 10. Batch drain: partial apply before rethrow (**decision C1**)

**Decision:** Treat **best-effort drain** as the specified behavior: run **all** queued phase-2 callbacks with **per-callback** error isolation; surface the **first** error only **after** the queue is quiescent. Callers may observe a **partially updated** graph — this is **intentional** (prefer that to orphaned deferrals or fail-fast leaving dirty state). **Document** in module docstrings / spec prose; optional future knobs (`fail_fast`, `AggregateError`) are not required for parity.

| | |
|--|--|
| **Python** | Keep per-emission handling + `ExceptionGroup` (or first-error policy as chosen); document the partial-state contract explicitly. |
| **TypeScript** | JSDoc on `batch` / `drainPending` documents partial delivery + first error rethrown. |

### 11. `describe_node` / `describeNode` and read-only `meta`

| | |
|--|--|
| **Python** | `describe_node(n)` reads `NodeImpl` internals; `node.meta` is `MappingProxyType` (read-only mapping of companion nodes). |
| **TypeScript** | `describeNode(n)` uses `instanceof NodeImpl` to read class fields directly; `node.meta` is `Object.freeze({...})`. |
| **Shared** | `meta_snapshot` / `metaSnapshot` omit keys when a companion `get()` throws; same best-effort `type` inference for Appendix B entries; `Graph.describe()` aggregates slices (Python + TS Phase 1.3). |

### 12. `INVALIDATE` local lifecycle (**GRAPHREFLY-SPEC §1.2**)

**Decision:** On `INVALIDATE`, if the node has a registered **`fn` cleanup** (callable returned from `fn`), **run it once** and clear the registration; then clear the cached output (`_cached` / `_cached = undefined`) and drop the dep-value memo (`_last_dep_values` / `_lastDepValues`) so the next settlement cannot skip `fn` purely via unchanged dep identity. Do not schedule `fn` from the `INVALIDATE` handler itself (“don’t auto-emit”). **`INVALIDATE` also passes the post-terminal gate** together with `TEARDOWN` (§9).

| | |
|--|--|
| **Python** | `NodeImpl._handle_local_lifecycle` |
| **TypeScript** | `NodeImpl._handleLocalLifecycle` |

### 13. `Graph` Phase 1.1 (registry + edges)

| | |
|--|--|
| **Shared** | `connect` validates that the target node’s dependency list includes the source node (**reference identity**). Edges are **pure wires** (no transforms). `connect` is **idempotent** for the same `(from, to)` pair. |
| **disconnect** | Both ports **throw** if the edge was not registered. Dropping an edge does **not** remove constructor-time deps on the node (registry / future `describe()`). **See Resolved design decisions §C** (QA 1d #2). |
| **remove** | Unregisters the node, drops incident edges, sends **`[[TEARDOWN]]`** to that node. |
| **Python** | `Graph(..., {"thread_safe": True})` (default): registry uses an `RLock`; **`down([[TEARDOWN]])` runs after the lock is released** on `remove`. |
| **TypeScript** | No graph-level lock (single-threaded spec). |

### 14. `Graph` Phase 1.2 composition — parity (mount, `resolve`, `signal`)

**Path separator:** Both ports use `::` as the qualified-path separator (e.g. `"parent::child::node"`). Single `:` is allowed in graph names, node names, and mount names. Both ports forbid `::` in names.

**Aligned:** Both provide `mount`, `::` separated `resolve`, recursive `signal`, forbid `::` in local node and mount names, forbid mount versus node name collisions, reject self-mount and mount cycles, treat a path that ends on a subgraph (or continues past a leaf node) as an error, and:

- `remove(mount_name)` unmounts and sends TEARDOWN through the mounted subtree
- `node` / `get` / `set` accept `::` qualified paths
- `connect` / `disconnect` accept `::` qualified paths; same-owner edges stored on child graph, cross-subgraph edges on parent
- `add` rejects duplicate node instances (same reference registered under two names)
- `mount` rejects the same child `Graph` instance mounted twice on one parent
- `edges()` public read-only listing of registered `(from, to)` pairs
- `signal` visit order: recurse into mounts first, then deliver to local nodes
- `resolve` strips leading graph name (e.g. `root.resolve("app::sub::x")` when `root.name == "app"`)
- Graph names may contain single `:` (both ports reject `::` in graph names)

**Remaining intentional divergence:**

| Topic | Python | TypeScript | Rationale |
|-------|--------|------------|-----------|
| `signal` node dedupe | No per-call dedupe (duplicate mount is forbidden, so unnecessary). | Shared `visited` `Set<Node>` across recursion. | TS keeps the dedupe as defense-in-depth. |

**Docs:** This repo’s `docs/roadmap.md` still lists `graph.signal` under Phase 1.4 unchecked while Phase 1.2 marks composition done; `signal` exists — checklist drift only.

### 15. `Graph` Phase 1.3 introspection (`describe`, `observe`, meta paths)

Cross-language: `graphrefly-ts/docs/optimizations.md` §15. **Python (shipped):**

| | |
|--|--|
| **Meta path segment** | Exported as `GRAPH_META_SEGMENT` (`__meta__`; alias `META_PATH_SEG`). Address: `registeredNode::__meta__::<metaKey>`; repeat for nested companion meta. |
| **Forbidden names** | `Graph.add` / `Graph.mount` reject `__meta__`. |
| **`connect` / `disconnect`** | Paths containing `__meta__` raise `ValueError` (wires stay on primary endpoints). |
| **`signal` → meta** | After mount-first recursion, each local primary node gets the batch, then depth-first through `meta` subtrees (**meta keys sorted** per parent, aligned with TS); per-call `visited` on `id(node)` avoids duplicate delivery if a node is shared. **TEARDOWN-only** batches skip the meta subtree walk — `NodeImpl.down` already cascades TEARDOWN to companions (matches graphrefly-ts). |
| **`describe()`** | Appendix B-shaped JSON; `nodes` / `edges` use qualified paths; `subgraphs` lists all mount points from this graph root; `deps` rewritten via the same id→path map. |
| **`observe()`** | `GraphObserveSource.subscribe` — one path: `sink(messages)`; whole graph: `sink(qualified_path, messages)`. **Resolved (2026-03-31):** Both ports now use full-path code-point sort for target ordering. Python switched from per-level sort to full-path `sorted()`. Cross-language observe order is now identical for the same graph topology. |
| **Describe `type`** | Both: `describe_kind` / `describeKind` on node options; sugar constructors (`effect`, `producer`, `derived`) set it; `_infer_describe_type` / `inferDescribeType` prefers explicit kind when set. |
| **`describe().nodes`** | Both strip `name` from per-node entries (dict key is the qualified path). |
| **`describe().subgraphs`** | Both recursively collect all nested mount paths. |
| **`connect` self-loop** | Both reject `connect(x, x)` before dep validation. |
| **`signal` / `_collect_observe_targets` ordering** | Both sort local nodes and mounts by name within each graph level. **Resolved (2026-03-31):** `observe()` target ordering now uses full-path code-point sort in both ports (aligned). |

### 16. `Graph` Phase 1.4 lifecycle & persistence (`destroy`, `snapshot`, `restore`, `from_snapshot`, `to_json`)

**Aligned:**

| | |
|--|--|
| **`destroy()`** | Both: `signal([[TEARDOWN]])` then clear all registries recursively through mounts. |
| **`snapshot()`** | Both: `{ version: 1, ...describe() }` — flat `version` field, sorted `nodes` keys. |
| **`restore(data)`** | Both: validate `data.name` matches graph name; skip `derived`/`operator`/`effect` types; silently ignore unknown/failing paths. |
| **`from_snapshot(data, build?)`** | Both: optional `build` callback registers topology before `restore()` applies values. Without `build`, both use registry-based reconstruction (mounts → topo node creation via factories → edges → restore). |
| **`to_json_string()` / `toJSON()`** | Python `to_json_string()` returns compact JSON **string** with trailing newline. TS `toJSON()` returns a plain sorted-key **object** (for `JSON.stringify(graph)`). Language-appropriate. |
| **`toJSONString()`** | TS only — `JSON.stringify(toJSON()) + "\n"`. Python's `to_json_string()` serves the same role. |

**Intentional divergence:**

| Topic | Python | TypeScript | Rationale |
|-------|--------|------------|-----------|
| `to_json_string` return type | `to_json_string()` → `str` (no universal `__json__` hook in Python) | `toJSON()` → plain object (ECMAScript `JSON.stringify` protocol) | Language idiom |
| `_parse_snapshot_envelope` | Validates `version`, `name`, `nodes`, `edges`, `subgraphs` types | Only validates `data.name` match | Python is stricter; both correct |

### Ingest adapters (roadmap 5.2c / 5.3b) — deferred items (QA)

Applies to `src/extra/adapters.ts` and `graphrefly.extra.adapters`. **Keep the table below identical in both repos' `docs/optimizations.md`.**

| Item | Status | Notes |
|------|--------|-------|
| **`fromRedisStream` / `from_redis_stream` never emits COMPLETE** | Documented limitation (2026-04-03) | Long-lived stream consumers intentionally never complete. The consumer loop runs until teardown. This is expected behavior for persistent stream sources (same as Kafka). Document in JSDoc/docstrings. |
| **`fromRedisStream` / `from_redis_stream` does not disconnect client** | Documented limitation (2026-04-03) | The caller owns the Redis client lifecycle. The adapter does not call `disconnect()` on teardown — the caller is responsible for closing the connection. Same contract as `fromKafka` (caller owns `consumer.connect()`/`disconnect()`). |
| **PY `from_csv` / `from_ndjson` thread cleanup** | Resolved (2026-04-05) | `t.join(timeout=1)` added after stop flag. |

### Ingest adapters — intentional cross-language divergences (parity review 2026-04-03)

| Aspect | TypeScript | Python | Rationale |
|--------|-----------|--------|-----------|
| **`KafkaConsumerLike` protocol** | KafkaJS shape: `subscribe({topic, fromBeginning})`, `run({eachMessage})`, `disconnect()` | confluent-kafka shape: `subscribe(topics: list)`, `run(callback)` | Each port targets its ecosystem's dominant Kafka client library. Both are duck-typed; users plug in their library's consumer directly. |
| **`RedisClientLike` protocol** | ioredis shape: `xadd(key, id, ...fieldsAndValues)`, `xread(...args)` — variadic positional | redis-py shape: `xadd(name, fields: dict)`, `xread(streams: dict)` — dict-based | Same reasoning: each port matches the dominant Redis client for its ecosystem. Serialize defaults match (`string[]` vs `dict[str, str]`). |
| **`toSSE` / `to_sse` return type** | `ReadableStream<Uint8Array>` (Web Streams API) | `Iterator[str]` (Python generator) | Language-native streaming idiom. TS uses Web Streams for SSE (compatible with `Response` constructor); PY uses generators (compatible with WSGI/ASGI streaming responses). |
| **`fromPrometheus` / `fromClickHouseWatch` `signal` option** | `signal: AbortSignal` for external cancellation | No equivalent; uses `active[0]` flag on teardown | PY has no standard `AbortSignal`. External cancellation in PY is handled by unsubscribing (which triggers the cleanup/stop function). Both ports stop cleanly on teardown. |
| **`SyslogMessage` field naming** | camelCase: `appName`, `procId`, `msgId` | snake_case: `app_name`, `proc_id`, `msg_id` | Language convention applied to output data structures. Each port follows its ecosystem's naming idiom. |
| **`fromCSV` / `fromNDJSON` source type** | `AsyncIterable<string>` (async streams with chunk buffering) | `Iterable[str]` (sync iterators via threads) | PY uses threads for I/O concurrency; sync iterables are natural for `csv.reader` integration. TS uses async iteration for streaming I/O. |
| **`PulsarConsumerLike` protocol** | `pulsar-client` JS shape: `receive()` returns Promise, `acknowledge(msg)` returns Promise, getter methods (`getData()`, `getTopicName()`, etc.) | `pulsar-client` PY shape: `receive()` blocking, `acknowledge(msg)` sync, attribute methods (`data()`, `topic_name()`, etc.) | Each port matches the native Pulsar client API for its ecosystem. TS uses async loop; PY uses threaded blocking loop. |
| **`PulsarProducerLike.send()` call shape** | Single object: `send({data, partitionKey, properties})` | Positional + kwargs: `send(data, partition_key=..., properties=...)` | Matches respective native Pulsar client SDK calling conventions. |
| **`PulsarMessage` field naming** | camelCase: `messageId`, `publishTime`, `eventTime` | snake_case: `message_id`, `publish_time`, `event_time` | Language convention applied to output data structures. |
| **`NATSClientLike` protocol** | nats.js shape: `subscribe()` returns `AsyncIterable`, `publish(subject, data)` | Dual: sync iterable (threaded drain) or async iterable/coroutine (via `Runner`). Auto-detected at subscribe time. Optional `runner` kwarg. | TS uses native async iteration. PY auto-detects sync vs async subscriptions: sync uses threaded drain, async uses `resolve_runner().schedule()`. Both support queue groups. |
| **`RabbitMQChannelLike` protocol** | amqplib shape: `consume(queue, callback)` returns `Promise<{consumerTag}>`, `cancel(tag)`, `ack(msg)`, `publish(exchange, routingKey, content)` | pika shape: `basic_consume(queue, on_message_callback, auto_ack)`, `start_consuming()`, `basic_ack(delivery_tag)`, `basic_publish(exchange, routing_key, body)` | Each port matches its ecosystem's dominant AMQP library. Pika requires `start_consuming()` to enter the event loop; amqplib's consume is promise-based. |
| **`RabbitMQMessage` field naming** | camelCase: `routingKey`, `deliveryTag` | snake_case: `routing_key`, `delivery_tag` | Language convention applied to output data structures. |

---

## Summary

| Topic | Python | TypeScript |
|-------|--------|------------|
| Core sugar `subscribe(dep, fn)` / `operator` | Not exported (parity with graphrefly-ts): use `node([dep], fn)`, `effect([dep], fn)`, `derived` | Not exported: use `node([dep], fn)`, `effect([dep], fn)`, and `derived` for all deps+fn nodes |
| `pipe` and `Node.__or__` | `pipe()` plus `|` on nodes (GRAPHREFLY-SPEC §6.1) | `pipe()` only |
| Message tags | `StrEnum` | `Symbol` |
| Subgraph write locks | Union-find + `RLock`; `defer_set` / `defer_down`; per-node `_cache_lock` for `get()`/`_cached`; bounded retry (`_MAX_LOCK_RETRIES=100`) | N/A (single-threaded) |
| Batch emit API | `emit_with_batch` (+ `dispatch_messages` alias); optional `subgraph_lock` for node emissions | `emitWithBatch` |
| Defer phase-2 | `defer_when`: `depth` vs `batching` | depth **or** draining (aligned with Py `batching`) |
| `isBatching` / `is_batching` | depth **or** draining | depth **or** draining |
| Batch drain resilience | per-emission try/catch, `ExceptionGroup` | per-emission try/catch, first error re-thrown |
| Nested `batch` throw + drain (**A4**) | Do **not** clear global queue while flushing | `!flushInProgress` guard before clear |
| `TEARDOWN` / `INVALIDATE` after terminal (**B3**) | Filter + full lifecycle + emit to sinks | Same |
| Partial drain before rethrow (**C1**) | Document intentional | Document intentional (JSDoc) |
| Source `up` / `unsubscribe` | no-op | no-op (always present for V8 shape stability) |
| `fn` returns callable | cleanup | cleanup |
| Connect re-entrancy | `_connecting` | `_connecting` (aligned) |
| Sink snapshot during delivery | `list(self._sinks)` snapshot before iterating | `[...this._sinks]` snapshot before iterating |
| Drain cycle detection | `_MAX_DRAIN_ITERATIONS = 1000` cap (aligned) | `MAX_DRAIN_ITERATIONS = 1000` cap |
| TEARDOWN → `"disconnected"` status | `_status_after_message` maps TEARDOWN | `statusAfterMessage` maps TEARDOWN |
| DIRTY→COMPLETE settlement (D2) | `_run_fn()` when no dirty deps remain but node is dirty | `_runFn()` when no dirty deps remain but node is dirty |
| Describe slice + frozen meta | `describe_node`, `MappingProxyType` | `describeNode` via `instanceof NodeImpl`, `Object.freeze(meta)` |
| Node internals | Class-based `NodeImpl`, all methods on class | Class-based `NodeImpl`, V8 hidden class optimization, prototype methods |
| Dep-value identity check | Before cleanup (skip cleanup+fn on no-op) | Before cleanup (skip cleanup+fn on no-op) |
| `INVALIDATE` (§1.2) | Cleanup + clear `_cached` + `_last_dep_values`; terminal passthrough (§9); no auto recompute | Same |
| `Graph` Phase 1.1 | `thread_safe` + `RLock`; TEARDOWN after unlock on `remove`; `disconnect` registry-only (§C resolved); `add()` auto-registers edges from constructor deps | Registry only; `connect` / `disconnect` errors aligned; §C resolved; `add()` auto-registers edges from constructor deps |
| `Graph` Phase 1.2 | Aligned: `::` path separator, mount `remove` + subtree TEARDOWN, qualified paths, `edges()`, signal mounts-first, `resolve` strips leading name, `:` in names OK; see §14 | Same; see §14 |
| `Graph` Phase 1.3 | `describe`, `observe`, `GRAPH_META_SEGMENT`, `signal`→meta, `describe_kind` on sugar; see §15 | TS: `describe()`, `observe()`, `GRAPH_META_SEGMENT`, `describeKind` on sugar; see graphrefly-ts §15 | `observe()` order: both use full-path code-point sort (resolved 2026-03-31; see §15) |
| `Graph` Phase 1.4 | `destroy`, `snapshot` (flat `version: 1`), `restore` (name check + type filter + silent catch), `from_snapshot(data, build=)`, `to_json_string()` → str + `\n`; see §16 | `destroy`, `snapshot`, `restore`, `fromSnapshot(data, build?)`, `toJSON()` → object, `toJSONString()` → str + `\n`; see §16 |
| `Graph` Phase 1.5 | **Python:** `Actor`, `GuardDenied`, `policy()`, `compose_guards`, node `guard` opt, `down`/`set`/`signal`/`subscribe`/`describe` actor params, `internal` propagation bypass, `remove`/unmount subtree TEARDOWN `internal=True`; see built-in §8 | **TypeScript:** aligned — `GraphActorOptions`, `NodeTransportOptions`, scoped `describe`/`observe`, `GuardDenied.node` getter mirrors `nodeName` |
| `policy()` semantics | Deny-overrides: any matching deny blocks; if no deny, any matching allow permits; no match → deny | Same (aligned from parity round) |
| `DEFAULT_ACTOR` | `{"type": "system", "id": ""}` | `{ type: "system", id: "" }` (aligned) |
| `lastMutation` timestamp | `timestamp_ns` via `wall_clock_ns()` (`time.time_ns()`) | `timestamp_ns` via `wallClockNs()` (`Date.now() * 1_000_000`) — both wall-clock nanoseconds; centralised in `core/clock` |
| `accessHintForGuard` | Probes guard with standard actor types → `"both"`, `"human"`, `"restricted"`, etc. | `accessHintForGuard()` — same probing logic (aligned from parity round) |
| `subscribe()` observe guard | `subscribe(sink, hints, *, actor=)` checks observe guard at node level | `subscribe(sink, { actor? })` checks observe guard at node level (aligned from parity round) |
| `up()` guard + attribution | `up(msgs, *, actor=, internal=, guard_action=)` checks guard, records `last_mutation` | `up(msgs, opts?)` checks guard, records `lastMutation` (aligned from parity round) |
| `on_message` (spec §2.6) | `on_message` option on node; checked in `_handle_dep_messages`; `True` consumes, exception → ERROR | `onMessage` option; same semantics |
| `meta` guard inheritance | Meta companions inherit parent guard at construction | Same |
| `Graph.destroy()` guard bypass | `_signal_graph(..., internal=True)` bypasses all guards | Same |
| `Graph.set` internal | `set(name, value, *, internal=False)` | `set(name, value, { internal? })` |
| `allows_observe()` / `has_guard()` | Public methods on `NodeImpl` | Public methods on `Node` interface |
| Extra Phase 2.3 (sources/sinks) | `graphrefly.extra.sources` + `graphrefly.extra.cron`; see §5 above | `src/extra/sources.ts` + `src/extra/cron.ts`; see §5 above |
| `gate(source, control)` | `graphrefly.extra.tier2.gate` | `src/extra/operators.ts` `gate` (aligned 2026-03-28) |
| `first_value_from` / `firstValueFrom` | `first_value_from(source, timeout=)` (blocking) | `firstValueFrom(source): Promise<T>` |
| `from_event_emitter` / `fromEvent` | Generic emitter (`add_method=`, `remove_method=`) | DOM `addEventListener` API |
| `to_array` / `toArray` | Reactive `Node[list]` | Reactive `Node<T[]>` |
| `to_list` (blocking) | Py-only sync bridge | N/A |
| Extra Phase 3.1 (resilience) | `graphrefly.extra.{backoff,resilience,checkpoint}` + `core/timer.py` (`ResettableTimer`); see §6 below | `src/extra/{backoff,resilience,checkpoint}.ts` + `core/timer.ts` (`ResettableTimer`); see §6 below |
| Extra Phase 3.2 (data structures) | `graphrefly.extra.data_structures` (`reactive_map`, …); see §17 | `reactiveMap` + `reactive-base` (`Versioned` snapshots); see §17 |

### 18. CQRS reactive log snapshot shape (Phase 4.5 — cross-language note)

The append-only log underneath CQRS events is `reactiveLog` in TypeScript (`Versioned<{ entries: readonly T[] }>`) and `reactive_log` in Python (`Versioned` wrapping a **tuple** of entries). The CQRS layer adapts locally when building projections and sagas. This is **not** a user-facing API mismatch for typical use.

### 19. Inspector causality hooks (Phase 3.3 observe extensions)

| Topic | Python | TypeScript |
|-------|--------|------------|
| Core hook shape | `NodeImpl._set_inspector_hook()` installs an internal, opt-in hook with `dep_message` and `run` events. | `NodeImpl._setInspectorHook()` mirrors the same hook contract (`dep_message`, `run`). |
| Runtime overhead | Hook pointer is `None` by default; no event allocation unless `observe(..., timeline/causal/derived)` is active. | Hook pointer is `undefined` by default; no event allocation unless `observe(name, { timeline/causal/derived })` is active. |
| Graph usage | `observe(name, timeline=True, causal=True, derived=True)` enriches structured events with `in_batch`, trigger dep metadata, and dep snapshots (graph-wide structured supported). | `observe(name, { timeline, causal, derived })` uses the same hook-driven enrichment model (graph-wide structured supported). |

Parity hardening (2026-03-30): both ports now keep `data` / `resolved` events under `causal` even when no trigger index is known yet, always emit `derived` on every `run`, and set `completed_cleanly` / `completedCleanly` only when no prior `ERROR` was seen. Structured timeline timestamps use `timestamp_ns` in both ports (nanoseconds). `ObserveResult.values` is latest-by-path map in both ports.

### 20. Inspector helper parity (reasoning trace + diagram export)

| Topic | Python | TypeScript |
|-------|--------|------------|
| Reasoning trace path validation | `graph.annotate(path, reason)` resolves `path` and raises if unknown. | `graph.annotate(path, reason)` resolves `path` and throws if unknown. |
| Reasoning trace entry key | `TraceEntry.path` (qualified node path) | `TraceEntry.path` (qualified node path) |
| Inspector disabled behavior | `trace_log()` returns `[]`; `annotate()` is a no-op. | `traceLog()` returns `[]`; `annotate()` is a no-op. |
| Diagram export | `graph.to_mermaid(direction=...)`, `graph.to_d2(direction=...)` | `graph.toMermaid({ direction })`, `graph.toD2({ direction })` |
| Direction set | `TD`, `LR`, `BT`, `RL` | `TD`, `LR`, `BT`, `RL` |
| D2 direction mapping | `TD→down`, `LR→right`, `BT→up`, `RL→left` | `TD→down`, `LR→right`, `BT→up`, `RL→left` |
| Direction validation | Runtime guard raises for values outside `TD/LR/BT/RL`. | Runtime guard throws for values outside `TD/LR/BT/RL`. |
| Trace ring size | 1000 entries (bounded ring). | 1000 entries (bounded ring). |
| Trace timestamp | `timestamp_ns` via `monotonic_ns()` (`time.monotonic_ns()`). | `timestamp_ns` via `monotonicNs()` (`performance.now`-based ns). Both centralised in `core/clock`. |
| Inspector default | Disabled when `NODE_ENV=production`; enabled otherwise. | Disabled when `NODE_ENV=production`; enabled otherwise. |
| `spy` return shape | `Graph.spy(...)` returns `SpyHandle` with `.result` and `.dispose()` | `Graph.spy(...)` returns `{ result: ObserveResult, dispose() }` (`GraphSpyHandle`) |
| `dump_graph` / `dumpGraph` JSON stability | Uses `json.dumps(..., sort_keys=True)` (byte-stable for same graph + options) | Uses recursively sorted keys before stringify (byte-stable for same graph + options) |

### 21. `reachable(...)` parity decisions (2026-03-30)

| Topic | Python | TypeScript |
|-------|--------|------------|
| Signature style | `reachable(described, from_path, direction, *, max_depth=None)` | `reachable(described, from, direction, { maxDepth? })` |
| Direction validation | Runtime guard: only `"upstream"` / `"downstream"` accepted; invalid raises | Runtime guard: only `"upstream"` / `"downstream"` accepted; invalid throws |
| Depth validation | Integer-only `max_depth >= 0` (`0` returns `[]`; rejects `bool`) | Integer-only `maxDepth >= 0` (`0` returns `[]`) |
| Malformed payload handling | Defensive: non-dict `nodes` / non-list `edges` treated as empty; malformed edges skipped | Defensive: same behavior (`nodes`/`edges` normalized, malformed entries skipped) |
| Traversal semantics | BFS over `deps` + `edges`; upstream = deps+incoming, downstream = reverse-deps+outgoing | Same |
| Output ordering | Lexical code-point ordering via `sorted()` | Lexical code-point ordering (stable, locale-independent) |

### 22. Centralised clock utilities (`core/clock`) — parity (2026-03-30)

Both repos export two timestamp functions from `core/clock`:

| Function | Python | TypeScript | Use case |
|----------|--------|------------|----------|
| `monotonic_ns` / `monotonicNs` | `time.monotonic_ns()` — true nanoseconds | `Math.trunc(performance.now() * 1_000_000)` — ~microsecond effective precision | Timeline events, trace entries, resilience timers, TTL deadlines, all internal duration tracking |
| `wall_clock_ns` / `wallClockNs` | `time.time_ns()` — true nanoseconds | `Date.now() * 1_000_000` — ~256ns precision loss at epoch scale | `lastMutation` attribution (guard), `fromCron` emission payload |

**Convention:** all timestamps in the protocol are nanoseconds (`_ns` suffix). No code outside `core/clock` should call `Date.now()`, `performance.now()`, `time.time_ns()`, or `time.monotonic_ns()` directly.

**JS platform precision limits** (documented in TS `src/core/clock.ts`):

- `monotonicNs`: `performance.now()` returns ms with ~5µs browser resolution; last 3 digits of ns value are always zero.
- `wallClockNs`: `Date.now() * 1e6` produces values ~1.8×10¹⁸ which exceed IEEE 754's 2⁵³ safe integer limit, causing ~256ns quantisation. Irrelevant in practice — JS is single-threaded, so sub-µs collisions cannot occur.

Python has no precision limitations (arbitrary-precision `int`).

**Internal timing (acceptable divergence):** TS `throttle` operator uses `performance.now()` (milliseconds) directly for relative elapsed-time gating. This is internal and never exposed as a protocol timestamp. Python tier-2 time operators use `threading.Timer` (wall-clock seconds). Both are correct for their purpose.

**Ring buffer:** TS trace log uses a fixed-capacity `RingBuffer<TraceEntry>` (default 1000) for O(1) push + eviction. Python uses `collections.deque(maxlen=1000)`.

**Diagram export — deps + edges:** Both `to_mermaid`/`toMermaid` and `to_d2`/`toD2` now render arrows from **both** constructor `deps` and explicit `connect()` edges, deduplicated by `(from, to)` pair.

### 22b. Phase 4.3 `vector_index` backend seam (optional HNSW dependency)

| Topic | TypeScript | Python |
|-------|------------|--------|
| Default backend | `backend: "flat"` exact cosine search (no external dependency). | `backend="flat"` exact cosine search (no external dependency). |
| HNSW backend | `backend: "hnsw"` requires an injected optional adapter (`hnswFactory`). Missing adapter throws a clear configuration error. | `backend="hnsw"` requires an injected optional adapter (`hnsw_factory`). Missing adapter raises a clear configuration error. |
| Product contract | Stable `vectorIndex` API now; production HNSW can be enabled later without changing the public API. | Same contract for cross-language parity. |

### 22c. Phase 4.3 memory patterns — Graph extension style, variable-length vectors, snapshot immutability

**Graph extension style (parity note):** TypeScript factories typically build a `Graph`, then attach domain methods with `Object.assign(graph, { ... })` so call sites get a single object with both `Graph` APIs and helpers. Python factories use a **`Graph` subclass** (for example `CollectionGraph`, `KnowledgeGraph`) with the same surface methods. Behavior is aligned; the difference is idiomatic typing and ergonomics in each language.

**Variable-length vectors (when `dimension` is omitted):** Stored rows and queries may differ in length. Flat cosine similarity **implicitly zero-pads both sides to `max(len(query), len(row))`** so ranking matches across TypeScript and Python. When `dimension` is set, vectors must match that length (unchanged).

**Snapshot immutability:** Memory-pattern derived snapshots follow the same spirit as messaging metadata: Python uses `MappingProxyType` / tuples for adjacency lists; TypeScript exposes **frozen** arrays for per-node edge lists in `knowledgeGraph` adjacency so callers do not accidentally mutate derived state.

### 22. Phase 4.2 messaging patterns parity (`topic`, `subscription`, `jobQueue`)

Both repos now ship a Pulsar-inspired messaging domain layer under `patterns.messaging`:

| Topic | TypeScript | Python | Notes |
|-------|------------|--------|-------|
| Namespace | `patterns.messaging` | `graphrefly.patterns.messaging` | Aligned |
| Topic factory | `topic(name, { retainedLimit?, graph? })` | `topic(name, retained_limit=, opts=)` | Naming differs by language convention |
| Subscription factory | `subscription(name, topicGraph, { cursor?, graph? })` | `subscription(name, topic_graph, cursor=, opts=)` | Cursor-based consumer on retained topic log |
| Job queue factory | `jobQueue(name, { graph? })` | `job_queue(name, opts=)` | Same queue behavior; naming differs by language convention |
| Job flow factory | `jobFlow(name, { stages?, maxPerPump?, graph? })` | `job_flow(name, stages=, max_per_pump=, opts=)` | Autonomous multi-stage queue chaining |
| Topic bridge factory | `topicBridge(name, sourceTopic, targetTopic, { cursor?, maxPerPump?, map?, graph? })` | `topic_bridge(name, source_topic, target_topic, cursor=, max_per_pump=, map_fn=, opts=)` | Autonomous cursor-based topic relay |
| Queue controls | `enqueue`, `claim`, `ack`, `nack` | `enqueue`, `claim`, `ack`, `nack` | `nack(requeue=false)` drops the job on both |
| Metadata capture | `Object.freeze({...metadata})` | `MappingProxyType(dict(metadata))` | Immutable snapshot at enqueue time on both |
| Return shape (imperative helpers) | Arrays (`readonly T[]`) | Tuples (`tuple[...]`) | Intentional language idiom; reactive node outputs remain protocol-driven in both |

**Design note:** helper methods like `pull()` / `retained()` return local collection snapshots for ergonomics. Reactive protocol semantics still flow through node outputs and `Graph.observe()` (messages are always `[[Type, Data?], ...]`). `job_flow` and `topic_bridge` use keepalive-backed effect pumps (Option B) so forwarding/advancement runs autonomously after graph construction.

#### 3.1 Composition strategy: explicit topology via `mount()`

`subscription(name, topic_graph, ...)` now mounts the topic graph under `topic` and wires `topic::events -> source` via explicit graph edges.

| Approach | Pros | Cons |
|---|---|---|
| Direct cross-graph dep | Minimal API surface; easy to compose quickly. | Topology/ownership is implicit; edge registry cannot fully represent the dependency. |
| `mount()` + explicit edge (current) | Topology ownership and dependency edges are explicit (`topic::events -> source`), aligned with graph composition semantics. | Slightly more internal wiring. |

**Recommendation (current contract):** keep explicit topology (`mount` + explicit edge) for messaging composition.

**Supported counteract:** when lightweight composition is desired, use `topic_bridge` and `job_flow` helpers that still preserve explicit topology internally.

### 6. Resilience & checkpoint (roadmap 3.1) — parity (2026-03-29)

**Aligned:**

| Topic | Both |
|-------|------|
| `retry` | Resubscribe-on-ERROR with optional backoff; `count` caps attempts; `backoff` accepts strategy or preset name; successful DATA resets attempt counter; max-retries sentinel: `2_147_483_647` (`0x7fffffff`) |
| `backoff` strategies | `constant`, `linear`, `exponential`, `fibonacci`, `decorrelated_jitter` / `decorrelatedJitter`; jitter modes: `none`, `full`, `equal`; `resolve_backoff_preset` / `resolveBackoffPreset` maps preset names (including `"decorrelated_jitter"`); `with_max_attempts` / `withMaxAttempts` caps any strategy at N attempts (returns `None`/`null` after cap) |
| `CircuitBreaker` | `closed` → `open` → `half-open` states; `can_execute` / `canExecute`, `record_success` / `recordSuccess`, `record_failure` / `recordFailure`, `reset()`, `failure_count` / `failureCount`; optional `cooldown_strategy` / `cooldownStrategy` (BackoffStrategy) for escalating cooldowns across open cycles |
| `with_breaker` / `withBreaker` | Returns `WithBreakerBundle` (`node` + `breaker_state`/`breakerState`); `on_open: "skip"` → RESOLVED, `"error"` → CircuitOpenError |
| `rate_limiter` / `rateLimiter` | Sliding-window FIFO queue; raises/throws on `max_events <= 0` or `window_seconds <= 0`; COMPLETE/ERROR clear timers + pending + window times |
| `TokenBucket` | Capacity + refill-per-second; `try_consume` / `tryConsume`; `token_tracker` / `tokenTracker` factory alias |
| `with_status` / `withStatus` | `WithStatusBundle` (`node` + `status` + `error`); recovery from `errored` via `batch` |
| `describe_kind` | All resilience operators use `"operator"` |
| Checkpoint adapters | `Memory`, `Dict`, `File`, `Sqlite` on both; `save_graph_checkpoint`/`restore_graph_checkpoint`; `checkpoint_node_value` returns `{ version: 1, value }` |

**Intentional divergences:**

| Topic | Python | TypeScript | Rationale |
|-------|--------|------------|-----------|
| Timer base | `monotonic_ns()` (nanoseconds via `time.monotonic_ns()`); `ResettableTimer` in `core/timer.py` | `monotonicNs()` (nanoseconds via `performance.now()`); `ResettableTimer` in `core/timer.ts` | Both centralised in `core/clock`; nanosecond internal tracking; `ResettableTimer` used by `retry`, `rate_limiter`, `timeout` |
| Thread safety | `CircuitBreaker` + `TokenBucket` use `threading.Lock`; retry uses `threading.Timer` | Single-threaded (`setTimeout`) | Spec §6.1 |
| `CircuitBreaker` params | `cooldown` (seconds, implicit) | `cooldownSeconds` (seconds, explicit) | Naming convention |
| `CircuitOpenError` base | `RuntimeError` | `Error` | Language convention |
| API pattern | `@runtime_checkable Protocol` + private `_Impl` class + `circuit_breaker()` / `token_bucket()` factory | `interface` + private class + `circuitBreaker()` / `tokenBucket()` factory | Both expose factory functions as primary API; types for structural checks |
| Retry delay validation | `_coerce_delay()` raises `ValueError` for non-finite | `coerceDelaySeconds()` throws `TypeError` for non-finite | Both validate; error type differs |
| IndexedDB checkpoint | N/A (backend-only) | `saveGraphCheckpointIndexedDb` / `restoreGraphCheckpointIndexedDb` (browser) | TS browser runtime only |
| `SqliteCheckpointAdapter` | `sqlite3` stdlib | `node:sqlite` (`DatabaseSync`, Node 22.5+) | Both stdlib, zero deps |

**Meta integration (spec §2.3, Option A):** `with_breaker` and `with_status` wire companion nodes into `node.meta` at construction via the `meta` option. Bundles still provide ergonomic typed access; `node.meta["breaker_state"]` / `node.meta["status"]` are the same node instances returned in the bundle. Companions appear in `graph.describe()` under `::__meta__::` paths.

### 17. Phase 3.2 data structures (versioned snapshots)

**TypeScript:** `reactiveMap` (`src/extra/reactive-map.ts`); shared `Versioned<T>` + `snapshotEqualsVersion` in `src/extra/reactive-base.ts` (not re-exported from the package barrel — use concrete factories).

**Python:** `reactive_map`, `reactive_log`, `reactive_index`, `reactive_list`, `pubsub`, `log_slice` in `graphrefly.extra.data_structures` (re-exported from `graphrefly.extra`). **Parity aligned (2026-03-29):** All mutations emit via two-phase `batch()` (DIRTY then DATA); all snapshot nodes use `Versioned` (named tuple with monotonic `version` + `value`) with `_versioned_equals` for efficient dedup; `data.get().value` returns `MappingProxyType` (immutable) for maps and `tuple` for logs/lists; all factories accept an optional `name` param; `describe_kind` set on all internal nodes.

**Semantics (aligned):** Both ports use `Versioned` snapshots with a monotonic version counter for `NodeOptions.equals`. TTL: both use `monotonic_ns()` / `monotonicNs()` internally; public API takes seconds (`default_ttl` / `defaultTtl`). Lazy expiry + explicit `prune()` / `pruneExpired()` on both; no background timer in the first iteration. LRU: TS refreshes order on `get`/`has`; Python refreshes order on `set` only (reads use `data.get()` as a dict snapshot — no per-key LRU touch on read). `pubsub` topic publish uses two-phase protocol on both.

**Doc / API surface:** Both use seconds for TTL: TS `defaultTtl` / Python `default_ttl`.

**Derived log views (`tail` / `log_slice` / `logSlice`):** Both ports attach a noop subscription to each derived view so `get()` stays wired without a user sink (Python: `_keepalive_derived`). Each call allocates a new derived node plus that subscription; creating very many throwaway views can retain subscriptions until those nodes are unreachable. See JSDoc on `reactiveLog` / `logSlice` in graphrefly-ts and docstrings on `ReactiveLogBundle.tail` / `log_slice` in `graphrefly.extra.data_structures`.

### 17b. Phase 3.2b composite patterns parity (`verifiable`, `distill`)

Both ports now align on the following:

- **Falsy option values are honored** (`trigger`, `context`, `consolidateTrigger`) by checking only for `None`/missing (`null`/`undefined` in TS), not truthiness.
- **Extraction/consolidation are atomic**: each `Extraction` payload applies inside one outer `batch`, so downstream observers do not see intermediate partial states for multi-op updates.
- **Extraction contract is strict**: `upsert` is required by contract; malformed payloads are ignored by internal sink wiring (no imperative exception leakage to caller).
- **Eviction contract is explicit**: `evict` accepts `bool | Node[bool]` on both sides.

### Resolved design items (low priority)

1. **`_is_cleanup_fn` / `isCleanupFn` treats any callable return as cleanup (resolved 2026-03-31 — document limitation).** Both languages use `callable(value)` / `typeof value === "function"`. A compute function cannot emit a callable as a data value — it will be silently swallowed as cleanup. **Decision:** Document this as a known limitation in docstrings on `node()` and in API docs. No wrapper or opt-out flag — the pattern is well-documented, extremely rare in practice, and adding `{ cleanup: fn }` would add API surface for a near-zero use case.

2. **Describe `type` before first run (operator vs derived).** Both ports: `describe_kind` / `describeKind` on node options and sugar (`effect`, `producer`, `derived`); operators that only use `down()`/`emit()` still infer via `_manual_emit_used` after a run unless `describe_kind="operator"` is set.

3. **Tier 1 extra operators (roadmap 2.1).** Python ships `graphrefly.extra.tier1`; TypeScript ships `src/extra/operators.ts`. **Parity aligned (2026-03-28):**

   | Operator | Aligned behavior |
   |----------|-----------------|
   | `skip` | Both count wire `DATA` only (via `on_message`); initial dep settlement does not consume a skip slot |
   | `reduce` | Both: COMPLETE-gated fold — accumulate silently, emit once on COMPLETE (not alias for `scan`) |
   | `race` | Both: winner-lock — first source to emit DATA wins, continues forwarding only that source |
   | `merge` | Both: dirty bitmask tracking; single DIRTY downstream per wave; `COMPLETE` after all sources complete |
   | `zip` | Both: only DATA enqueues (RESOLVED does not, per spec §1.3.3); COMPLETE when a source completes with empty buffer or all complete |
   | `concat` | Both: buffer DATA from second source during phase 0; replay on handoff |
   | `take_until` | Both: default trigger on DATA only from notifier; optional `predicate` for custom trigger |
   | `with_latest_from` | Both: full `on_message` — suppress secondary-only emissions; emit only on primary settle |
   | `filter` | Both: pure predicate gate — no implicit dedup (use `distinct_until_changed` for that) |
   | `scan` | Both: delegate equality to `node(equals=eq)`, no manual RESOLVED in compute |
   | `distinct_until_changed` | Both: delegate to `node(equals=eq)` |
   | `pairwise` | Both: explicit RESOLVED for first value (no pair yet) |
   | `take_while` | Both: predicate exceptions handled by node-level error catching (spec §2.4) |
   | `start_with` | Both: inline `actions.emit(value)` then `actions.emit(deps[0])` in compute |
   | `combine/merge/zip/race` | Both: accept empty sources (degenerate case: empty tuple or COMPLETE producer) |
   | `last` | Both: sentinel for no-default — empty completion without default emits only COMPLETE |

   **Deferred QA items:** see §Deferred follow-ups.

4. **Tier 2 extra operators (roadmap 2.2).** Python ships `graphrefly.extra.tier2` (`threading.Timer`); TypeScript ships `src/extra/operators.ts` (`setTimeout`/`setInterval`). **Parity aligned (2026-03-28):**

   | Operator | Aligned behavior |
   |----------|-----------------|
   | `debounce` | Both: flush pending value on COMPLETE before forwarding COMPLETE |
   | `delay` | Both: only delay DATA; RESOLVED forwarded immediately |
   | `throttle` | Both: `leading` (default `True`/`true`) + `trailing` (default `False`/`false`) params |
   | `audit` | Both: trailing-only (Rx `auditTime`); timer starts on DATA, emits latest when timer fires; no leading edge |
   | `sample` | Both: trigger on notifier `DATA` only (RESOLVED ignored) |
   | `buffer` | Both: flush trigger on notifier `DATA` only |
   | `buffer_count` | Both: throw/raise on `count <= 0` |
   | `repeat` | Both: throw/raise on `count <= 0` |
   | `scan` | Both: `reset_on_teardown=True` / `resetOnTeardown: true` |
   | `concat_map` | Both: optional `max_buffer` / `maxBuffer` queue depth limit |
   | `switch_map` / `exhaust_map` / `concat_map` / `merge_map` | Both: inner ERROR unsubscribes inner; outer ERROR tears down all active inners |
   | `pausable` | Both: protocol-level PAUSE/RESUME buffer; buffers DIRTY/DATA/RESOLVED while paused, flushes on RESUME |
   | `window` | Both: true sub-node windows (emits `Node[T]` per window, not lists); notifier-based |
   | `window_count` | Both: true sub-node windows of `count` items each |
   | `window_time` | Both: true sub-node windows of `seconds` / `ms` duration |
   | `merge` / `zip` | Python: unlimited-precision `int` bitmask; TS: BigInt bitmask (no >31-source overflow) |

   `gate(source, control)` — value-level boolean gate. Both ports (parity aligned 2026-03-28).

   Timer callbacks emit via `NodeActions` → `Node.down(..., internal=True)`, which takes the subgraph write lock when `thread_safe` is true (default), so background threads serialize with synchronous graph work.

   **Deferred QA items:** see **Deferred follow-ups** → *Tier 2 extra operators (roadmap 2.2) — deferred semantics (QA)*.

5. **Sources & sinks (roadmap 2.3).** Python ships `graphrefly.extra.sources` + `graphrefly.extra.cron`; TypeScript ships `src/extra/sources.ts` + `src/extra/cron.ts`. **Parity aligned (2026-03-28):**

   | Source/Sink | Aligned behavior |
   |-------------|-----------------|
   | `from_timer` / `fromTimer` | Both: `(delay, period=)` — one-shot emits `0` then COMPLETE; periodic emits `0, 1, 2, …` every `period` (never completes). TS: `signal` (AbortSignal) support; Py: no signal (deferred). |
   | `from_cron` / `fromCron` | Both: built-in 5-field cron parser (zero external deps); emits wall-clock `timestamp_ns` via `wall_clock_ns()` / `wallClockNs()`. TS: `output: "date"` option for Date objects. |
   | `from_iter` / `fromIter` | Both: synchronous drain, one DATA per item, then COMPLETE. Error → ERROR. |
   | `of` | Both: `from_iter(values)` / `fromIter` under the hood. |
   | `empty` | Both: synchronous COMPLETE, no DATA. |
   | `never` | Both: no-op producer, never emits. |
   | `throw_error` / `throwError` | Both: immediate ERROR. |
   | `from_any` / `fromAny` | Both: Node passthrough, then async/iterable/scalar dispatch. Scalar → `of(value)`. |
   | `for_each` / `forEach` | Both: return unsubscribe callable (`Callable[[], None]` / `() => void`). Py: optional `on_error`; TS: `onMessage`-based. |
   | `to_array` / `toArray` | Both: reactive Node — collect DATA, emit `[…]` on COMPLETE. |
   | `share` | Both: ref-counted upstream wire; pass `initial=source.get()`. |
   | `cached` | Both: `replay(source, buffer_size=1)` / `replay(source, 1)`. |
   | `replay` | Both: real circular buffer + late-subscriber replay; reject `buffer_size < 1`. |
   | `first_value_from` | Py: blocks via `threading.Event`, returns value. TS: `firstValueFrom(source): Promise<T>`. |
   | `to_sse` / `toSSE` | Both: standard SSE frames (`event:` + `data:` lines + blank line), DATA/ERROR/COMPLETE mapping, optional keepalive comments, optional DIRTY/RESOLVED inclusion, and transport-level cancellation without synthetic graph ERROR frames. |
   | `describe_kind` | Both: source factories use `"producer"` (not `"operator"`). |
   | Static source timing | Both: synchronous emission during producer start (no deferred microtask). |

   **Intentional divergences:**

   | Topic | Python | TypeScript | Rationale |
   |-------|--------|------------|-----------|
   | `from_event_emitter` / `fromEvent` | `from_event_emitter(emitter, event, add_method=, remove_method=)` — generic emitter | `fromEvent(target, type, opts?)` — DOM `addEventListener` API | Language ecosystem |
   | `to_list` (blocking) | Py-only: blocks via `threading.Event`, returns `list` | N/A — use `await firstValueFrom(toArray(src))` | Py sync bridge |
   | `first_value_from` | Py-only: sync bridge | `firstValueFrom`: `Promise<T>` | Language concurrency model |
   | `to_sse` / `toSSE` return type | `Iterator[str]` SSE chunks | `ReadableStream<Uint8Array>` | Language runtime idiom |
   | `from_awaitable` / `fromPromise` | `from_awaitable`: worker thread + `asyncio.run` | `fromPromise`: native Promise | Language async model |
   | `from_async_iter` / `fromAsyncIter` | Worker thread + `asyncio.run` | Native async iteration | Language async model |
   | `from_http` / `fromHTTP` transform input | `transform(raw_bytes: bytes)` | `transform(response: Response)` | Runtime/library shape (`urllib` bytes vs Fetch `Response`) |
   | `from_http` external cancellation | No external signal (deferred); unsubscribe suppresses late emissions | Supports external `AbortSignal` via options | Language/runtime cancellation primitives |
   | AbortSignal on async sources | Not supported (deferred) | `signal` option on `fromTimer`, `fromPromise`, `fromAsyncIter` | TS has native AbortSignal; Py deferred |

   **Resolved (2026-03-31, implemented 2026-04-04):** `CancellationToken` protocol and `cancellation_token()` factory implemented in `core/cancellation.py`. Exported from `graphrefly.core`. Backed by `threading.Event` with lock-protected callback list, `is_cancelled` property, `on_cancel(fn)` returning an unsubscribe callable, and `cancel()` method. Wiring into async sources (`from_timer`, `from_awaitable`, `from_async_iter`) deferred — the protocol is ready. `TEARDOWN`-via-unsubscribe remains the primary cancellation path; the token is for external/cooperative cancellation.

## Resolved design decisions (cross-language, 2026-04-03)

- **Per-factory `resubscribable` option (Phase 7.1+, resolved 2026-04-03 — option (b) per-factory opt-in):** Add `resubscribable: bool` to `reactive_layout` / `reactive_block_layout` options (default `False`). When true, adapter errors emit ERROR but the node can be re-triggered via INVALIDATE. Broader audit across all extra factories deferred — apply the option incrementally as use cases arise. Pre-1.0, no backward compat concern.

- **`SvgBoundsAdapter` regex hardening (Phase 7.1, resolved 2026-04-03):** Strip `<!--...-->` and `<![CDATA[...]]>` from SVG content before viewBox/width/height extraction. Document that input should be a single root SVG element. Additionally, expose a `SvgParser` protocol so users can opt in their own parser for complex SVG inputs. Default: built-in regex parser. Cross-language: TS exposes equivalent `SvgParserAdapter` interface.

- **`sample` + `undefined` as `T` (Tier 2, resolved 2026-04-03 — no action):** Documented limitation. TS-specific edge case (Python does not have the `undefined` ambiguity). No sentinel needed.

- **`mergeMap` / `merge_map` + `ERROR` cascading (Tier 2, resolved 2026-04-03 — no action):** Documented limitation. Inner errors do not cascade to siblings. Current behavior (independent inner lifecycles) is more useful for parallel work. Document in docstrings.

---

## Design decisions (QA review)

These came out of QA review. Most are now **resolved** (2026-03-31); remaining items are deferred with rationale. Tracked in both `graphrefly-ts/docs/optimizations.md` and here for cross-language visibility.

### A. `COMPLETE` when all dependencies complete

**Resolved (2026-03-31, Option A3):** Auto-completion is controlled via `complete_when_deps_complete` (Python) / `completeWhenDepsComplete` (TS). Both ports already expose this option. Defaults: **`True`** for effect and operator nodes (matches spec §1.3.5 — effects complete when all deps complete), **`False`** for derived nodes (derived nodes should stay alive for future `INVALIDATE` / resubscription). Most operators already set `complete_when_deps_complete=False` explicitly.

**Rationale:** The spec mandates effect-node completion; derived nodes benefit from staying alive for invalidation and Graph lifecycle. The existing opt-in flag gives maximum flexibility.

### B. More than 31 dependencies

**Resolved.** Python uses unlimited-precision `int` bitmasks natively. TypeScript uses a `BitSet` abstraction with `Uint32Array` fallback for >31 deps.

### C. `graph.disconnect` vs `NodeImpl` dependency lists (QA 1d #2)

**Resolved (2026-03-31, Option C1 — registry-only):** `Graph.disconnect(from, to)` removes the `(from, to)` pair from the graph’s **edge registry** only. It does **not** mutate the target node’s constructor-time dependency list, bitmasks, or upstream subscriptions. This is the **long-term contract**.

**Why registry-only is correct:** Dependencies are fixed at node construction. True single-edge removal would require partial upstream unsubscribe, bitmask width resizing, diamond invariant recalculation, and thread-safety rework in Python — enormous complexity for a niche use case. For runtime dep rewiring, use `dynamic_node` (Phase 0.3b), which handles full dep-diff, bitmask rebuild, and subscription lifecycle.

**Contract:** `disconnect` is a registry/bookkeeping operation. `describe()` and `edges()` are the source of truth for registered topology. Message flow follows constructor-time deps, not the edge registry. Document this clearly in docstrings and API docs.

### D. Tier-2 time operators — `asyncio` vs wall-clock timers

**Resolved (2026-03-31 — keep `threading.Timer` as default, defer `asyncio`):** `graphrefly.extra.tier2` uses wall-clock **`threading.Timer`**. Callbacks emit via **`Node.down(..., internal=True)`**, which takes the **subgraph write lock** when **`thread_safe`** is true (default), so timer threads stay consistent with synchronous graph work **without** requiring a running **`asyncio`** loop.

**Rationale:** The current design is correct and portable. Optional **`asyncio`**-based scheduling (e.g. **`loop.call_soon_threadsafe`**) can be added later only when a concrete user reports integration friction with an existing event loop, while keeping **`threading.Timer`** as the default baseline.

**TypeScript (parity note):** The same product split applies on the JS side: tighter integration with the host’s **event loop / task queue** vs timer primitives that do not assume a specific runtime; align cross-language when either port adds loop-integrated scheduling.

### E. Roadmap §3.1b callback coercion scope (`fromAny` / `from_any`)

**Resolved (Option 2):** Public higher-order operators in TypeScript (`switchMap`, `concatMap`, `mergeMap`, `exhaustMap`) and Python (`switch_map`, `concat_map`, `merge_map`, `exhaust_map`) now accept callback outputs as **Node, scalar, Promise/Awaitable, Iterable, or AsyncIterable**, with coercion through `fromAny` / `from_any`.

**Rationale:** Better ergonomics and stronger parity with AI-generated integration code while preserving the single reactive output model.

### F. Phase 4.1 orchestration API shape (`pipeline` + step naming collisions)

**Resolved (Option B):** Orchestration primitives ship under a grouped namespace (`patterns.orchestration.*`), not as colliding top-level exports. This keeps Phase 2 `extra` names (`for_each`, `gate`) intact while exposing solution-level workflow APIs as domain constructs.

**Current contract:** `patterns.orchestration.gate` / `approval` / `branch` / `task` are workflow-step builders over `Graph` topology and lifecycle, not aliases of stream-only `extra` operators/sinks.

**Parity note:** `describe()["nodes"][*]["meta"]` uses canonical key `orchestration_type` in both ports for orchestration step metadata.

### G. Phase 4.1 `loop(iterations)` coercion contract

**Resolved:** Orchestration `loop` uses a shared **permissive numeric parse + truncate** rule in both ports:

- Parse iteration input permissively (numeric values and numeric-like strings).
- Truncate toward zero.
- Clamp negatives to `0`.
- If parse is non-finite/invalid, default to `1`.
- Empty string and `null`/`None` normalize to `0`.

**Rationale:** Keeps orchestration ergonomics AI-friendly while preserving deterministic cross-language behavior.

### H. Phase 5.2 WebSocket adapter seam (`from_websocket` / `to_websocket`)

**Resolved subset:** Both ports now support the same practical seam for source/sink adapters:

- Source supports either runtime socket listener wiring or explicit register-style wiring.
- Inbound payload normalization uses `event.data` when present, otherwise the raw event payload.
- Sink supports optional terminal close metadata (`close_code`/`close_reason` in Python, `closeCode`/`closeReason` in TypeScript).

**Rationale:** This keeps adapters thin and runtime-friendly while preserving parity for message shaping and terminal close behavior.

**Note:** Lifecycle and sink error-policy behavior are tracked separately below under
**WebSocket adapter lifecycle and error-policy seams**.

---

## Deferred follow-ups (QA)

Non-blocking items tracked for later; not optimizations per se. Keep this section **identical** in `graphrefly-ts/docs/optimizations.md` and here (aside from language-specific labels in the first table).

| Item | Notes |
|------|-------|
| **`lastDepValues` + `is` / referential equality (resolved 2026-03-31 — keep + document)** | Default `is` identity check is correct for the common immutable-value case. The `node(equals=)` option already exists for custom comparison. Document clearly that mutable dep values should use a custom `equals` function. No code change needed. |
| **`sideEffects: false` in `package.json`** | TypeScript package only. Safe while the library has no import-time side effects. Revisit if global registration or polyfills are added at module load. |
| **JSDoc / docstrings on `node()` and public APIs** | `docs/docs-guidance.md`: JSDoc on new TS exports; docstrings on new Python public APIs. |
| **Roadmap §0.3 checkboxes** | Mark Phase 0.3 items when the team agrees the milestone is complete. |

### AI surface (Phase 4.4) — behavioral semantics parity (resolved 2026-03-31)

Cross-language notes for `patterns.ai` / `graphrefly.patterns.ai`. **Keep this subsection aligned in both repos’ `docs/optimizations.md`.**

| Topic | Resolution |
|-------|------------|
| **`agent_loop` / `agentLoop` — LLM adapter output** | `invoke` may return a plain `LLMResponse`, or any `NodeInput` (including `Node`, awaitables, async iterables). Implementations coerce with `fromAny` / `from_any`, prefer a synchronous `get()` when it already holds an `LLMResponse`, then **block until the first settled `DATA`** (`subscribe` + `Promise` in TypeScript; `first_value_from` in Python). Do not unsubscribe immediately after `subscribe` without waiting for emissions. |
| **`toolRegistry` / `tool_registry` — handler output** | Handlers may return plain values, Promise-like values, or reactive `NodeInput`. **TypeScript:** `execute` awaits Promise-likes, then resolves **only** `Node` / `AsyncIterable` via `fromAny` + first `DATA` (do **not** pass arbitrary strings through `fromAny` — it treats strings as iterables and emits per character). **Python:** `execute` uses `from_any` + `first_value_from` only for awaitables, async iterables, or `Node`; plain values return as-is. |
| **`agentMemory` / `agent_memory` — factory scope** | **Resolved (2026-03-31):** Ship as-designed. The full in-factory composition (`knowledgeGraph` + `vectorIndex` + `lightCollection` + `decay` + `autoCheckpoint`, opt-in via options) will be implemented per the resolved design decision at the top of this document. A single `agentMemory(name, { vectorDimensions, embedFn, enableKnowledgeGraph })` / `agent_memory(name, vector_dimensions=, embed_fn=, enable_knowledge_graph=)` call provides batteries-included memory. Implementation to follow. |

### AI surface (Phase 4.4) — parity follow-ups

| # | Topic | Notes |
|---|--------|-------|
| **3** | **`_invoke_llm` / `_invokeLLM` defensive alignment** | **TypeScript** (`_invokeLLM`): rejects `null`/`undefined` and plain `str` before `fromAny` (strings would iterate per character); accepts sync plain objects with `content` when not a Promise/`Node`. **Python** (`_invoke_llm`): should mirror those guards — reject `None`; reject `str`; do not pass raw `dict`/`Mapping` through `from_any` without normalizing to `LLMResponse` (iterating a `dict` yields keys). **Status:** pending implementation in `graphrefly-py`. |
| **4** | **`LLMInvokeOptions` + cooperative cancellation** | **Resolved (2026-03-31):** Python will use a `CancellationToken` protocol — a small interface with `.is_cancelled` property and `.on_cancel(fn)` callback registration, backed internally by `threading.Event`. This mirrors TS's `AbortSignal` pattern. The token is passed from `AgentLoopGraph` into `adapter.invoke()`. Adapters react to cancellation via `.on_cancel()` callbacks (no polling — respects the reactive invariant). The protocol can be extended to `asyncio.Event` backing later. **TypeScript** retains `AbortSignal` via `LLMInvokeOptions.signal`. |

Normative anti-patterns table: [**Implementation anti-patterns**](#implementation-anti-patterns) (top of this document).

### AI surface (Phase 4.4) — resolved follow-ups (2026-03-31)

| Item | Resolution |
|------|------------|
| **keepalive subscription cleanup on destroy** | `ChatStreamGraph`, `ToolRegistryGraph`, and `system_prompt_builder` create keepalive subscriptions (`n.subscribe(lambda: None)`) that are never cleaned up. **Auto-fixable:** add `destroy()` methods that unsubscribe keepalive sinks to prevent leaks in long-lived processes. |
| **`AgentLoopGraph.destroy()` does not cancel running loop (resolved — internal abort signal)** | `destroy()` sets an internal cancellation flag (`threading.Event`); the `run()` loop checks it between iterations. No polling — reactive cancellation via event signal. Rejected: (b) reject-only (doesn't stop the LLM call); (c) document-as-limitation (violates `destroy()` safety contract). |
| **`chat_stream.clear()` + `append()` race (resolved — serialize via `batch()`)** | Both `clear()` and `append()` internally use `batch()` so they are atomic within a reactive cycle. Callers who need deterministic ordering across multiple mutations use `batch(lambda: (stream.clear(), stream.append(msg)))`. No new mechanism needed — uses existing protocol. Rejected: (b) arbitrary "clear wins" rule; (c) microtask queue (fights the reactive-not-queued invariant). |

### AI surface (Phase 4.4) — deferred optimizations (QA 2026-03-31)

| Item | Status | Notes |
|------|--------|-------|
| **Re-indexes entire store on every change** | Deferred | Decision: diff-based indexing using `Versioned` snapshot version field to track indexed entries. Deferred to after Phase 6 — current N is small enough that full re-index is acceptable pre-1.0. |
| **Budget packing always includes first item** | Documented behavior | The retrieval budget packer always includes the first ranked result even if it exceeds `max_tokens`. This is intentional "never return empty" semantics — a query that matches at least one entry always returns something. Callers who need strict budget enforcement should post-filter. |
| **Retrieval pipeline auto-wires when vectors/KG enabled** | Documented behavior | When `embed_fn` or `enable_knowledge_graph` is set, the retrieval pipeline automatically wires vector search and KG expansion into the retrieval derived node. There is no explicit opt-in/opt-out per retrieval stage — the presence of the capability implies its use. Callers who need selective retrieval should use the individual nodes directly. |

### Tier 1 extra operators (roadmap 2.1) — resolved semantics (2026-03-31)

Applies to `graphrefly-ts` `src/extra/operators.ts` and `graphrefly.extra.tier1`. **Keep the table below identical in both repos’ `docs/optimizations.md`.**

| Item | Resolution |
|------|------------|
| **`takeUntil` / `take_until` + notifier `DIRTY` (resolved — DATA-only trigger)** | The notifier must emit **`DATA`** to terminate the primary. `DIRTY` is phase-1 transient signaling; termination is permanent and must only trigger on settled phase-2 data. Aligns with compat adapter rule (ignore DIRTY waves). A notifier that only sends DIRTY+RESOLVED (no payload change) never triggers — by design. |
| **`zip` + partial queues (resolved — drop + document)** | When one inner source completes, buffered values that never formed a full tuple are **dropped**; downstream then completes. This matches RxJS behavior and the zip contract (all slots always filled). Callers who need all values should use `combineLatest` or `merge`. Document in JSDoc/docstrings. |
| **`concat` + `ERROR` on the second source before the first completes (resolved — fail-fast short-circuit)** | `ERROR` from **any** source (even buffered/inactive) immediately terminates `concat`. Silent error swallowing is a bug magnet; fail-fast is the safer pre-1.0 default. Callers who need “ignore inactive source errors” can wrap source 2 in `retry` or `catchError`. |
| **`race` + pre-winner `DIRTY` (resolved — keep current + document)** | Before the first winning `DATA`, `DIRTY` from multiple sources **may** forward downstream. This is transient and harmless — downstream handles it via normal settlement. A stricter “winner-only” implementation adds complexity for minimal gain. Document the behavior clearly in JSDoc/docstrings. |

### Tier 2 extra operators (roadmap 2.2) — deferred semantics (QA)

Applies to `src/extra/operators.ts` and `graphrefly.extra.tier2`. **Keep the table below identical in both repos’ `docs/optimizations.md`.**

| Item | Status | Notes |
|------|--------|-------|
| **`sample` + `undefined` as `T`** | Documented limitation (2026-03-31) | Sampling uses the primary dep’s cached value (`get()`). If `T` allows `undefined`, a cache of `undefined` is indistinguishable from “no snapshot yet”; TypeScript currently emits `RESOLVED` instead of `DATA` in that case (JSDoc `@remarks`). This is a known TS-specific edge case (Python does not have the `undefined` ambiguity). Document in JSDoc; no sentinel needed. |
| **`mergeMap` / `merge_map` + `ERROR`** | Documented limitation (2026-03-31) | When the outer stream or one inner emits `ERROR`, other inner subscriptions may keep running until they complete or unsubscribe. Rx-style “first error cancels all sibling inners” is **not** specified or implemented. Current behavior (inner errors don’t cascade) is arguably more useful for parallel work — no change needed. Document in JSDoc/docstrings. |

### TC39 compat read/subscribe terminal semantics (`Signal.get` / `Signal.sub`)

**Resolved (2026-03-31, Option I1 — strict data-only):** Both ports standardize on **strict data-only** compat APIs:

- `get()` **never throws** and returns the last good value when status is `errored`.
- `Signal.sub` forwards **only `DATA`** — terminal/error tuples remain in the core/node API layer.

**Rationale:** The compat layer should be the simplest possible bridge to framework APIs. Users who need terminal observability should use the core `subscribe` / `node` APIs directly. This matches the spec's `get()` contract and keeps the compat surface minimal.

### WebSocket adapter lifecycle and error-policy seams (`from_websocket` / `to_websocket`)

**Resolved (2026-03-31, Option J1 — eager teardown + propagate):** Both ports standardize on:

1. **Eager terminal teardown:** Listeners are detached immediately on first terminal message (`COMPLETE`/`ERROR`). Close is idempotent — repeated terminal calls are no-ops.
2. **Propagate sink errors:** `send`/`close` transport exceptions are surfaced as protocol-level `[[ERROR, err]]` to callers, not swallowed.

**Rationale:** Keep it simple and predictable. Resources are freed immediately; errors are visible. Users who need reconnect behavior can layer `retry` on top — that's what the resilience operators are for.

### Filesystem watch adapter contract (`from_fs_watch` / `fromFSWatch`)

**Resolved (2026-03-31):** Cross-language adapter contract for filesystem watch sources now standardizes on:

1. **Debounce-only, no polling fallback** (event-driven watcher backends only),
2. **Dual-path glob matching** against both absolute path and watch-root-relative path,
3. **Expanded payload shape** with `path`, `root`, `relative_path`, `timestamp_ns`,
4. **Rename-aware payloads** (TS classifies `fs.watch` rename notifications with best-effort `create`/`delete` and preserves `rename` fallback; Py preserves move/rename semantics and includes `src_path`/`dest_path` when available),
5. **Watcher error handling via protocol** (`[[ERROR, err]]`) with teardown-latched cleanup.

**Rationale:** Prevent silent filter mismatches, preserve rename semantics, and keep lifecycle/error behavior inside GraphReFly message protocol without violating the no-polling invariant.

### Adapter behavior contract scope (`from_webhook` / `from_websocket` / `to_websocket`)

**Resolved (2026-03-31, Option K1 — define canonical contract now):** A shared cross-language adapter contract covers:

1. **Register callback expectations:** `register` must return a cleanup callable. Registration is atomic — the cleanup callable is valid immediately. Errors raised during registration are forwarded as `[[ERROR, err]]`.
2. **Terminal-time ordering:** Cleanup runs **before** terminal tuple emission. Listeners are detached before `COMPLETE`/`ERROR` propagates downstream.
3. **Sink transport failure handling:** Transport exceptions (`send`/`close` failures) surface as `[[ERROR, err]]` — never swallowed, never raised to caller (see §J). Callback payloads are structured and non-raising by contract.
4. **Idempotency:** Repeated terminal input (multiple `COMPLETE`/`ERROR`) is idempotent — first terminal wins, subsequent are no-ops. Malformed input is ignored (no crash).

**Action (done):** `docs/ADAPTER-CONTRACT.md` defined in both repos with mirrored integration tests.

---

### Block layout (Phase 7.1 — multi-content) — parity (2026-04-02)

- **Block layout adapters are sync-only:** `SvgBoundsAdapter` parses viewBox/width/height from SVG strings (pure regex, no DOM). `ImageSizeAdapter` returns pre-registered dimensions by src key (sync lookup, no I/O). No async measurement path. Browser users who need `getBBox()` or `Image.onload` should pre-measure and pass explicit dimensions on the content block. PY `ImageSizeAdapter` takes `dict[str, dict[str, float]]`; TS takes `Record<string, {width, height}>`.
- **Block layout graph shape parity:** Both TS and PY use identical 6-node graph: `state("blocks")`, `state("max-width")`, `state("gap")` → `derived("measured-blocks")` → `derived("block-flow")` → `derived("total-height")`. Meta on `measured-blocks`: `block-count`, `layout-time-ns` (phase-3 deferred, matching text layout pattern). Text blocks delegate to `analyze_and_measure`/`compute_line_breaks` internally — no separate `reactive_layout` subgraph mount.
- **Block content model divergence:** TS uses discriminated union `ContentBlock = { type: "text" | "image" | "svg", ... }` with optional inline fields. PY uses typed dataclasses `TextBlock`, `ImageBlock`, `SvgBlock` with `ContentBlock = TextBlock | ImageBlock | SvgBlock`. SVG dimensions: TS uses `viewBox?: { width, height }` object, PY uses `view_box?: tuple[float, float]`. Image dimensions: TS uses `naturalWidth?/naturalHeight?`, PY uses `natural_width?/natural_height?`. These are language-idiomatic adaptations, not behavioral differences.
- **SvgBoundsAdapter validation (Phase 7.1, resolved 2026-04-02):** Parsed viewBox width/height and fallback ``<svg>`` width/height must be finite and positive. TS uses ``Number.isFinite``; PY uses :func:`math.isfinite`. When attributes are present but numeric values are invalid, both ports raise a message distinct from the “no viewBox or width/height” case.
- **Block layout INVALIDATE + text adapter cache (Phase 7.1, resolved 2026-04-02):** PY ``reactive_block_layout`` invokes ``clear_cache`` only when ``callable(getattr(adapters.text, "clear_cache", None))``, matching ``reactive_layout`` and TS ``clearCache?.()``.
- **Block layout deferred items (partially resolved 2026-04-03):** (1) Adapter throw inside `derived("measured-blocks")` fn produces terminal `[[ERROR, err]]` with no recovery — resolved: add per-factory `resubscribable: bool` option (default `False`) to `reactive_layout` / `reactive_block_layout`. When true, adapter errors emit ERROR but the node can be re-triggered via INVALIDATE. (2) ~~Closure-held `measure_cache` survives `graph.destroy()`~~ — **resolved 2026-04-03:** `on_message` now clears `measure_cache` and calls `clear_cache()` on both INVALIDATE and TEARDOWN. (3) `SvgBoundsAdapter` regex may match nested `<svg>` elements or content inside XML comments/CDATA — resolved: strip `<!--...-->` and `<![CDATA[...]]>` before viewBox extraction; document single-root-SVG constraint; expose `SvgParser` protocol so users can opt in their own parser. (4) ~~`ImageSizeAdapter` returns mutable references~~ — **resolved 2026-04-03:** `measure_image` now returns `dict(dims)` (shallow copy).

---

### Runner protocol and async utilities (Phase 5.1 — asyncio/trio) — design decisions (2026-04-02)

- **No ThreadRunner default (pre-1.0):** Python removes `ThreadRunner` (daemon-thread-per-coroutine). Users must explicitly call `set_default_runner(AsyncioRunner.from_running())` or `set_default_runner(TrioRunner(nursery))`. TS has no equivalent concern (native async). If graphrefly-ts adds a non-browser runtime (e.g. Deno, Bun workers), a similar explicit runner setup should be required.
- **`call_soon_threadsafe` always (no loop-thread detection):** Async utilities always use `loop.call_soon_threadsafe()` for cross-boundary scheduling. The earlier `_is_loop_thread` optimization was removed because first-call heuristic was fundamentally broken (cached the wrong thread id if first called from a non-loop thread). If perf profiling shows `call_soon_threadsafe` as a bottleneck, revisit with `asyncio.get_running_loop() == loop` (try/except for non-async callers).
- **`to_async_iter` yields on both DATA and RESOLVED:** Yields the current value on `RESOLVED` (no-change cycles) in addition to `DATA` values. This ensures repeated identical values (e.g. `of(1, 2, 2)` feeding through a derived) are all yielded. TS should match if it adds an equivalent utility.
- **Cancel race prevention:** `AsyncioRunner` and `TrioRunner` use a `cancelled` flag checked inside `_create_task`/`_wrapper` to handle cancellation before the task/scope is created. TS equivalent should guard similarly.
- **Structured concurrency preservation:** Runner wrappers re-raise `CancelledError` (asyncio) and `Cancelled` (trio) instead of routing them to `on_error`. This preserves structured-concurrency contracts. TS should re-throw `AbortError` / framework-specific cancellation signals similarly.
