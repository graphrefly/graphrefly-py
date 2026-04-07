# Documentation guidance (graphrefly-py)

Single-source-of-truth strategy: **protocol spec lives in `~/src/graphrefly`**; **docstrings on exported APIs** are the source of truth for Python API docs; **`examples/`** holds all runnable code.

---

## Authority order

1. **`~/src/graphrefly/GRAPHREFLY-SPEC.md`** — protocol, node contract, Graph, invariants (cross-language, canonical)
2. **Docstrings** on public exports — parameters, returns, examples, remarks (source of truth for Python API docs)
3. **`examples/*.py`** — all runnable library code (single source for recipes, demos, guides)
4. **`docs/roadmap.md`** — what is implemented vs planned
5. **`README.md`** — install, quick start, links

---

## Design invariant documentation

- When documenting Phase 4+ APIs, never expose protocol internals (`DIRTY`, `RESOLVED`, bitmask) in primary API docs — use domain language (e.g. "the value updates reactively" not "emits DIRTY then DATA").
- Docstring examples should demonstrate reactive patterns, not polling or imperative triggers.
- Reference the design invariants in **GRAPHREFLY-SPEC §5.8–5.12** when reviewing doc changes for Phase 4+ features.

---

## Documentation tiers

| Tier | What | Where it lives | Flows to |
|------|------|----------------|----------|
| **0 — Protocol spec** | `~/src/graphrefly/GRAPHREFLY-SPEC.md` | `~/src/graphrefly/` repo (sibling checkout) | Both sites via `sync-docs.mjs` |
| **1 — Docstrings** | Structured doc blocks on exports | `src/graphrefly/*.py` | Generated API pages via `website/scripts/gen_api_docs.py` (`extra/tier1.py`, `extra/tier2.py`, `extra/sources.py`, `extra/backoff.py`, `extra/checkpoint.py`, `extra/resilience.py`, `extra/data_structures.py`, …) → `website/src/content/docs/api/` |
| **2 — Runnable examples** | Self-contained scripts using public imports | `examples/*.py` | Imported by recipes + demos |
| **3 — Recipes / guides** | Long-form Starlight pages with context | `website/src/content/docs/recipes/` | Pull code from `examples/` |
| **4 — Interactive demos** | Pyodide labs | `website/src/content/docs/lab/` | Interactive Python playground |
| **5 — `llms.txt`** | AI-readable docs | repo root + `website/public/` | Updated when adding user-facing primitives |

### Unified code location rule

**All library logic lives in `examples/`.** Recipes, demos, and guides never duplicate inline code:

- **Recipe pages** import code via file snippets or code blocks referencing `examples/`
- **Pyodide labs** can load examples for interactive execution
- **Docstring examples** (Tier 1) stand alone in IDE tooltips — they don't import from `examples/`

---

## How shared docs are synced

The spec flows from the canonical `~/src/graphrefly` repo into the Starlight site via `website/scripts/sync-docs.mjs`:

```bash
cd website && pnpm sync-docs              # copy spec + local docs
cd website && pnpm sync-docs --check      # CI dry-run — exit 1 if stale
```

| Source | Origin |
|--------|--------|
| `~/src/graphrefly/GRAPHREFLY-SPEC.md` | `~/src/graphrefly/` repo only (no in-repo copy) |
| `roadmap.md`, `optimizations.md`, etc. | `docs/` (this repo) |

---

## Docstrings on exported functions (Tier 1)

Every public function/class should have a structured docstring. Use Google-style or NumPy-style consistently. Match the same semantic tags as the TS repo's JSDoc for cross-language alignment:

- **Description** — first line, starts with a verb ("Creates", "Transforms")
- **Args** / **Parameters** — each parameter with type and description
- **Returns** — return type and semantics
- **Examples** — at least one usage example with `graphrefly` imports
- **Notes** — optional: invariants, interaction with batch, errors

---

## How API docs are generated

API reference pages under `website/src/content/docs/api/` are **generated** from top-level function exports listed in each module’s `__all__`, in `website/scripts/gen_api_docs.py` `EXTRA_MODULES` (currently `extra/tier1.py`, `tier2.py`, `sources.py`, `backoff.py`, `checkpoint.py`, `resilience.py`, …).

```bash
cd website && pnpm docs:gen              # regenerate all
cd website && pnpm docs:gen:check        # CI dry-run — exit 1 if stale
```

