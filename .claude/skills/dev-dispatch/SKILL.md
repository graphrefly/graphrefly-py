---
name: dev-dispatch
description: "Implement feature/fix with planning and self-test. Use when user says 'dispatch', 'dev-dispatch', or provides a task with implementation context. Supports --light flag for bug fixes and small changes. Run /qa afterward for code review and final checks."
disable-model-invocation: true
argument-hint: "[--light] [task description or context]"
---

You are executing the **dev-dispatch** workflow for **graphrefly-py** (Python).

The user's task/context is: $ARGUMENTS

### Mode detection

If `$ARGUMENTS` contains `--light`, this is **light mode**. Otherwise, this is **full mode**. Differences are noted inline per phase.

---

## Phase 1: Context & Planning

Load context and plan the implementation in a single pass. **Parallelize all reads.**

Read in parallel:

- **`docs/GRAPHREFLY-SPEC.md`** — primary behavioral authority; read sections relevant to the task
- **`docs/test-guidance.md`** — checklists for the layer you touch (protocol, node, graph, operators)
- **`docs/roadmap.md`** — phase alignment and acceptance criteria (read for new features or scope questions)
- **`archive/docs/SESSION-graphrefly-spec-design.md`** — design lineage, simplifications vs callbag-recharge, scenario validation
- Any files the user referenced in $ARGUMENTS
- Relevant source under `src/graphrefly/{core,graph,extra}/`
- Existing tests in `tests/`
- **Predecessor reference (patterns, tests, concurrency):** `~/src/callbag-recharge-py` — prior Python port (Phase 0–1 complete). Use for proven patterns, stress tests, operator semantics when porting, and subgraph-lock / concurrency lessons. This repo implements **GraphReFly**, not the older callbag protocol; treat callbag-recharge-py as a **reference implementation**, not the spec.
- **TypeScript reference (optional):** `~/src/callbag-recharge/` — original TS library for operator naming and edge cases when the spec is silent

While planning, explicitly validate proposed changes against these invariants (see **GRAPHREFLY-SPEC** for full detail):

- Messages are **always** `list[tuple[Type, Any] | tuple[Type]]` — no single-message shorthand
- **DIRTY** precedes **DATA** or **RESOLVED** within a batch; two-phase push for glitch-free diamonds
- **Batch** defers **DATA**, not **DIRTY**
- **COMPLETE** and **ERROR** are terminal; forward unknown message types
- Core reactive graph logic stays **synchronous** unless the task explicitly adds an async boundary (adapters / runners per roadmap)
- Prefer **typed** message/type enums and clear protocols over ad-hoc integers
- **Thread safety:** design for GIL and free-threaded Python where core APIs are documented as thread-safe (see roadmap Phase 0.4)
- **Diamond resolution** via bitmask (Python `int`) at convergence nodes

Do NOT start implementing yet.

---

## Phase 2: Architecture Discussion

### Full mode — HALT

**HALT and report to the user before implementing.** Present:

1. **Architecture assumptions** — how this fits `Graph`, `node`, and message flow
2. **New patterns** — anything not yet present under `src/graphrefly/`
3. **Options considered** — alternatives with pros/cons
4. **Recommendation** — preferred approach and why

Prioritize (in order):

1. **Correctness** — matches **GRAPHREFLY-SPEC** semantics
2. **Completeness** — edge cases (lifecycle, errors, reconnect if applicable)
3. **Consistency** — matches patterns in this repo and clear mapping from callbag-recharge-py when porting
4. **Simplicity** — minimal change
5. **Thread safety** — where concurrent `get()` / propagation applies

Do NOT treat backward compatibility as a primary constraint pre-1.0 unless the user says otherwise.

**Wait for user approval before proceeding.**

### Light mode — Skip unless escalation needed

Proceed directly to Phase 3 **unless** Phase 1 reveals any of:

- Changes to **message protocol** or global invariants in **GRAPHREFLY-SPEC**
- Changes to **node** primitive behavior, **Graph** container contracts, or **meta** companion stores
- Concurrency model changes (locks, batch isolation, defer_set)
- New patterns with non-obvious trade-offs

If any apply, escalate: HALT as in full mode.

---

## Phase 3: Implementation & Self-Test

After user approves (full mode) or after Phase 1 (light mode, no escalation):

1. **Implement**
   - Treat **GRAPHREFLY-SPEC** as non-negotiable for protocol behavior
   - When porting from **callbag-recharge-py**, map old callbag/STATE/DATA/END concepts to GraphReFly **Messages** and lifecycle types explicitly
   - Favor clean implementation over compatibility shims unless preserving a public API
   - Use `__slots__` on hot-path classes when appropriate
   - Type hints on public APIs; match **strict** mypy settings in `pyproject.toml`

2. **Tests** — follow **`docs/test-guidance.md`**
   - Add tests in the most specific existing file, or a new file aligned with roadmap layers (`test_protocol`, `test_core`, `test_graph`, …)
   - Cover protocol ordering, diamond resolution, lifecycle signals, and concurrency where relevant

3. **Run checks**

   ```bash
   uv run pytest && uv run ruff check src/ tests/ && uv run mypy src/
   ```

4. Fix any failures

When done, briefly list files changed and new exports. Suggest running **`/qa`** for adversarial review and final checks.
