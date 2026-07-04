# GraphReFly Python

`graphrefly` is the Python host package for GraphReFly. It provides a Python
facade over the native Rust graph engine and keeps the clean-slate wave protocol
inside the native runtime.

```bash
pip install graphrefly
```

```python
from graphrefly import Graph

with Graph("hello") as graph:
    count = graph.state(1, name="count")
    doubled = graph.derived([count], lambda value: value * 2, name="doubled")

    with doubled.subscribe(lambda msg: print(msg.kind, msg.value)):
        count.set(2)
```

Use `Graph.derived(...)` for value-level transforms, `Graph.node(...)` for
advanced callback-scoped `Ctx` work, and `Graph.close()` or `with Graph(...)` for
host lifetime cleanup.

## Documentation Boundary

The package-local docs policy is `docs/docs.jsonl`. This repo owns Python
docstrings, generated Python API docs, Zensical pages, examples, PyPI install
material, and release notes.

Shared graphrefly.dev website architecture, shared guide records, protocol
authority, and public blog material stay in `~/src/graphrefly` under D563.
Generated API output is built from public docstrings through Zensical/mkdocstrings;
rebuild it with `uv run zensical build --strict` and do not edit generated `site/`
files by hand.

## What This Package Owns

- Pythonic graph, node, subscription, message, and exception facades.
- Host lifetime and exception mapping over the native Rust engine.
- Optional async runner adapters that use caller-owned runtimes.
- High-level wire bridge facades for cross-graph transport.

## What It Does Not Expose

- Native handles.
- Raw protocol ingress or arbitrary message construction.
- A Python reimplementation of the native wave core.
