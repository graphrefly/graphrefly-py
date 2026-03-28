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

### Open design items (low priority)

1. **`_is_cleanup_fn` / `isCleanupFn` treats any callable return as cleanup.** Both languages use `callable(value)` / `typeof value === "function"`. A compute function cannot emit a callable as a data value — it will be silently swallowed as cleanup. Fix: accept `{ cleanup: fn }` wrapper or add an opt-out flag. Low priority because the pattern is well-documented and rarely needed.

2. **Describe `type` before first run (operator vs derived).** Both ports: `describe_kind` / `describeKind` on node options and sugar (`effect`, `producer`, `derived`); operators that only use `down()`/`emit()` still infer via `_manual_emit_used` after a run unless `describe_kind="operator"` is set.

---

## Open design decisions (needs product/spec call)

These are tracked primarily in `graphrefly-ts/docs/optimizations.md`; listed here for cross-language visibility.

### A. `COMPLETE` when all dependencies complete

**Current behavior:** A node with dependencies and a compute `fn` may emit `[[COMPLETE]]` when **every** upstream dependency has emitted `COMPLETE`.

**Spec note:** `GRAPHREFLY-SPEC.md` §1.3.5 states that **effect** nodes complete when all deps complete — it does not necessarily require the same rule for derived/operator-style nodes.

**Decision needed:** Should auto-completion apply only to side-effect nodes (`fn` returns nothing), always, never, or behind an explicit option (e.g. `complete_when_deps_complete`)? TypeScript already exposes `completeWhenDepsComplete` as a node option (default `true`).

### B. More than 31 dependencies

**Resolved.** Python uses unlimited-precision `int` bitmasks natively. TypeScript uses a `BitSet` abstraction with `Uint32Array` fallback for >31 deps.

### C. `graph.disconnect` vs `NodeImpl` dependency lists (QA 1d #2)

**Current behavior:** Phase 1.1 `Graph.disconnect(from, to)` removes the `(from, to)` pair from the graph’s **edge registry** only. It does **not** mutate the target node’s constructor-time dependency list (`NodeImpl._deps` in Python; the fixed deps array inside `NodeImpl` in TypeScript). Upstream/downstream **message wiring** tied to those deps is unchanged.

**Why:** Dependencies are fixed when the node is created. True single-edge removal would require core APIs (partial upstream unsubscribe, bitmask width and diamond invariants, thread-safety on the Python side, etc.).

**Decision needed:** Is registry-only `disconnect` the long-term contract (documentation + `describe()` as source of truth), or should a later phase add **dynamic topology** so `disconnect` (or a new API) actually detaches one dep? Align with `GRAPHREFLY-SPEC.md` §3.3 when the spec is tightened.
