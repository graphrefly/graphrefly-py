"""Tests for dynamic_node — runtime dep tracking with diamond resolution."""

from graphrefly import MessageType, batch, dynamic_node, node, state


def _collect(n):
    batches = []
    unsub = n.subscribe(lambda msgs: batches.append(msgs))
    return batches, unsub


def _data_values(batches):
    return [m[1] for b in batches for m in b if m[0] is MessageType.DATA]


def test_tracks_deps_via_get_proxy():
    a = state(1)
    b = state(2)
    d = dynamic_node(lambda get: get(a) + get(b))
    batches, unsub = _collect(d)
    assert 3 in _data_values(batches)
    unsub()


def test_conditional_deps_switch():
    cond = state(True)
    a = state(10)
    b = state(20)
    d = dynamic_node(lambda get: get(a) if get(cond) else get(b))

    batches, unsub = _collect(d)
    assert 10 in _data_values(batches)

    # Switch condition
    with batch():
        cond.down([(MessageType.DIRTY,)])
        cond.down([(MessageType.DATA, False)])

    assert 20 in _data_values(batches)
    unsub()


def test_dep_set_changes_adds_new_deps():
    a = state(1)
    b = state(2)
    c = state(3)
    use_c = state(False)

    def compute(get):
        total = get(a) + get(b)
        if get(use_c):
            total += get(c)
        return total

    d = dynamic_node(compute)
    batches, unsub = _collect(d)
    assert 3 in _data_values(batches)  # 1 + 2

    with batch():
        use_c.down([(MessageType.DIRTY,)])
        use_c.down([(MessageType.DATA, True)])

    assert 6 in _data_values(batches)  # 1 + 2 + 3
    unsub()


def test_emits_resolved_when_unchanged():
    a = state(1)
    d = dynamic_node(lambda get: "positive" if get(a) > 0 else "non-positive")

    batches, unsub = _collect(d)
    assert "positive" in _data_values(batches)

    # Change a to 2 — result still "positive"
    with batch():
        a.down([(MessageType.DIRTY,)])
        a.down([(MessageType.DATA, 2)])

    last_batch = batches[-1]
    assert any(m[0] is MessageType.RESOLVED for m in last_batch)
    unsub()


def test_diamond_resolution_single_recompute():
    root = state(1)
    left = node([root], lambda deps, _: deps[0] * 2)
    right = node([root], lambda deps, _: deps[0] + 10)

    compute_count = 0

    def compute(get):
        nonlocal compute_count
        compute_count += 1
        return [get(left), get(right)]

    d = dynamic_node(compute)
    batches, unsub = _collect(d)
    compute_count = 0  # reset after initial

    with batch():
        root.down([(MessageType.DIRTY,)])
        root.down([(MessageType.DATA, 5)])

    assert compute_count == 1
    assert _data_values(batches)[-1] == [10, 15]
    unsub()


def test_cleanup_on_disconnect():
    a = state(1)
    d = dynamic_node(lambda get: get(a))
    _, unsub = _collect(d)
    assert d.status != "disconnected"
    unsub()
    assert d.status == "disconnected"


def test_get_returns_cached_value():
    a = state(42)
    d = dynamic_node(lambda get: get(a))
    _collect(d)
    assert d.get() == 42
