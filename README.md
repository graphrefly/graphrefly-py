# graphrefly-py

Python implementation of **GraphReFly** — reactive graph protocol for human + LLM co-operation.

Based on the shared behavior spec: [`GRAPHREFLY-SPEC.md`](https://github.com/graphrefly/graphrefly).

## Install

```bash
pip install graphrefly-py
```

## Requirements

Python 3.12 or later.

## Quick Start

```python
from graphrefly import state, derived, Graph
from graphrefly.extra.sources import for_each, first_value_from

# 1. Create a state node (mutable source)
count = state(0)

# 2. Create a derived node (computed from dependencies)
doubled = derived([count], lambda deps, _actions: deps[0] * 2)

# 3. Subscribe and receive values
unsub = for_each(doubled, lambda v: print("doubled:", v))

# 4. Push a value and observe the reaction
count.push(1)   # prints: doubled: 2
count.push(5)   # prints: doubled: 10

unsub()  # detach subscriber

# 5. Use a Graph to group related nodes under a namespace
g = Graph("counter")
g.describe("count", count)
g.describe("doubled", doubled)

snapshot = g.snapshot()
print(snapshot)  # {"count": 5, "doubled": 10}

# Escape-hatch: block until the first value (useful in scripts / tests)
next_val = first_value_from(count)
print(next_val)  # 5 (current value emitted on subscribe)
```

## Layout

- `src/graphrefly/core/` — message protocol, node primitive, batch, sugar constructors
- `src/graphrefly/graph/` — Graph container, describe/observe, snapshot, persistence
- `src/graphrefly/extra/` — operators, sources, data structures, resilience, checkpoint
- `src/graphrefly/patterns/` — domain-layer APIs: orchestration, messaging, memory, AI, CQRS, reactive layout
- `src/graphrefly/compat/` — async runners (asyncio, trio)
- `src/graphrefly/integrations/` — framework integrations (FastAPI)

## Dev setup

```bash
# requires mise and uv
mise trust && mise install
uv sync
```

## Status

Early development (Phase 7 — polish and launch). See `docs/roadmap.md` for the phased plan.

## License

MIT
