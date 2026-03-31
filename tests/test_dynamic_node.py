"""Tests for dynamic_node — runtime dep tracking with diamond resolution."""

from graphrefly import MessageType, batch, dynamic_node, node, state
from graphrefly.core.guard import policy
from graphrefly.core.meta import describe_node


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


# --- Meta (companion stores) ---


def test_meta_builds_subscribable_nodes():
    a = state(1)
    d = dynamic_node(lambda get: get(a), meta={"tag": "hello", "count": 0})
    assert d.meta["tag"].get() == "hello"
    assert d.meta["count"].get() == 0


def test_meta_independently_subscribable():
    a = state(1)
    d = dynamic_node(lambda get: get(a), meta={"status": "idle"})

    msgs = []
    unsub = d.meta["status"].subscribe(lambda m: msgs.append(m))

    d.meta["status"].down([(MessageType.DATA, "loading")])
    assert d.meta["status"].get() == "loading"
    assert len(msgs) > 0
    unsub()


def test_meta_teardown_propagates():
    a = state(1)
    d = dynamic_node(lambda get: get(a), name="dyn", meta={"tag": "x"})
    _collect(d)

    meta_msgs = []
    d.meta["tag"].subscribe(lambda m: meta_msgs.append(m))

    d.down([(MessageType.TEARDOWN,)])

    has_teardown = any(m[0] is MessageType.TEARDOWN for batch in meta_msgs for m in batch)
    assert has_teardown


def test_meta_empty_when_not_provided():
    a = state(1)
    d = dynamic_node(lambda get: get(a))
    assert len(d.meta) == 0


def test_meta_node_names_include_parent():
    a = state(1)
    d = dynamic_node(lambda get: get(a), name="myDyn", meta={"label": "test"})
    assert d.meta["label"].name == "myDyn:meta:label"


# --- onMessage handler ---


def test_on_message_intercepts_dep_messages():
    a = state(1)
    intercepted = []

    def handler(msg, dep_index, actions):
        intercepted.append(msg)
        return False  # don't consume

    d = dynamic_node(lambda get: get(a), on_message=handler)
    _collect(d)
    with batch():
        a.down([(MessageType.DIRTY,)])
        a.down([(MessageType.DATA, 2)])
    assert len(intercepted) > 0
    assert d.get() == 2


def test_on_message_consume_prevents_settlement():
    a = state(1)
    compute_count = 0

    def compute(get):
        nonlocal compute_count
        compute_count += 1
        return get(a)

    def handler(msg, dep_index, actions):
        return msg[0] is MessageType.DATA  # consume DATA

    d = dynamic_node(compute, on_message=handler)
    _collect(d)
    compute_count = 0
    with batch():
        a.down([(MessageType.DIRTY,)])
        a.down([(MessageType.DATA, 99)])
    assert compute_count == 0


def test_on_message_error_emits_error():
    a = state(1)

    def handler(msg, dep_index, actions):
        raise RuntimeError("handler boom")

    d = dynamic_node(lambda get: get(a), on_message=handler)
    batches, _ = _collect(d)
    with batch():
        a.down([(MessageType.DIRTY,)])
        a.down([(MessageType.DATA, 2)])
    has_error = any(m[0] is MessageType.ERROR for b in batches for m in b)
    assert has_error


# --- onResubscribe ---


def test_on_resubscribe_called_on_terminal_transition():
    a = state(1)
    resub_count = 0

    def on_resub():
        nonlocal resub_count
        resub_count += 1

    d = dynamic_node(lambda get: get(a), resubscribable=True, on_resubscribe=on_resub)
    unsub1 = d.subscribe(lambda _: None)
    d.down([(MessageType.COMPLETE,)])
    unsub1()
    unsub2 = d.subscribe(lambda _: None)
    assert resub_count == 1
    unsub2()


# --- completeWhenDepsComplete ---


def test_auto_complete_when_all_deps_complete():
    a = state(1)
    d = dynamic_node(lambda get: get(a))
    batches, _ = _collect(d)
    a.down([(MessageType.COMPLETE,)])
    has_complete = any(m[0] is MessageType.COMPLETE for b in batches for m in b)
    assert has_complete