**Do not edit generated `website/src/content/docs/api/*.md` by hand** — edit docstrings in the source module, add the module path to `EXTRA_MODULES` if needed, then run `docs:gen`.

To document a new public **function**, **class**, or PEP 695 **type** alias, add it to the appropriate module and its `__all__` (use a plain `__all__ = [...]` or annotated `__all__: list[str] = [...]`); re-run `docs:gen`. The Starlight sidebar picks up the `api/` directory via `autogenerate` in `website/astro.config.mjs`.

`docs:gen` runs on `pnpm dev` and `pnpm build` in `website/` (with `sync-docs`).

---

## Archive indexes (JSONL)

`archive/optimizations/`, `archive/roadmap/`, and `archive/docs/` use JSONL as the machine-readable index format. One JSON object per line, searchable with `grep`, parseable with `jq` or `python3 -m json.tool --json-lines`.

### Roadmap archive

`docs/roadmap.md` contains **only active/open items**. All completed phases and items are archived to JSONL files in `archive/roadmap/`.

| File | Content |
|------|---------|
| `phase-0-foundation.jsonl` | Phase 0: scaffold, message protocol, node primitive, concurrency, meta, sugar, tests |
| `phase-1-graph-container.jsonl` | Phase 1: Graph core, composition, introspection, lifecycle, persistence, actor/guard, tests |
| `phase-2-extra.jsonl` | Phase 2: Tier 1/2 operators, sources & sinks |
| `phase-3-resilience-data.jsonl` | Phase 3: resilience, reactive output consistency, data structures, composite data |
| `phase-4-domain-layers.jsonl` | Phase 4: orchestration, messaging, memory, AI surface, CQRS |
| `phase-5-framework-distribution.jsonl` | Phase 5: framework compat, ORM adapters, adapters, ingest, storage, LLM tools |
| `phase-6-versioning.jsonl` | Phase 6: V0 id+version, V0 backfill, V1 cid+prev |
| `phase-7-polish.jsonl` | Phase 7: README, reactive layout |
| `phase-8-reduction-layer.jsonl` | Phase 8: reduction primitives, domain templates, LLM graph composition |
| `phase-9-harness-sprint.jsonl` | Phase 9: architecture debt (8.2 rearchitecture) |

**JSONL schema:**

```json
{"id": "kebab-case-slug", "phase": "0.1", "title": "Short title", "items": ["completed item 1", "completed item 2"]}
```

**Workflow:**

1. **New open items** go in `docs/roadmap.md` under the appropriate section.
2. When a phase or item group is **completed**, move it from `docs/roadmap.md` to the appropriate `archive/roadmap/*.jsonl` file (append a new line).
3. Items that remain open from completed phases stay in `docs/roadmap.md` under "Open items from completed phases."

### Design decision archive

`archive/docs/design-archive-index.jsonl` indexes all design session files (`SESSION-*.md`). See `archive/docs/DESIGN-ARCHIVE-INDEX.md` for schema and query examples.

When a new design session is completed, append an entry to `design-archive-index.jsonl` with `id`, `date`, `title`, `file`, `topic`, `decisions`, and optional fields (`roadmap_impact`, `canonical`). Python companion sessions should include `"canonical"` pointing to the TS repo's canonical session file.

### Optimization decision log

`docs/optimizations.md` contains **only active work items, anti-patterns, and deferred follow-ups**. All resolved decisions and reference material are archived to JSONL files in `archive/optimizations/`.

### Archive structure

| File | Content |
|------|---------|
| `resolved-decisions.jsonl` | All resolved design decisions (gateway, streaming, AI, compat, core, patterns, layout, etc.) |
| `cross-language-notes.jsonl` | Cross-language implementation notes (§1–§22c): batch, settlement, concurrency, Graph phases, operators, etc. |
| `parity-fixes.jsonl` | Cross-language parity fixes (one-time alignment fixes) |
| `qa-design-decisions.jsonl` | QA review design decisions (A–L), resolved operator/source semantics, ingest adapter divergences |
| `built-in-optimizations.jsonl` | Built-in optimization descriptions and summary table |
| `summary-table.jsonl` | Cross-language summary comparison table |

### JSONL schema

Each `.jsonl` file has one JSON object per line. Common fields:

```json
{"id": "kebab-case-slug", "title": "Short title", "body": "Full markdown text"}
```

Additional fields vary by file: `phase`, `noted`, `resolved`, `section`, `status`.

### Workflow for new decisions

