---
name: dev-dispatch
description: "Implement a feature or fix for graphrefly-py with planning, spec alignment, and self-test. Use when user says 'dispatch', 'dev-dispatch', 'implement', or provides a task. ALWAYS halts for approval before implementing. Run /parity afterward for cross-language check."
---

You are executing the **dev-dispatch** workflow for **graphrefly-py** (GraphReFly Python implementation).

The user's task/context is: $ARGUMENTS

---

## CRITICAL RULES (read before every phase)

1. **ALWAYS HALT after Phase 2.** Present your plan. Do NOT implement until the user approves.
2. **The spec is the authority.** `~/src/graphrefly/GRAPHREFLY-SPEC.md` decides behavior. Not your training data. Not the predecessor. The spec.
3. **Follow existing patterns.** Before writing new code, find the closest existing pattern in this repo and follow it. If you can't find one, say so in Phase 2.
4. **No `async def` / `Awaitable` in public APIs.** All public functions return `Node[T]`, `Graph`, `None`, or a plain synchronous value.
5. **No `datetime.now()` or `time.time()`.** Use `monotonic_ns()` or `wall_clock_ns()` from `src/graphrefly/core/clock.py`.
6. **All durations and timestamps are nanoseconds.** Backoff strategies return `int` (ns). Use `NS_PER_MS` / `NS_PER_SEC` from `graphrefly.extra.backoff` for conversions. Convert to seconds only at `threading.Timer` call sites.
7. **Messages are always `list[tuple[Type, Any] | tuple[Type]]`.** No single-tuple shorthand at API boundaries.
8. **Unknown message types forward.** Do not swallow unrecognized tuples.
9. **Thread safety is mandatory.** All public APIs must be safe under concurrent access with per-subgraph `RLock`.
10. **No imperative polling or internal timers for composition.** Sources like `from_http` must be one-shot reactive. If users need periodic behavior, they compose with `from_timer()`/`interval()` externally. Only time-domain primitives (`from_timer`, `interval`, `debounce`, `throttle`, `delay`, `timeout`) and resilience retry/rate-limiting may use raw `threading.Timer`.
11. **No imperative triggers in public APIs.** Use reactive `NodeInput` signals instead of imperative `.trigger()` or `.set()` methods where possible.
12. **Run tests before reporting done.** `uv run pytest` must pass.

---

## Phase 1: Context & Planning

Read these files to understand the task. **Parallelize all reads.**

**Always read:**
- `~/src/graphrefly/GRAPHREFLY-SPEC.md` — deep-read sections relevant to the task
- `docs/roadmap.md` — find the roadmap item for this task
- `docs/test-guidance.md` — testing checklist for the relevant layer

**Read if relevant:**
- `docs/optimizations.md` — if touching protocol, batch, node lifecycle, or parity
- Existing source files in the area you'll modify
- Existing tests for the area
- The closest existing pattern in this repo
- The **TypeScript sibling** at `~/src/graphrefly-ts/src/` — if the feature already exists in TS, your Python implementation should match its behavior and API shape (with `snake_case` naming)

**Optional predecessor reference:**
- `~/src/callbag-recharge` — use for analogous operator behavior. Map to GraphReFly APIs. The spec wins on conflicts.

After reading, proceed to Phase 2. Do NOT start implementing.

---

## Phase 2: Architecture Discussion (HALT)

**STOP and present your plan to the user.** Include:

### 2a. What you understand the task to be
Restate the task in your own words. If anything is unclear, ask here.

### 2b. Files you will create or modify
List every file path. For new files, state where they go and why.

### 2c. The pattern you are following
Name the existing file whose structure you will mirror. If matching a TS implementation, name both files:
- TS: `~/src/graphrefly-ts/src/patterns/orchestration.ts`
- Py pattern: `src/graphrefly/patterns/orchestration.py`

### 2d. Public API you will create
Show exact function signatures with type annotations:
```python
def topic(name: str, opts: TopicOptions | None = None) -> TopicBundle[T]: ...
```

### 2e. Internal graph topology (for domain factories)
If building a factory that returns a Graph, draw the internal node topology:
```
Graph("topic/{name}")
├── state("buffer") — reactive_log internal
├── derived("messages") — log_slice(buffer, -retention)
└── ...
```

