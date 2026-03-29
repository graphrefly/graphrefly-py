"""Basic reactive counter — the simplest GraphReFly example.

Demonstrates: state(), derived(), node subscription via .sinks.
"""

from graphrefly import DATA, derived, state

# A manual source node with initial value 0.
count = state(0)

# A derived node that doubles the count.
doubled = derived([count], lambda deps, _actions: deps[0] * 2)

# Subscribe to changes.
def on_msg(msgs):
    for msg_type, *rest in msgs:
        if msg_type == DATA:
            print("doubled:", rest[0])

doubled.sinks.add(on_msg)

# Push values.
count.push(1)  # doubled: 2
count.push(2)  # doubled: 4
count.push(3)  # doubled: 6