1. **New open decisions** go in `docs/optimizations.md` under "Active work items".
2. When a decision is **resolved**, move it from `docs/optimizations.md` to the appropriate `archive/optimizations/*.jsonl` file (append a new line).
3. If the sibling repo (`graphrefly-py` / `graphrefly-ts`) is available, mirror the entry to its `archive/optimizations/*.jsonl` too.
4. The anti-patterns table and deferred follow-ups stay in `docs/optimizations.md` as living reference.

### Reading archived decisions

```bash
# Search for a topic across all archives
grep -i "batch" archive/optimizations/*.jsonl

# Pretty-print a specific file
cat archive/optimizations/resolved-decisions.jsonl | python3 -m json.tool --json-lines
```

---

## Spec vs code

- If **implementation** intentionally differs from the spec, **fix the implementation** unless the spec is wrong — then update **`~/src/graphrefly/GRAPHREFLY-SPEC.md`** with a version note (see spec §8).
- Coordinate spec changes across both `graphrefly-ts` and `graphrefly-py`.

---

## When to update which file

| Change | Update |
|--------|--------|
| New public API | Docstring + export from `extra/__init__.py` + `pnpm docs:gen` when under `extra/tier1.py` or `extra/tier2.py` |
| Protocol or Graph behavior | `~/src/graphrefly/GRAPHREFLY-SPEC.md` (canonical) + docstring |
| New runnable example | `examples/<name>.py` + optional recipe page |
| Phase completed | Archive done items to `archive/roadmap/*.jsonl`, update `docs/roadmap.md` |
| AI / LLM discovery | `llms.txt` when introduced |

---

## Order of execution for new features

1. **Implementation** in `src/graphrefly/` + tests (`docs/test-guidance.md`)
2. **Structured docstring** on the exported function/class (Tier 1)
3. **`cd website && pnpm docs:gen`** for symbols in `extra/tier1.py` / `extra/tier2.py` (regenerates API pages)
4. **Runnable example** in `examples/` (Tier 2) — if the feature warrants a standalone demo
5. **Recipe** on the site that imports from `examples/` (Tier 3) — for complex patterns
6. **Pyodide lab** if warranted (Tier 4)
7. **Update llms.txt** if the feature is user-facing (Tier 5)
8. **Roadmap** — mark items done

---

## Cross-repo references

- **`~/src/graphrefly`** — canonical spec repo; both TS and Py sites pull from here
- **graphrefly-ts** — parallel implementation; keep behavioral docs aligned via the shared spec
- **callbag-recharge-py** — helpful for **implementation** and **test** ideas; spec is GraphReFly

---

## Style

- Prefer **precise, testable statements** over marketing language.
- Link to **`~/src/graphrefly/GRAPHREFLY-SPEC.md`** sections instead of copying large excerpts.
- Use **Markdown** headings consistently (`##` / `###`); one H1 per file at the top.
- For message types and code identifiers, use the same names as the spec (`DATA`, `DIRTY`, `RESOLVED`, ...).

---

## Agent and PR workflow

- **`/dev-dispatch`** — implementation planning and self-test; ends with **`/qa`** suggestion.
- **`/qa`** — review, checks, doc touch-ups.
- Update **`roadmap.md`** checkboxes when a phase item is completed.
- Update **`test-guidance.md`** when new test categories or helpers become standard.

---

## File locations summary

| What | Where | Editable? |
|------|-------|-----------|
| Canonical spec | `~/src/graphrefly/GRAPHREFLY-SPEC.md` | Yes — coordinate across repos |
| Source of truth (docstrings) | `src/graphrefly/*.py` | Yes — primary edit target |
| API doc generator | `website/scripts/gen_api_docs.py` | Yes — tier1 operators |
| Generated API pages | `website/src/content/docs/api/*.md` | **No** — run `docs:gen` |
| Sync script | `website/scripts/sync-docs.mjs` | Yes |
| Synced doc pages | `website/src/content/docs/*.md` | **No** — regenerated from `docs/` |
| Runnable examples | `examples/*.py` | Yes — all library demo code lives here |
| Recipes | `website/src/content/docs/recipes/*.md` | Yes — import code from `examples/` |
| Pyodide labs | `website/src/content/docs/lab/*.mdx` | Yes |
| Roadmap | `docs/roadmap.md` | Yes |
| This file | `docs/docs-guidance.md` | Yes |
| Astro config (sidebar) | `website/astro.config.mjs` | Yes — update when adding pages |
