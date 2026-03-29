# examples/

Runnable examples using the public `graphrefly` API. This directory is the **single source of truth for all library demo code** (Tier 2 in `docs/docs-guidance.md`).

## Rules

- Each file is self-contained and imports from `graphrefly` (public package name).
- Recipe pages and Pyodide labs **import from here** — never duplicate code inline.
- Keep examples focused: one concept per file, named descriptively (`basic_counter.py`, `combine_sources.py`).

## Running

```bash
python examples/<name>.py
```
