---
title: "Python Docs"
description: "Package-local documentation for graphrefly."
tableOfContents: false
slug: /
---

Python APIs, examples, recipes, integrations, and generated API reference for
building graph-driven reactive systems with `graphrefly`.

## Start here

- **[Quick Start](/quickstart/)** — build a tiny graph through the Python facade.
- **[API Reference](/api/)** — generated from current exports and source docstrings.
- **[Examples](/examples/)** — package-local Python usage patterns.
- **[Recipes](/recipes/)** — runtime-boundary and composition notes.
- **[Integrations](/integrations/)** — host-owned async and network adapter surfaces.
- **[Release](/release/)** — wheel, version, and publishing policy.

## Install

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

## Ownership

This site is generated in `graphrefly-py` and deployed to
`https://py.graphrefly.dev/`. The shared `graphrefly.dev` site links here for
Python-specific API details and keeps cross-language concepts in the shared docs.
