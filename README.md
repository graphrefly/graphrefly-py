# graphrefly-py

Reactive graph protocol for human and LLM co-operation.

Python implementation of GraphReFly, based on the shared behavior defined in
`~/src/graphrefly/GRAPHREFLY-SPEC.md`.

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

## Dev setup

```bash
# requires mise and uv
mise trust && mise install
uv sync
```

## Status

Early development. See `docs/roadmap.md` for the phased implementation plan.

## License

MIT
