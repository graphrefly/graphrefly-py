# Documentation guidance

How to write and maintain documentation in **graphrefly-py**. Read this before adding or restructuring docs.

---

## Authority order

1. **`GRAPHREFLY-SPEC.md`** — Cross-language behavioral spec (shared with graphrefly-ts). Defines messages, node, Graph, and protocol invariants. **Do not contradict** unless you are deliberately revising the spec in coordination with the TS repo.
2. **`roadmap.md`** — Phased delivery plan, checkboxes, and what “done” means for this Python package.
3. **`archive/docs/SESSION-graphrefly-spec-design.md`** — Historical design session: rationale, simplifications vs callbag-recharge, scenario validation. Use for **why**, not for overriding the spec.
4. **Predecessor repo `~/src/callbag-recharge-py`** — Reference for porting patterns, tests, concurrency ideas, and operator behavior when the spec is brief. It describes the **callbag-recharge** model; GraphReFly uses **Messages** and a single **node** primitive — translate concepts, do not copy naming blindly.

---

## What belongs where

| Doc | Purpose |
|-----|---------|
| `GRAPHREFLY-SPEC.md` | Protocol and API behavior **as shipped across languages** |
| `roadmap.md` | Python implementation phases and milestones |
| `docs-guidance.md` (this file) | Conventions for contributors and agents |
| `test-guidance.md` | How to write tests |
| `archive/docs/` | Frozen design notes and session exports — append or add dated files; avoid rewriting history |
| `README.md` | User-facing intro, install, links (project root) |

Optional future additions (when content grows):

- `architecture.md` — Python-only encoding notes (import layout, sync vs async boundaries, threading) **without** duplicating the spec’s protocol section

---

## Style

- Prefer **precise, testable statements** over marketing language.
- Link to **`GRAPHREFLY-SPEC.md`** sections instead of copying large excerpts.
- Use **Markdown** headings consistently (`##` / `###`); one H1 per file at the top.
- For message types and code identifiers, use the same names as the spec (`DATA`, `DIRTY`, `RESOLVED`, …).
- When documenting a port from callbag-recharge-py, name the **GraphReFly** equivalent (e.g. old STATE/DATA/END → unified message tuples + `COMPLETE`/`ERROR`).

---

## Agent and PR workflow

- **`/dev-dispatch`** (`.claude/skills/dev-dispatch/`) — implementation planning and self-test; ends with **`/qa`** suggestion.
- **`/qa`** (`.claude/skills/qa/`) — review, checks, doc touch-ups.
- Update **`roadmap.md`** checkboxes when a phase item is completed.
- Update **`test-guidance.md`** when new test categories or helpers become standard.

---

## Cross-repo references

- **graphrefly-ts** — parallel implementation; keep behavioral docs aligned via **`GRAPHREFLY-SPEC.md`**.
- **callbag-recharge-py** — helpful for **implementation** and **test** ideas; **spec** is GraphReFly.
