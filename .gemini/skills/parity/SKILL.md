---
name: parity
description: "Cross-language parity check between graphrefly-py and graphrefly-ts. Compares API surface, behavior, tests, and spec conformance. READ-ONLY — reports findings, never applies fixes without explicit approval. Use when user says 'parity', 'cross-lang check', or 'sync repos'."
---

You are executing the **parity** workflow, comparing **graphrefly-py** (this repo) against **graphrefly-ts** (`~/src/graphrefly-ts`).

User's context: $ARGUMENTS

---

## CRITICAL RULES (read before every phase)

1. **READ-ONLY until Phase 5.** You are comparing, not fixing. Do NOT edit any file until the user explicitly approves fixes in Phase 5.
2. **Spec is the authority.** `~/src/graphrefly/GRAPHREFLY-SPEC.md` decides what is correct. Not the TS code. Not the Python code. The spec.
3. **Report everything you find.** Do not filter, summarize, or skip "minor" differences. Present all findings and let the user decide.
4. **Stay in scope.** If the user specifies a feature area (e.g. "4.1 orchestration"), only check that area. If they say "full", check all implemented phases.
5. **No architectural decisions.** If you find a gap where neither the spec nor `docs/optimizations.md` has a clear answer, report it as "needs decision" — do NOT pick a resolution yourself.

---

## Phase 1: Scope & Gather

Determine scope from the user's input:
- If a **feature area** is given (e.g. "4.2 messaging", "guard", "batch"), focus only on that area.
- If `full`, check all phases that are checked off in BOTH roadmaps.

Read these files (parallelize all reads):

**From graphrefly-py (this repo):**
- `docs/roadmap.md` — which phases are complete
- `docs/optimizations.md` — cross-language notes and open decisions
- Source files in the scoped area under `src/graphrefly/`
- Test files in the scoped area under `tests/`

**From graphrefly-ts:**
- `~/src/graphrefly-ts/docs/roadmap.md` — which phases are complete
- `~/src/graphrefly-ts/docs/optimizations.md` — cross-language notes
- Source files in the scoped area under `~/src/graphrefly-ts/src/`
- Test files in the scoped area under `~/src/graphrefly-ts/src/__tests__/`

**Spec:**
- `~/src/graphrefly/GRAPHREFLY-SPEC.md` — sections relevant to the scoped area

After reading, list what you scoped and what files you read. Then proceed to Phase 2.

---

## Phase 2: API Surface Comparison

For the scoped area, compare the **public API** between Python and TS. Check each of these dimensions:

| Dimension | What to compare |
|-----------|----------------|
| **Function/method names** | Python `snake_case` vs TS `camelCase` — names should be equivalent after case conversion |
| **Signatures** | Parameters, their types, optionality, defaults |
| **Return types** | Node[T] vs Node<T>, Graph vs Graph, None vs void |
| **Options/kwargs** | Same option names (case-converted), same defaults, same validation |
| **Error behavior** | Same exception/error types, same conditions |
| **Exports** | Every public export in Python has a TS counterpart and vice versa |

Present findings as a table:

```
| Aspect | Python | TypeScript | Verdict |
|--------|--------|-----------|---------|
| topic() signature | topic(name, opts=None) | topic<T>(name, opts?) | ALIGNED |
| job_queue retry default | 5 | 3 | GAP — TS value is spec-correct |
| from_llm() | missing | exists | GAP — Py behind |
```

Use these verdicts:
- **ALIGNED** — equivalent behavior
- **GAP** — unintentional difference (one side wrong or behind)
- **INTENTIONAL** — language-idiomatic difference (e.g. Python `|` operator, `with batch():` context manager, per-subgraph `RLock`)

---

## Phase 3: Behavioral Semantics Check

For each **GAP** found in Phase 2, dig deeper:

1. Read the **implementation** on both sides
2. Read the **tests** on both sides
3. Check what the **spec** says (`~/src/graphrefly/GRAPHREFLY-SPEC.md`)
4. Check `docs/optimizations.md` for prior cross-language decisions

