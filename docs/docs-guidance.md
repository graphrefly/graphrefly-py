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

## Documentation tiers

| Tier | What | Where it lives | Flows to |
|------|------|----------------|----------|
| **0 — Protocol spec** | `GRAPHREFLY-SPEC.md` | `~/src/graphrefly/` (canonical) | Both sites via `sync-docs.mjs` |
| **1 — Docstrings** | Structured doc blocks on exports | `src/graphrefly/*.py` | Future: generated API pages via a Python doc generator |
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
| `GRAPHREFLY-SPEC.md` | `~/src/graphrefly/` (shared spec repo) with fallback to `docs/` |
| `roadmap.md`, `optimizations.md`, etc. | `docs/` (this repo) |

---

## Docstrings on exported functions (Tier 1)

Every public function/class should have a structured docstring. Use Google-style or NumPy-style consistently. Match the same semantic tags as the TS repo's JSDoc for cross-language alignment:

- **Description** — first line, starts with a verb ("Creates", "Transforms")
- **Args** / **Parameters** — each parameter with type and description
- **Returns** — return type and semantics
- **Examples** — at least one usage example with `graphrefly` imports
- **Notes** — optional: invariants, interaction with batch, errors

When a Python doc generator is added (analogous to TS `gen-api-docs.mjs`), these docstrings will feed generated API pages.

---

## Spec vs code

- If **implementation** intentionally differs from the spec, **fix the implementation** unless the spec is wrong — then update **`~/src/graphrefly/GRAPHREFLY-SPEC.md`** with a version note (see spec §8).
- Coordinate spec changes across both `graphrefly-ts` and `graphrefly-py`.

---

## When to update which file

| Change | Update |
|--------|--------|
| New public API | Docstring + export from `__init__.py` |
| Protocol or Graph behavior | `~/src/graphrefly/GRAPHREFLY-SPEC.md` (canonical) + docstring |
| New runnable example | `examples/<name>.py` + optional recipe page |
| Phase completed | `docs/roadmap.md` checkboxes |
| AI / LLM discovery | `llms.txt` when introduced |

---

## Order of execution for new features

1. **Implementation** in `src/graphrefly/` + tests (`docs/test-guidance.md`)
2. **Structured docstring** on the exported function/class (Tier 1)
3. **Runnable example** in `examples/` (Tier 2) — if the feature warrants a standalone demo
4. **Recipe** on the site that imports from `examples/` (Tier 3) — for complex patterns
5. **Pyodide lab** if warranted (Tier 4)
6. **Update llms.txt** if the feature is user-facing (Tier 5)
7. **Roadmap** — mark items done

---

## Cross-repo references

- **`~/src/graphrefly`** — canonical spec repo; both TS and Py sites pull from here
- **graphrefly-ts** — parallel implementation; keep behavioral docs aligned via the shared spec
- **callbag-recharge-py** — helpful for **implementation** and **test** ideas; spec is GraphReFly

---

## Style

- Prefer **precise, testable statements** over marketing language.
- Link to **`GRAPHREFLY-SPEC.md`** sections instead of copying large excerpts.
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
| Local spec copy | `docs/GRAPHREFLY-SPEC.md` | Synced — prefer editing canonical |
| Source of truth (docstrings) | `src/graphrefly/*.py` | Yes — primary edit target |
| Sync script | `website/scripts/sync-docs.mjs` | Yes |
| Synced doc pages | `website/src/content/docs/*.md` | **No** — regenerated from `docs/` |
| Runnable examples | `examples/*.py` | Yes — all library demo code lives here |
| Recipes | `website/src/content/docs/recipes/*.md` | Yes — import code from `examples/` |
| Pyodide labs | `website/src/content/docs/lab/*.mdx` | Yes |
| Roadmap | `docs/roadmap.md` | Yes |
| This file | `docs/docs-guidance.md` | Yes |
| Astro config (sidebar) | `website/astro.config.mjs` | Yes — update when adding pages |
