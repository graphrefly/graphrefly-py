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

---

## Cross-language implementation notes

**Keep this section in sync with `graphrefly-ts/docs/optimizations.md` § Cross-language implementation notes** so you can open both files side by side.

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
| **disconnect** | Both ports **throw** if the edge was not registered. Dropping an edge does **not** remove constructor-time deps on the node (registry / future `describe()`). **See Open design decisions §C** (QA 1d #2). |
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
| **`observe()`** | `GraphObserveSource.subscribe` — one path: `sink(messages)`; whole graph: `sink(qualified_path, messages)`. Target order matches `signal` (mount-first, then sorted locals + sorted-key meta DFS). Graph-wide subscription order vs TS may still differ for **primary** nodes (TS `localeCompare` on full paths). |
| **Describe `type`** | Both: `describe_kind` / `describeKind` on node options; sugar constructors (`effect`, `producer`, `derived`) set it; `_infer_describe_type` / `inferDescribeType` prefers explicit kind when set. |
| **`describe().nodes`** | Both strip `name` from per-node entries (dict key is the qualified path). |
| **`describe().subgraphs`** | Both recursively collect all nested mount paths. |
| **`connect` self-loop** | Both reject `connect(x, x)` before dep validation. |
| **`signal` / `_collect_observe_targets` ordering** | Both sort local nodes and mounts by name within each graph level. |

### 16. `Graph` Phase 1.4 lifecycle & persistence (`destroy`, `snapshot`, `restore`, `from_snapshot`, `to_json`)

**Aligned:**

| | |
|--|--|
| **`destroy()`** | Both: `signal([[TEARDOWN]])` then clear all registries recursively through mounts. |
| **`snapshot()`** | Both: `{ version: 1, ...describe() }` — flat `version` field, sorted `nodes` keys. |
| **`restore(data)`** | Both: validate `data.name` matches graph name; skip `derived`/`operator`/`effect` types; silently ignore unknown/failing paths. |
| **`from_snapshot(data, build?)`** | Both: optional `build` callback registers topology before `restore()` applies values. Without `build`, both use registry-based reconstruction (mounts → topo node creation via factories → edges → restore). |
| **`to_json()` / `toJSON()`** | Python returns compact JSON **string** with trailing newline. TS returns a plain sorted-key **object** (for `JSON.stringify(graph)`). Language-appropriate. |
| **`toJSONString()`** | TS only — `JSON.stringify(toJSON()) + "\n"`. Python's `to_json()` serves the same role. |

**Intentional divergence:**

| Topic | Python | TypeScript | Rationale |
|-------|--------|------------|-----------|
| `to_json` return type | `to_json()` → `str` (no universal `__json__` hook in Python) | `toJSON()` → plain object (ECMAScript `JSON.stringify` protocol) | Language idiom |
| `_parse_snapshot_envelope` | Validates `version`, `name`, `nodes`, `edges`, `subgraphs` types | Only validates `data.name` match | Python is stricter; both correct |

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
| Drain cycle detection | TBD | `MAX_DRAIN_ITERATIONS = 1000` cap |
| TEARDOWN → `"disconnected"` status | `_status_after_message` maps TEARDOWN | `statusAfterMessage` maps TEARDOWN |
| DIRTY→COMPLETE settlement (D2) | `_run_fn()` when no dirty deps remain but node is dirty | `_runFn()` when no dirty deps remain but node is dirty |
| Describe slice + frozen meta | `describe_node`, `MappingProxyType` | `describeNode` via `instanceof NodeImpl`, `Object.freeze(meta)` |
| Node internals | Class-based `NodeImpl`, all methods on class | Class-based `NodeImpl`, V8 hidden class optimization, prototype methods |
| Dep-value identity check | Before cleanup (skip cleanup+fn on no-op) | Before cleanup (skip cleanup+fn on no-op) |
| `INVALIDATE` (§1.2) | Cleanup + clear `_cached` + `_last_dep_values`; terminal passthrough (§9); no auto recompute | Same |
| `Graph` Phase 1.1 | `thread_safe` + `RLock`; TEARDOWN after unlock on `remove`; `disconnect` vs `_deps` → §C | Registry only; `connect` / `disconnect` errors aligned; see §C |
| `Graph` Phase 1.2 | Aligned: `::` path separator, mount `remove` + subtree TEARDOWN, qualified paths, `edges()`, signal mounts-first, `resolve` strips leading name, `:` in names OK; see §14 | Same; see §14 |
| `Graph` Phase 1.3 | `describe`, `observe`, `GRAPH_META_SEGMENT`, `signal`→meta, `describe_kind` on sugar; see §15 | TS: `describe()`, `observe()`, `GRAPH_META_SEGMENT`, `describeKind` on sugar; see graphrefly-ts §15 | `observe()` order: TS full-path `localeCompare` vs Py per-level sort (§15) |
| `Graph` Phase 1.4 | `destroy`, `snapshot` (flat `version: 1`), `restore` (name check + type filter + silent catch), `from_snapshot(data, build=)`, `to_json()` → str + `\n`; see §16 | `destroy`, `snapshot`, `restore`, `fromSnapshot(data, build?)`, `toJSON()` → object, `toJSONString()` → str + `\n`; see §16 |
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
| Extra Phase 3.1 (resilience) | `graphrefly.extra.{backoff,resilience,checkpoint}`; see §6 below | `src/extra/{backoff,resilience,checkpoint}.ts`; see §6 below |
| Extra Phase 3.2 (data structures) | `graphrefly.extra.data_structures` (`reactive_map`, …); see §17 | `reactiveMap` + `reactive-base` (`Versioned` snapshots); see §17 |

### 18. Inspector causality hooks (Phase 3.3 observe extensions)

| Topic | Python | TypeScript |
|-------|--------|------------|
| Core hook shape | `NodeImpl._set_inspector_hook()` installs an internal, opt-in hook with `dep_message` and `run` events. | `NodeImpl._setInspectorHook()` mirrors the same hook contract (`dep_message`, `run`). |
| Runtime overhead | Hook pointer is `None` by default; no event allocation unless `observe(..., timeline/causal/derived)` is active. | Hook pointer is `undefined` by default; no event allocation unless `observe(name, { timeline/causal/derived })` is active. |
| Graph usage | `observe(name, timeline=True, causal=True, derived=True)` enriches structured events with `in_batch`, trigger dep metadata, and dep snapshots (graph-wide structured supported). | `observe(name, { timeline, causal, derived })` uses the same hook-driven enrichment model (graph-wide structured supported). |

Parity hardening (2026-03-30): both ports now keep `data` / `resolved` events under `causal` even when no trigger index is known yet, always emit `derived` on every `run`, and set `completed_cleanly` / `completedCleanly` only when no prior `ERROR` was seen. Structured timeline timestamps use `timestamp_ns` in both ports (nanoseconds). `ObserveResult.values` is latest-by-path map in both ports.

### 19. Inspector helper parity (reasoning trace + diagram export)

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

### 20. `reachable(...)` parity decisions (2026-03-30)

| Topic | Python | TypeScript |
|-------|--------|------------|
| Signature style | `reachable(described, from_path, direction, *, max_depth=None)` | `reachable(described, from, direction, { maxDepth? })` |
| Direction validation | Runtime guard: only `"upstream"` / `"downstream"` accepted; invalid raises | Runtime guard: only `"upstream"` / `"downstream"` accepted; invalid throws |
| Depth validation | Integer-only `max_depth >= 0` (`0` returns `[]`; rejects `bool`) | Integer-only `maxDepth >= 0` (`0` returns `[]`) |
| Malformed payload handling | Defensive: non-dict `nodes` / non-list `edges` treated as empty; malformed edges skipped | Defensive: same behavior (`nodes`/`edges` normalized, malformed entries skipped) |
| Traversal semantics | BFS over `deps` + `edges`; upstream = deps+incoming, downstream = reverse-deps+outgoing | Same |
| Output ordering | Lexical code-point ordering via `sorted()` | Lexical code-point ordering (stable, locale-independent) |

### 21. Centralised clock utilities (`core/clock`) — parity (2026-03-30)

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
| Timer base | `monotonic_ns()` (nanoseconds via `time.monotonic_ns()`) | `monotonicNs()` (nanoseconds via `performance.now()`) | Both centralised in `core/clock`; nanosecond internal tracking |
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

### Open design items (low priority)

1. **`_is_cleanup_fn` / `isCleanupFn` treats any callable return as cleanup.** Both languages use `callable(value)` / `typeof value === "function"`. A compute function cannot emit a callable as a data value — it will be silently swallowed as cleanup. Fix: accept `{ cleanup: fn }` wrapper or add an opt-out flag. Low priority because the pattern is well-documented and rarely needed.

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
   | `describe_kind` | Both: source factories use `"producer"` (not `"operator"`). |
   | Static source timing | Both: synchronous emission during producer start (no deferred microtask). |

   **Intentional divergences:**

   | Topic | Python | TypeScript | Rationale |
   |-------|--------|------------|-----------|
   | `from_event_emitter` / `fromEvent` | `from_event_emitter(emitter, event, add_method=, remove_method=)` — generic emitter | `fromEvent(target, type, opts?)` — DOM `addEventListener` API | Language ecosystem |
   | `to_list` (blocking) | Py-only: blocks via `threading.Event`, returns `list` | N/A — use `await firstValueFrom(toArray(src))` | Py sync bridge |
   | `first_value_from` | Py-only: sync bridge | `firstValueFrom`: `Promise<T>` | Language concurrency model |
   | `from_awaitable` / `fromPromise` | `from_awaitable`: worker thread + `asyncio.run` | `fromPromise`: native Promise | Language async model |
   | `from_async_iter` / `fromAsyncIter` | Worker thread + `asyncio.run` | Native async iteration | Language async model |
   | AbortSignal on async sources | Not supported (deferred) | `signal` option on `fromTimer`, `fromPromise`, `fromAsyncIter` | TS has native AbortSignal; Py deferred |

   **Open:** Python AbortSignal equivalent (e.g. `threading.Event` signal parameter) — deferred to future parity round.

---

## Open design decisions (needs product/spec call)

These are tracked primarily in `graphrefly-ts/docs/optimizations.md`; listed here for cross-language visibility.

### A. `COMPLETE` when all dependencies complete

**Current behavior:** A node with dependencies and a compute `fn` may emit `[[COMPLETE]]` when **every** upstream dependency has emitted `COMPLETE`.

**Spec note:** `~/src/graphrefly/GRAPHREFLY-SPEC.md` §1.3.5 states that **effect** nodes complete when all deps complete — it does not necessarily require the same rule for derived/operator-style nodes.

**Decision needed:** Should auto-completion apply only to side-effect nodes (`fn` returns nothing), always, never, or behind an explicit option (e.g. `complete_when_deps_complete`)? TypeScript already exposes `completeWhenDepsComplete` as a node option (default `true`).

### B. More than 31 dependencies

**Resolved.** Python uses unlimited-precision `int` bitmasks natively. TypeScript uses a `BitSet` abstraction with `Uint32Array` fallback for >31 deps.

### C. `graph.disconnect` vs `NodeImpl` dependency lists (QA 1d #2)

**Current behavior:** Phase 1.1 `Graph.disconnect(from, to)` removes the `(from, to)` pair from the graph’s **edge registry** only. It does **not** mutate the target node’s constructor-time dependency list (`NodeImpl._deps` in Python; the fixed deps array inside `NodeImpl` in TypeScript). Upstream/downstream **message wiring** tied to those deps is unchanged.

**Why:** Dependencies are fixed when the node is created. True single-edge removal would require core APIs (partial upstream unsubscribe, bitmask width and diamond invariants, thread-safety on the Python side, etc.).

**Decision needed:** Is registry-only `disconnect` the long-term contract (documentation + `describe()` as source of truth), or should a later phase add **dynamic topology** so `disconnect` (or a new API) actually detaches one dep? Align with `~/src/graphrefly/GRAPHREFLY-SPEC.md` §3.3 when the spec is tightened.

### D. Tier-2 time operators — `asyncio` vs wall-clock timers

**Current Python design (intentional):** `graphrefly.extra.tier2` uses wall-clock **`threading.Timer`**. Callbacks emit via **`Node.down(..., internal=True)`**, which takes the **subgraph write lock** when **`thread_safe`** is true (default), so timer threads stay consistent with synchronous graph work **without** requiring a running **`asyncio`** loop.

**Open decision:** Whether to add optional **`asyncio`**-based scheduling later (e.g. **`loop.call_soon_threadsafe`** and loop-backed delays) so time-based operators integrate cleanly with apps that already own a **running event loop**, while keeping **`threading.Timer`** as the default portable baseline.

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

---

## Deferred follow-ups (QA)

Non-blocking items tracked for later; not optimizations per se. Keep this section **identical** in `graphrefly-ts/docs/optimizations.md` and here (aside from language-specific labels in the first table).

| Item | Notes |
|------|-------|
| **`lastDepValues` + `Object.is` / referential equality** | Skips `fn` when dep snapshots are referentially equal. Fine for immutable values; misleading if deps are mutated in place. |
| **`sideEffects: false` in `package.json`** | TypeScript package only. Safe while the library has no import-time side effects. Revisit if global registration or polyfills are added at module load. |
| **JSDoc / docstrings on `node()` and public APIs** | `docs/docs-guidance.md`: JSDoc on new TS exports; docstrings on new Python public APIs. |
| **Roadmap §0.3 checkboxes** | Mark Phase 0.3 items when the team agrees the milestone is complete. |

### Tier 1 extra operators (roadmap 2.1) — deferred semantics (QA)

Applies to `graphrefly-ts` `src/extra/operators.ts` and `graphrefly.extra.tier1`. **Keep the table below identical in both repos’ `docs/optimizations.md`.**

| Item | Notes |
|------|-------|
| **`takeUntil` / `take_until` + notifier `DIRTY`** | Decide whether the first notifier signal that ends the primary should be any protocol tuple (e.g. a lone `DIRTY`) or only phase-2 / `DATA` (Rx-style “next”). Implementations may differ until aligned. |
| **`zip` + partial queues** | When one inner source completes, buffered values that never formed a full tuple are dropped; downstream then completes. Document if stricter Rx parity is required. |
| **`concat` + `ERROR` on the second source before the first completes** | Phase gating ignores the second source until the first completes; an `ERROR` on the second during phase 0 may be swallowed until phase 1. Decide whether tail-source errors should short-circuit early. |
| **`race` + pre-winner `DIRTY`** | Before the first winning `DATA`, `DIRTY` (and other tuples) may be forwarded from more than one inner source (TypeScript: `take(merge(...), 1)`; Python: multi-dep `on_message`). JSDoc on TS `race` notes this; a stricter “winner-only” behavior would need a different implementation in either port. |

### Tier 2 extra operators (roadmap 2.2) — deferred semantics (QA)

Applies to `src/extra/operators.ts` and `graphrefly.extra.tier2`. **Keep the table below identical in both repos’ `docs/optimizations.md`.**

| Item | Notes |
|------|-------|
| **`sample` + `undefined` as `T`** | Sampling uses the primary dep’s cached value (`get()`). If `T` allows `undefined`, a cache of `undefined` is indistinguishable from “no snapshot yet”; TypeScript currently emits `RESOLVED` instead of `DATA` in that case (JSDoc `@remarks`). Decide whether both ports should adopt an explicit optional/sentinel, or document the limitation only. |
| **`mergeMap` / `merge_map` + `ERROR`** | When the outer stream or one inner emits `ERROR`, other inner subscriptions may keep running until they complete or unsubscribe. Rx-style “first error cancels all sibling inners” is **not** specified or implemented; align if product wants fail-fast teardown across active inners. |

---