Classify each gap:
- **spec-decided** — spec clearly defines the behavior; one side is wrong. State which side and cite the spec section.
- **convention-decided** — `optimizations.md` already aligned this. State the convention.
- **needs-decision** — neither spec nor conventions cover this. Do NOT guess — flag it.

---

## Phase 4: Test Coverage Comparison

For the scoped area, compare test coverage:

1. List test files and test names on both sides
2. Identify scenarios tested in Python but NOT in TS (and vice versa)
3. For each missing test, classify:
   - **port** — the test should exist on both sides (same behavior, same edge case)
   - **language-specific** — test only makes sense on one side (e.g. Python thread-safety / free-threaded 3.14, TS async scheduling)

Present as a table:

```
| Test scenario | Python file:test | TS file:test | Verdict |
|--------------|-----------------|-------------|---------|
| thread-safe batch drain | test_concurrency.py:test_batch | (N/A) | LANGUAGE-SPECIFIC |
| 10 rapid orders no loss | (missing) | orchestration.test.ts:AC-1 | PORT to Python |
```

---

## Phase 5: Report (HALT)

Present ALL findings from Phases 2-4, grouped:

### Group 1: Gaps — one side needs a fix
For each: the gap, which repo needs the fix, what the fix is, effort estimate (S/M/L).

### Group 2: Test coverage gaps
For each: missing test, which repo, what the test should assert.

### Group 3: Needs Decision
For each: the gap, both behaviors, why the spec doesn't cover it, your recommended resolution (but the user decides).

### Group 4: Intentional Divergences (FYI only)
Language-specific differences that are correct on both sides.

**STOP HERE. Wait for the user to review and approve before proceeding.**

---

## Phase 6: Apply Fixes (only after user approval)

After the user approves specific fixes:

1. Apply fixes to the repo the user specifies
2. Run tests:
   - **This repo (Python):** `uv run pytest`
   - **Sibling repo (TS):** `cd ~/src/graphrefly-ts && pnpm test`
3. If any test fails, fix it. If a failure reveals a design question, HALT and ask.
4. Update `docs/optimizations.md` in BOTH repos:
   - Remove resolved gaps
   - Add any new decisions

---

## Phase 7: Final Verification

Run all checks and report results:

**Python:**
```bash
uv run pytest && uv run ruff check --fix src/ tests/ && uv run mypy src/
```

**TypeScript:**
```bash
cd ~/src/graphrefly-ts && pnpm test && pnpm run lint:fix && pnpm run build
```

Report pass/fail. If anything fails, fix it or HALT if it needs a decision.

---

## REMINDERS FOR FLASH-CLASS MODELS

These rules are critical. Re-read them if you are unsure at any point:

- **DO NOT edit files in Phase 1-5.** You are reading and reporting.
- **DO NOT resolve ambiguities yourself.** If the spec doesn't say, report "needs decision."
- **DO NOT skip the table format.** The user needs to scan findings quickly.
- **DO NOT summarize away details.** "A few minor differences" is not acceptable. List every difference.
- **DO** cite spec section numbers (e.g. "GRAPHREFLY-SPEC §1.3.5") when classifying gaps.
- **DO** show actual code snippets from both sides when the difference is subtle.
- **DO** note when a test exists on one side but not the other — test parity is as important as API parity.

## PYTHON-SPECIFIC CHECKS

When comparing Python against TS, also verify:
- **Thread safety:** Python uses per-subgraph `RLock`. Any new public API must be safe under concurrent access. TS doesn't have this concern (single-threaded).
- **Context managers:** Python uses `with batch():` instead of TS's `batch(() => ...)`. Verify the semantics are equivalent.
- **`|` pipe operator:** Python's `Node.__or__` must match TS's `pipe()` behavior.
- **Free-threaded Python 3.14:** Tests should pass with GIL disabled. This is a language-specific test axis.
- **Type annotations:** Python uses `TypedDict`, `Protocol`, `Literal` — verify these match TS's type structure in intent.
