# GraphReFly Python

`graphrefly` is GraphReFly for Python: a reactive graph on its own pure-Python
kernel, which implements the GraphReFly kernel rules, with a typed facade over it.

```bash
pip install graphrefly
```

```python
from graphrefly import Graph

with Graph("hello") as graph:
    count = graph.state(1, name="count")
    doubled = graph.derived([count], lambda value: value * 2, name="doubled")

    with doubled.subscribe(lambda array: print([m[0] for m in array])):
        count.set(2)
```

Use `Graph.derived(...)` for value-level transforms, `Graph.node(...)` for
advanced callback-scoped `Ctx` work, `State.set(...)` and the other `State`
methods for host input, and `Graph.close()` or `with Graph(...)` for host lifetime
cleanup.

## Documentation Boundary

The package-local docs policy is `docs/docs.jsonl`. This repo owns Python
docstrings, generated Python API docs, Starlight pages, examples, PyPI install
material, and release notes.

Shared graphrefly.dev website architecture, shared guide records, protocol
authority, and public blog material stay in `~/src/graphrefly` under D563.
Generated API output is built from public docstrings through the `griffe`-backed
Starlight generator; rebuild it with `cd website && pnpm build` and do not edit
generated `website/src/content/docs/api/` files by hand.

## What This Package Owns

- The Python kernel (`graphrefly._kernel`) and checkpoint (`graphrefly._checkpoint`).
- Pythonic graph, node, state, subscription, message, and exception facades.
- Host lifetime and exception mapping, and Python sugar such as derived's gate.
- Optional async runner adapters that use caller-owned runtimes.
- Wire edge groups and the bridge's protobuf transport (`graphrefly._wire`).

## What It Does Not Expose

- Raw kernel handles or arbitrary message sending outside `Ctx.down`.
- Raw bridge envelope values: transports see canonical protobuf bytes and
  `WireEdgeCause` frames (graphrefly-py:D8).

Documentation categories and maintenance: [documentation map](README.md).