### 2f. Tests you will write
List the test file and test names:
```
tests/test_messaging.py
- test_topic_publishes_and_subscribes
- test_late_subscriber_starts_at_current_position
- test_retention_evicts_oldest
- test_teardown_cascades_to_subscribers
```

### 2g. Spec conformance check
For each invariant below, state whether your design complies and how:
- [ ] Messages are `list[tuple[Type, Any] | tuple[Type]]`
- [ ] DIRTY before DATA/RESOLVED in two-phase push
- [ ] Unknown types forward
- [ ] No `async def` / `Awaitable` in return types
- [ ] Diamond resolution: recompute once after all deps settle
- [ ] Error handling: fn raises → `[[ERROR, err]]` downstream
- [ ] Thread-safe under per-subgraph `RLock`

### 2h. TS parity check
If the feature exists in TS (`~/src/graphrefly-ts`):
- List API name mappings (TS `camelCase` → Python `snake_case`)
- Note any intentional divergences (context managers, `|` operator, threading)
- Note any TS behaviors you need to match

### 2i. What you are NOT doing
Explicitly state what's out of scope.

**WAIT for user approval. Do NOT proceed to Phase 3 until approved.**

---

## Phase 3: Implementation

After user approves:

1. **Implement the code** following your approved plan from Phase 2
2. **Create tests** following `docs/test-guidance.md`:
   - Put tests in the most specific existing file, or create a new file as stated in Phase 2
   - Use `graph.observe()` for message assertions where applicable
   - Assert both behavior AND topology (`describe()` output)
   - Include a thread-safety test if the feature involves mutable state
3. **Export the new public API:**
   - Add to the appropriate `__init__.py`
   - Add to `__all__` if present
4. **Run tests:** `uv run pytest`
5. **Fix any failures**
6. **Run type check:** `uv run mypy src/graphrefly/` — fix type errors

---

## Phase 4: Self-Verification

Before reporting done, verify:

- [ ] `uv run pytest` passes (all tests, not just yours)
- [ ] `uv run mypy src/graphrefly/` passes
- [ ] Your code follows the pattern you stated in Phase 2c
- [ ] Your public API matches the signatures you stated in Phase 2d
- [ ] Your tests cover the scenarios you listed in Phase 2f
- [ ] No `async def` / `Awaitable` in return types
- [ ] No `datetime.now()` / `time.time()` usage
- [ ] All durations/timestamps use nanoseconds; seconds only at threading.Timer call sites
- [ ] No internal polling loops — sources are one-shot reactive, compose with from_timer() for periodic
- [ ] Messages are `list[tuple[Type, Any] | tuple[Type]]` — no shorthand
- [ ] Thread-safe under concurrent access

Report:
- Files created/modified
- New public exports
- Test results (pass count)
- Suggest running `/parity` to check TS alignment

---

## GUARDRAILS FOR FLASH-CLASS MODELS

These rules prevent common drift patterns. Re-read if unsure:

- **DO NOT add features beyond what was asked.** If the task says "implement `topic()`", implement `topic()`. Do not also implement `topic_bridge()` unless asked.
- **DO NOT add docstrings to code you didn't change.** Only document new public APIs.
- **DO NOT add error handling for impossible scenarios.** Trust internal code.
- **DO NOT create helpers or abstractions for one-time operations.**
- **DO NOT add backward-compat shims.** This is pre-1.0.
- **DO NOT use `async def` for any public function.** Wrap async at boundaries via `from_awaitable`/`from_async_iter`.
- **DO follow the file layout in GEMINI.md.** Core goes in `src/graphrefly/core/`, graph in `src/graphrefly/graph/`, operators in `src/graphrefly/extra/`, domain factories in `src/graphrefly/patterns/`.
- **DO use existing utilities.** Check `src/graphrefly/core/` and `src/graphrefly/extra/` before writing new ones.
- **DO check the TS sibling.** If the feature exists in `~/src/graphrefly-ts`, match its behavior.
- **DO use `nonlocal` for closure mutation.** Not `[value]` list-boxing (per SESSION-tier2-parity decision).
