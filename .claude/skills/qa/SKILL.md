---
name: qa
description: "Adversarial code review, apply fixes, final checks (test/lint/typecheck), and doc updates. Run after /dev-dispatch or any manual implementation. Use when user says 'qa', 'review', or 'code review'. Supports --skip-docs to skip documentation phase."
disable-model-invocation: true
argument-hint: "[--skip-docs] [optional context about what was implemented]"
---

You are executing the **qa** workflow for **graphrefly-py** (Python).

Context from user: $ARGUMENTS

### Flag detection

If `$ARGUMENTS` contains `--skip-docs`, skip Phase 4 (Documentation Updates).

---

## Phase 1: Adversarial Code Review

### 1a. Gather the diff

Run `git diff` to get all uncommitted changes. If there are untracked files relevant to the task, read and include them.

### 1b. Launch parallel review subagents

Launch these as parallel Agent calls. Each receives the diff and the context from $ARGUMENTS (what was implemented and why).

**Subagent 1: Blind Hunter** — Pure code review, no project context:

> You are a Blind Hunter code reviewer. Review this Python diff for: logic errors, off-by-one errors, race conditions, resource leaks, missing error handling, security issues, dead code, unreachable branches, thread safety (including free-threaded Python without GIL). Output each finding as: **title** | **severity** (critical/major/minor) | **location** (file:line) | **detail**. Be adversarial — assume bugs exist.

**Subagent 2: Edge Case Hunter** — Has project read access:

> You are an Edge Case Hunter. Review this diff in the context of **GraphReFly**: unified **Messages** `[[Type, Data?], ...]`, single **node** primitive, **Graph** container, **meta** companion stores, two-phase push (**DIRTY** then **DATA**/**RESOLVED**), diamond resolution, **COMPLETE**/**ERROR** terminals, batch semantics (defer **DATA**, not **DIRTY**), upstream vs downstream directions. Check for: invalid message sequences, diamond double-compute, missing **RESOLVED** when value unchanged, incorrect batch boundaries, teardown/resource leaks, reconnect state leaks, bitmask mistakes at merge nodes, concurrent **get()**/propagation issues, deadlock risks in any lock ordering. For each finding: **title** | **trigger_condition** | **potential_consequence** | **location** | **suggested_guard**.

### 1c. Triage findings

Classify each finding into:

- **patch** — fixable in this change
- **defer** — pre-existing, not introduced here
- **reject** — false positive

For **patch** and **defer**, prioritize:

1. **Spec alignment** — behavior matches **`~/src/graphrefly/GRAPHREFLY-SPEC.md`**
2. **Semantic correctness** — message and lifecycle semantics
3. **Thread safety** — GIL and free-threaded correctness where applicable
4. **Completeness** — edge cases
5. **Consistency** — with the rest of this repo and clear mapping from **`~/src/callbag-recharge-py`** when porting
6. **Effort**

### 1d. Present findings (HALT)

Present ALL patch and defer findings. For each: issue, location, recommended fix, whether it is architecture-level, whether it needs user decision.

Group:

1. **Needs Decision** — architecture-affecting or ambiguous
2. **Auto-applicable** — clear fixes matching existing patterns

**Cross-language decision log:** For **Needs Decision** items that are architectural or affect Python/TypeScript parity, note them in **`docs/optimizations.md`** (e.g. **Open design decisions** or **Cross-language implementation notes**). If **`graphrefly-ts`** is available alongside this repo, add the same bullet to **`graphrefly-ts/docs/optimizations.md`**. If not available, call out mirroring for the user.

**Wait for user decisions on group 1. Group 2 can be applied when the user approves the batch.**

---

## Phase 2: Apply Review Fixes

Apply approved fixes from Phase 1.

---

## Phase 3: Final Checks

Run all of these and fix failures (do not skip):

1. `uv run pytest`
2. `uv run ruff check --fix src/ tests/`
3. `uv run ruff format src/ tests/`
4. `uv run mypy src/`

If a failure implies a design question, **HALT** and ask the user before papering over it.

---

## Phase 4: Documentation Updates

**Skip if `--skip-docs` was passed.**

**Authoritative checklist:** follow **`docs/docs-guidance.md`** end-to-end (authority order, Tier 0–5, docstring shape aligned with TS JSDoc, runnable examples in `examples/`, `sync-docs`, and **API generation** — `cd website && pnpm docs:gen` / `docs:gen:check` when the generator is wired).

Update documentation as appropriate:

- **`docs/docs-guidance.md`** — if documentation *conventions* or generator commands change, update this file so `/qa` stays aligned
- **`~/src/graphrefly/GRAPHREFLY-SPEC.md`** — only if the user-owned spec change is part of this task (usually avoid; spec is shared with graphrefly-ts)
- **`docs/optimizations.md`** — when this review records **open architectural decisions** or cross-language parity notes; mirror substantive entries to **`graphrefly-ts/docs/optimizations.md`** if that repo is in the workspace
- **`docs/roadmap.md`** — mark items done, add items if scope changed
- **`docs/test-guidance.md`** — if new test patterns or file conventions were established
- **`archive/docs/`** — session or design notes only when explicitly part of the task
- **Structured docstrings** (Google-style: summary, Args, Returns, Examples) on exported public APIs — Tier 1 per **`docs/docs-guidance.md`**; keep semantics aligned with graphrefly-ts JSDoc where APIs match
- **Generated API pages** — after docstring or registry changes, run **`pnpm docs:gen`** from `website/` (see **`docs/docs-guidance.md`**); do not hand-edit generated `website/src/content/docs/api/*.md` unless the guidance says otherwise
- **`CLAUDE.md`** — only if commands or repo workflow changed
- **`~/src/callbag-recharge-py`** — do not edit; reference only
