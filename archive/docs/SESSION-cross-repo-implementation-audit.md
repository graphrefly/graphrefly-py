---
SESSION: cross-repo-implementation-audit
DATE: March 29, 2026 (updated — batch 13–15 remediation, dynamicNode, operator dep-based rewrite, xfail resolution)
TOPIC: Same audit program as graphrefly-ts — Python repo companion log
REPO: graphrefly-py
CANONICAL: ~/src/graphrefly-ts/archive/docs/SESSION-cross-repo-implementation-audit.md
ARTIFACTS: ~/src/graphrefly-ts/docs/audit-plan.md, ~/src/graphrefly-ts/docs/batch-review/*.md
---

## CONTEXT

The **cross-repo implementation audit** is orchestrated from **graphrefly-ts** (`docs/audit-plan.md`). All batch prompts and written reports live under **`~/src/graphrefly-ts/docs/batch-review/`** even when the prompt’s working directory is `graphrefly-py`. This file records **Python-specific** takeaways and points to the canonical session for the full batch table, synthesized findings, and open work.

---

## PYTHON-SPECIFIC HIGHLIGHTS

### Compliance notes (from batch 1)

- **Batch deferral during drain:** Python `emit_with_batch` + `defer_when="depth"` does **not** match TypeScript’s `isBatching()` (includes `flushInProgress`). Impact: possible phase-1/phase-2 ordering differences during nested drain. See `batch-1.md` and `protocol.py` (docstring claims should be reconciled with TS).
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

**Verification snapshot:** 302 passed, 1 skipped, **0 xfailed**.

### Related prior session

- Custom message types and closed `StrEnum` in Python were analyzed in **`SESSION-web3-research-and-type-extensibility.md`** in the **graphrefly-ts** archive (cross-repo type story). The graphrefly-py index links to TS for that file path.

---

## BATCH STATUS

Identical to the canonical table in **`~/src/graphrefly-ts/archive/docs/SESSION-cross-repo-implementation-audit.md`**. Batches 12 and 16 were not yet reported as of March 29, 2026. Batches 13–15 are fully remediated.

---

## READING GUIDE

1. Read **`~/src/graphrefly-ts/archive/docs/SESSION-cross-repo-implementation-audit.md`** for the full narrative.
2. Use **`~/src/graphrefly-ts/docs/batch-review/batch-N.md`** for evidence and line citations affecting both repos.
3. Use **`batch-4-8-processed-result.md`** for the consolidated fix list and remaining P2 backlog.
