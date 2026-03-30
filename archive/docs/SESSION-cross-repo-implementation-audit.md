---
SESSION: cross-repo-implementation-audit
DATE: March 30, 2026 (updated — batch 16 companion + Tier 2 parity tests; prior: batches 10 + 12 full remediation)
TOPIC: Same audit program as graphrefly-ts — Python repo companion log
REPO: graphrefly-py
CANONICAL: ~/src/graphrefly-ts/archive/docs/SESSION-cross-repo-implementation-audit.md
ARTIFACTS: ~/src/graphrefly-ts/docs/audit-plan.md, ~/src/graphrefly-ts/docs/batch-review/*.md
---

## CONTEXT

The **cross-repo implementation audit** is orchestrated from **graphrefly-ts** (`docs/audit-plan.md`). All batch prompts and written reports live under **`~/src/graphrefly-ts/docs/batch-review/`** even when the prompt's working directory is `graphrefly-py`. This file records **Python-specific** takeaways and points to the canonical session for the full batch table, synthesized findings, and open work.

---

## PYTHON-SPECIFIC HIGHLIGHTS

### Compliance notes (from batch 1)

- **Batch deferral during drain:** Python `emit_with_batch` + `defer_when="depth"` does **not** match TypeScript's `isBatching()` (includes `flushInProgress`). Impact: possible phase-1/phase-2 ordering differences during nested drain. See `batch-1.md` and `protocol.py` (docstring claims should be reconciled with TS).
- **Message typing:** Local `Message` widening in `node.py` vs `protocol.py` — runtime OK; type-hygiene note in batch 1.

### Fixes applied (batches 4–8 roll-up)

See **`~/src/graphrefly-ts/docs/batch-review/batch-4-8-processed-result.md`** for the authoritative list. Python items included: `PubSubHub.remove_topic`, reactive log `max_size` / `append_many` / `trim_head`, direct-form `retry` / `rate_limiter`, PEP 695 `type` aliases, `__all__` on `graph.graph` and `extra.cron`, `_MAX_DRAIN_ITERATIONS` in batch drain.

### Test and implementation gaps — RESOLVED (batch 13–15 remediation)

- **`tests/test_edge_cases.py`:** All 4 **xfail** markers **removed**. The root cause was that `switch_map`, `concat_map`, `exhaust_map`, `flat_map` used the **producer pattern** (`node(start_fn)`) which manually subscribed to outer and never processed the outer's initial value. TS uses the **dep-based pattern** (`node([source], fn, onMessage=handler)`) where the compute function receives the dep's value at connect time.
- **Resolution:** All 4 higher-order operators rewritten to dep-based pattern matching TS. Added `_forward_inner()` helper (matching TS `forwardInner`). Inner error forwarding and outer-complete waiting now work correctly.
- **Checkpoint / SQLite:** Concurrent write safety still called out as **P1** in the processed roll-up (mutex or documented single-writer contract).

### Batch 13–15 remediation — Python-specific changes

1. **Operator architecture rewrite (`src/graphrefly/extra/tier2.py`):**
   - `switch_map`, `concat_map`, `flat_map`, `exhaust_map` → `node([outer], compute_fn, on_message=handler)` pattern
   - `_forward_inner()` helper: subscribes to inner, forwards all messages except COMPLETE, emits inner's initial value
   - `mergeMap` concurrent option with buffer queue
   - RxJS aliases: `debounce_time`, `throttle_time`, `catch_error`, `merge_map`

2. **Operator API alignment (`src/graphrefly/extra/tier1.py`):**
   - `combine`, `merge`, `zip`, `race` → variadic `*sources`
   - `tap` → observer dict overload (`data`, `error`, `complete` keys)
   - `distinct_until_changed` default: `operator.is_` → `operator.eq`
   - `scan` default equality: `operator.is_` → `operator.eq`
   - `combine_latest` alias

3. **`dynamic_node` primitive (`src/graphrefly/core/dynamic_node.py`):**
   - Full implementation: `DynGet` proxy, `DynamicNodeFn`, `DynamicNodeImpl`
   - Dep tracking via `get()` proxy, dep diffing + rewire, re-entrancy guard
   - Two-phase DIRTY/RESOLVED handling, diamond resolution
   - 7 tests in `tests/test_dynamic_node.py`

4. **Graph inspector (`src/graphrefly/graph/graph.py`):**
   - `describe()` with `filter` parameter (dict or callable)
   - `observe()` structured mode returning `ObserveResult`
   - `annotate()`, `trace_log()`, `diff()`, `inspector_enabled`

5. **Misc fixes:**
   - `src/graphrefly/extra/sources.py` — `share_replay` alias
   - `src/graphrefly/extra/data_structures.py` — TTL `<= 0` validation
   - `src/graphrefly/extra/checkpoint.py` — `_check_json_serializable()` warning

6. **Tests:**
   - `tests/test_edge_cases.py` — 4 xfail removed, all 19 pass
   - `tests/test_extra_tier2.py` — updated for dep-based initial value processing
   - `tests/test_dynamic_node.py` — 7 new tests

**Verification snapshot (post batch 13–15):** 302 passed, 1 skipped, **0 xfailed**.

---

## BATCH 10 (Py DOCS) — REMEDIATION (March 30, 2026)

**Report:** `~/src/graphrefly-ts/docs/batch-review/batch-10.md`

### Google-style docstring standardization — 14 files

Every public function/class now has: one-line imperative summary, `Args:` section, `Returns:` (omitted for `None`), `Example:` with ```python block.

| File | Items fixed |
|------|-------------|
| `src/graphrefly/core/sugar.py` | `state`, `producer`, `derived`, `effect`, `pipe` |
| `src/graphrefly/core/node.py` | `node`, `SubscribeHints` |
| `src/graphrefly/core/protocol.py` | `batch`, `is_batching`, `emit_with_batch` |
| `src/graphrefly/core/meta.py` | `meta_snapshot`, `describe_node` |
| `src/graphrefly/core/guard.py` | `policy`, `compose_guards` |
| `src/graphrefly/graph/graph.py` | `Graph` class + 16 public methods + `reachable` |
| `src/graphrefly/extra/tier2.py` | 21 operators (switch_map, concat_map, flat_map, exhaust_map, debounce, throttle, timeout, window_time, sample, audit, delay, buffer, buffer_count, buffer_time, interval, repeat, gate, pausable, rescue, window, window_count) |
| `src/graphrefly/extra/sources.py` | `of`, `empty`, `never`, `throw_error`, `from_iter`, `from_timer`, `from_any`, `for_each`, `to_list`, `first_value_from`, `share`, `replay` |
| `src/graphrefly/extra/resilience.py` | `CircuitOpenError`, `retry`, `with_breaker`, `token_tracker`, `rate_limiter`, `with_status` |
| `src/graphrefly/extra/backoff.py` | `constant`, `linear`, `exponential`, `fibonacci`, `resolve_backoff_preset` |
| `src/graphrefly/extra/checkpoint.py` | 4 adapter classes + 3 helper functions |
| `src/graphrefly/extra/data_structures.py` | `ReactiveMapBundle`, `ReactiveLogBundle`, `ReactiveIndexBundle`, `ReactiveListBundle`, `PubSubHub`, `reactive_map`, `reactive_log`, `reactive_index`, `reactive_list`, `pubsub`, `log_slice` |
| `src/graphrefly/extra/cron.py` | `CronSchedule`, `parse_cron`, `matches_cron` |

### Export fix

`SpyHandle` added to `src/graphrefly/__init__.py` imports block and `__all__`. `from graphrefly import SpyHandle` now works.

### Roadmap

Phase 7 `- [ ] README` changed to `- [x] README` in `docs/roadmap.md`.

### README rewrite

- `## Requirements`: Python 3.12+ (from `pyproject.toml`)
- Quickstart: replaced `print(__version__)` with 30-line runnable example (state/derived/for_each/push/Graph/snapshot/first_value_from)
- `## Dev setup`: `mise trust && mise install` + `uv sync` (drawn from `CONTRIBUTING.md`)

### `first_value_from` cross-language parity note

- `Note:` Google-style section added to `src/graphrefly/extra/sources.py:first_value_from` docstring explaining synchronous (Py) vs Promise (TS) semantics
- `## Cross-language note` section added to `website/src/content/docs/api/first_value_from.md`

---

## BATCH 12 (Py TESTS) — REMEDIATION (March 30, 2026)

**Report:** `~/src/graphrefly-ts/docs/batch-review/batch-12.md`

### New tests — `tests/test_core.py`

- `test_terminal_blocks_later_data_when_not_resubscribable` — `resubscribable=False` node errors on first call; subsequent upstream pushes produce no DATA/DIRTY/RESOLVED. `# Spec: GRAPHREFLY-SPEC §1.3.4`

### New tests — `tests/test_extra_tier1.py` (26 tests)

- `test_merge_completes_after_all_sources_complete` — first-source COMPLETE does not terminate merged stream; terminates only after all sources complete. `# Spec: GRAPHREFLY-SPEC §1.3.5`
- **Tier 1 matrix (5 operators × 5 behaviors):** `map`, `filter`, `scan`, `take`, `combine`:
  - `_dirty_propagation` — DIRTY arrives before DATA
  - `_resolved_suppression` — same-value push yields RESOLVED; downstream fn call count unchanged
  - `_error_propagation` — upstream ERROR flows through
  - `_complete_propagation` — upstream COMPLETE flows through (note: `take` uses `complete_when_deps_complete=False`; COMPLETE fires on quota exhaustion, not upstream COMPLETE)
  - `_reconnect` — unsubscribe + resubscribe + fresh values received (note: `take` closure counter not reset; test verifies values flow post-reconnect)

### New tests — `tests/test_extra_tier2.py` (3 tests)

- `test_switch_map_reconnect_fresh_inner` — stale inner does not deliver after teardown + reconnect
- `test_debounce_teardown_cancels_timer` — advancing time post-unsubscribe suppresses stale emission
- `test_concat_map_reconnect_fresh_queue` — new inner created and delivers correctly after reconnect + new outer DATA (note: closure-level queue not erased; test accounts for this)

### New file — `tests/test_regressions.py`

Module-level comment establishes the `test_<stable_bug_name>` + `# Spec: §x.x` convention.

| Test | Spec |
|------|------|
| `test_resolved_transitive_skip_does_not_rerun_downstream` | §1.3.3 |
| `test_diamond_recompute_count_through_operators` | §2 |
| `test_describe_matches_appendix_b_schema` (manual validator, no `jsonschema` dep) | Appendix B |

**Verification snapshot (post batch 12):** **362 passed, 1 pre-existing skip, 0 failures**.

---

## BATCH 16 (PHASE G — INTEGRATION STRESS) — COMPANION (March 30, 2026)

**Canonical report (TS repo):** `~/src/graphrefly-ts/docs/batch-review/batch-16.md`.

Batch 16 is an **audit-design** batch: ten cross-layer integration scenarios (batch + graph + diamond, operators inside graphs, pause vs timers, snapshot/mount, guards, errors, threading, RESOLVED chains, stress, snapshot during batch). Each entry documents risk, failure mode, gap rationale, and **TS + Py pseudocode**. No Python-only report file; read the shared `batch-16.md`.

**Python code changes (same initiative as TS — Tier 2 parity, `SESSION-tier2-parity-nonlocal-forward-inner`):**

| File | Changes |
|------|---------|
| `tests/test_extra_tier2.py` | Helpers `_flatten_types`, `_global_dirty_before_phase2`; `switch_map` derived-inner duplicate-initial-`DATA` guard; protocol-order tests for `switch_map` / `concat_map` / `flat_map` / `exhaust_map` (sink cleared after outer attach before inner two-phase push); void-inner `None` ordering through `concat_map` / `flat_map` / `exhaust_map`; `rescue` ∘ `switch_map`; `debounce` + `batch()` defers emission until batch exits |
| `tests/test_regressions.py` | `test_switch_map_forward_inner_does_not_duplicate_derived_initial_data` — `# Spec: GRAPHREFLY-SPEC §2` |

**Harness note:** After subscribe, `_forward_inner` may emit settled inner `DATA` before any scripted `DIRTY`; tests that assert global `DIRTY`-before-`DATA` on an inner push must **clear** the capture buffer after the outer selects the inner (or assert on a sliced window). This matches TS `exhaustMap` matrix pattern (clear batches after initial attach).

**Verification:** `uv run pytest tests/test_extra_tier2.py tests/test_regressions.py` — green (counts drift).

---

## BATCH STATUS

| Batch | Status (as of March 30, 2026) |
|-------|-------------------------------|
| 1–8 | Done + processed roll-up |
| 9 | Done (two TS remediation passes) |
| 10 | Done (Py remediated March 30) |
| 11 | Done (two TS remediation passes) |
| 12 | Done (Py remediated March 30) |
| 13–15 | Done (remediated March 29) |
| 16 | Done — report in TS `docs/batch-review/batch-16.md`; Py tests in `test_extra_tier2.py` + `test_regressions.py` |

Full batch table with reports is in the **canonical TS session file**.

---

## OPEN WORK (Python-specific)

1. **Batch drain deferral parity** (batch 1): `defer_when="depth"` vs TS `isBatching()` — resolve or spec-clarify.
2. **SQLite checkpoint thread-safety** — P1: mutex or document single-writer contract.
3. **Tier 2 test matrix** — teardown/reconnect (batch 12) plus batch-16–adjacent protocol / `_forward_inner` / void-inner / rescue / debounce+batch cases in `test_extra_tier2.py` and `test_regressions.py`. Full TS-style per-operator matrix still optional follow-on.
4. **`test_describe_matches_appendix_b_schema`** — uses manual shape validator; consider `jsonschema` if Appendix B schema stabilizes.
5. **§1.4 direction enforcement** — neither repo enforces up/down direction; spec clarification pending.

---

## READING GUIDE

1. Read **`~/src/graphrefly-ts/archive/docs/SESSION-cross-repo-implementation-audit.md`** for the full narrative, batch table, and synthesized findings.
2. Use **`~/src/graphrefly-ts/docs/batch-review/batch-N.md`** for evidence and line citations affecting both repos.
3. Use **`batch-4-8-processed-result.md`** for the consolidated fix list and remaining P2 backlog.