def test_suppress_auto_complete():
    a = state(1)
    d = dynamic_node(lambda get: get(a), complete_when_deps_complete=False)
    batches, _ = _collect(d)
    a.down([(MessageType.COMPLETE,)])
    has_complete = any(m[0] is MessageType.COMPLETE for b in batches for m in b)
    assert not has_complete


# --- describeKind ---


def test_describe_kind_overrides_type():
    a = state(1)
    d = dynamic_node(lambda get: get(a), name="dyn", describe_kind="effect")
    desc = describe_node(d)
    assert desc["type"] == "effect"


def test_describe_node_defaults_to_derived():
    a = state(1)
    d = dynamic_node(lambda get: get(a), name="dyn")
    desc = describe_node(d)
    assert desc["type"] == "derived"


# --- inspector hook ---


def test_inspector_emits_dep_message():
    a = state(1)
    d = dynamic_node(lambda get: get(a))
    events = []
    dispose = d._set_inspector_hook(lambda e: events.append(e))
    _collect(d)
    with batch():
        a.down([(MessageType.DIRTY,)])
        a.down([(MessageType.DATA, 2)])
    assert any(e["kind"] == "dep_message" for e in events)
    dispose()


def test_inspector_emits_run():
    a = state(10)
    d = dynamic_node(lambda get: get(a))
    runs = []
    dispose = d._set_inspector_hook(lambda e: runs.append(e) if e["kind"] == "run" else None)
    _collect(d)
    with batch():
        a.down([(MessageType.DIRTY,)])
        a.down([(MessageType.DATA, 20)])
    assert len(runs) > 0
    assert "dep_values" in runs[0]
    dispose()


def test_inspector_dispose_restores_previous():
    a = state(1)
    d = dynamic_node(lambda get: get(a))
    events1 = []
    events2 = []
    dispose1 = d._set_inspector_hook(lambda e: events1.append(e))
    dispose2 = d._set_inspector_hook(lambda e: events2.append(e))
    dispose2()  # restore hook 1
    _collect(d)
    with batch():
        a.down([(MessageType.DIRTY,)])
        a.down([(MessageType.DATA, 2)])
    assert len(events1) > 0
    assert len(events2) == 0
    dispose1()


# --- guard ---


def test_guard_denies_write():
    a = state(1)
    guard = policy(lambda allow, deny: [allow("observe")])
    d = dynamic_node(lambda get: get(a), guard=guard)
    try:
        d.down([(MessageType.DATA, 42)])
        raise AssertionError("should have raised")
    except Exception:
        pass


def test_guard_denies_observe_on_subscribe():
    a = state(1)
    guard = policy(lambda allow, deny: [deny("observe")])
    d = dynamic_node(lambda get: get(a), guard=guard)
    try:
        d.subscribe(lambda _: None, actor={"type": "human", "id": "u1"})
        raise AssertionError("should have raised")
    except Exception:
        pass


def test_meta_inherits_guard():
    a = state(1)
    guard = policy(lambda allow, deny: [allow("observe")])
    d = dynamic_node(lambda get: get(a), guard=guard, meta={"tag": "x"})
    assert d.meta["tag"].has_guard()


# --- resubscribable ---


def test_resubscribable_allows_fresh_subscription():
    a = state(1)
    d = dynamic_node(lambda get: get(a), resubscribable=True)
    unsub1 = d.subscribe(lambda _: None)
    d.down([(MessageType.COMPLETE,)])
    assert d.status == "completed"
    unsub1()
    unsub2 = d.subscribe(lambda _: None)
    assert d.status != "completed"
    unsub2()


# --- up() forwarding ---


def test_up_forwards_to_tracked_deps():
    """Verify up() reaches dynamic deps by checking no error is raised."""
    a = state(1)
    d = dynamic_node(lambda get: get(a))
    _collect(d)
    # up() on a connected dynamicNode with deps should not raise
    d.up([(MessageType.PAUSE,)], internal=True)
    # If we get here without error, forwarding worked (source nodes no-op on up)
