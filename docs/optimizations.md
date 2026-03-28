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

### 8. `TEARDOWN` after terminal (`COMPLETE` / `ERROR`) — full pass-through (**decision B3**)

**Decision:** The terminal gate on `down()` **does not apply** to **`TEARDOWN`**. For a non-resubscribable node that has already reached `COMPLETE` or `ERROR`, a `down` payload that includes `TEARDOWN` must still:

1. Run normal **local lifecycle** for teardown (companion meta teardown, upstream disconnect, producer stop, etc.).
2. **Forward `TEARDOWN` to downstream sinks** (filter mixed payloads to teardown-only if needed).

| | |
|--|--|
| **Rationale** | `graph.destroy()` and resource cleanup must work after a node has terminated; §5.1 control flows **through** the graph — sinks may still need `TEARDOWN` after they saw `COMPLETE`/`ERROR`. |
| **TypeScript** | If `terminal && !resubscribable`, skip the early return when the payload contains `TEARDOWN`; handle lifecycle + emit teardown to sinks. |
| **Python** | Mirror in `NodeImpl.down` (or equivalent): teardown is not swallowed after terminal. |

### 9. Batch drain: partial apply before rethrow (**decision C1**)

**Decision:** Treat **best-effort drain** as the specified behavior: run **all** queued phase-2 callbacks with **per-callback** error isolation; surface the **first** error only **after** the queue is quiescent. Callers may observe a **partially updated** graph — this is **intentional** (prefer that to orphaned deferrals or fail-fast leaving dirty state). **Document** in module docstrings / spec prose; optional future knobs (`fail_fast`, `AggregateError`) are not required for parity.

| | |
|--|--|
| **Python** | Keep per-emission handling + `ExceptionGroup` (or first-error policy as chosen); document the partial-state contract explicitly. |
| **TypeScript** | JSDoc on `batch` / `drainPending` documents partial delivery + first error rethrown. |

---

## Summary

| Topic | Python | TypeScript |
|-------|--------|------------|
| Message tags | `StrEnum` | `Symbol` |
| Batch emit API | `emit_with_batch` (+ `dispatch_messages` alias) | `emitWithBatch` |
| Defer phase-2 | `defer_when`: `depth` vs `batching` | depth **or** draining (aligned with Py `batching`) |
| `isBatching` / `is_batching` | depth **or** draining | depth **or** draining |
| Batch drain resilience | per-emission try/catch, `ExceptionGroup` | per-emission try/catch, first error re-thrown |
| Nested `batch` throw + drain (**A4**) | Do **not** clear global queue while flushing | `!flushInProgress` guard before clear |
| `TEARDOWN` after terminal (**B3**) | Full lifecycle + emit to sinks | Same |
| Partial drain before rethrow (**C1**) | Document intentional | Document intentional (JSDoc) |
| Source `up` / `unsubscribe` | no-op | omitted |
| `fn` returns callable | cleanup | cleanup |
| Connect re-entrancy | `_connecting` | `connecting` (aligned) |

---

## Open design decisions (needs product/spec call)

These are tracked primarily in `graphrefly-ts/docs/optimizations.md`; listed here for cross-language visibility.

### A. `COMPLETE` when all dependencies complete

**Current behavior:** A node with dependencies and a compute `fn` may emit `[[COMPLETE]]` when **every** upstream dependency has emitted `COMPLETE`.

**Spec note:** `GRAPHREFLY-SPEC.md` §1.3.5 states that **effect** nodes complete when all deps complete — it does not necessarily require the same rule for derived/operator-style nodes.

**Decision needed:** Should auto-completion apply only to side-effect nodes (`fn` returns nothing), always, never, or behind an explicit option (e.g. `complete_when_deps_complete`)? TypeScript already exposes `completeWhenDepsComplete` as a node option (default `true`).

### B. More than 31 dependencies

**Resolved.** Python uses unlimited-precision `int` bitmasks natively. TypeScript uses a `BitSet` abstraction with `Uint32Array` fallback for >31 deps.
