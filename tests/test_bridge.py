"""Tests for core bridge helper."""

from graphrefly.core.bridge import bridge
from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import state
from graphrefly.graph.graph import Graph


class TestBridge:
    def test_forwards_data(self):
        a = state(0)
        b = state(0)
        br = bridge(a, b)
        br.subscribe(lambda _: None)

        a.down([(MessageType.DATA, 42)])
        assert b.get() == 42

    def test_forwards_dirty_data_resolved(self):
        a = state(0)
        b = state(0)
        br = bridge(a, b)
        br.subscribe(lambda _: None)

        received = []
        b.subscribe(lambda msgs: received.extend(msgs))

        a.down([(MessageType.DIRTY,)])
        a.down([(MessageType.DATA, 1)])
        a.down([(MessageType.RESOLVED,)])

        types = [m[0] for m in received]
        assert MessageType.DIRTY in types
        assert MessageType.DATA in types
        assert MessageType.RESOLVED in types

    def test_forwards_complete(self):
        a = state(0)
        b = state(0)
        br = bridge(a, b)
        br.subscribe(lambda _: None)

        completed = [False]
        b.subscribe(
            lambda msgs: [
                completed.__setitem__(0, True) for m in msgs if m[0] is MessageType.COMPLETE
            ]
        )

        a.down([(MessageType.COMPLETE,)])
        assert completed[0] is True

    def test_forwards_error_with_payload(self):
        a = state(0)
        b = state(0)
        br = bridge(a, b)
        br.subscribe(lambda _: None)

        errors = []
        b.subscribe(lambda msgs: errors.extend(m[1] for m in msgs if m[0] is MessageType.ERROR))

        a.down([(MessageType.ERROR, RuntimeError("boom"))])
        assert len(errors) == 1
        assert str(errors[0]) == "boom"

    def test_custom_down_filter(self):
        a = state(0)
        b = state(0)
        br = bridge(a, b, down=[MessageType.DATA])
        br.subscribe(lambda _: None)

        got_dirty = [False]
        b.subscribe(
            lambda msgs: [got_dirty.__setitem__(0, True) for m in msgs if m[0] is MessageType.DIRTY]
        )

        a.down([(MessageType.DIRTY,)])
        a.down([(MessageType.DATA, 5)])

        assert b.get() == 5
        assert got_dirty[0] is False

    def test_visible_in_describe(self):
        g = Graph("test")
        a = state(0)
        b = state(0)
        g.add("a", a)
        g.add("b", b)

        br = bridge(a, b, name="__bridge_a_b")
        g.add("__bridge_a_b", br)
        g.connect("a", "__bridge_a_b")

        desc = g.describe(detail="standard")
        assert "__bridge_a_b" in desc["nodes"]
        assert desc["nodes"]["__bridge_a_b"]["type"] == "effect"

    def test_forwards_unknown_custom_domain_types(self):
        """Unknown message types always forward — spec §1.3.6."""
        CUSTOM_TYPE = "custom/domain-signal"  # not a MessageType enum member
        a = state(0)
        b = state(0)
        br = bridge(a, b)
        br.subscribe(lambda _: None)

        received = []
        b.subscribe(lambda msgs: received.extend(m[0] for m in msgs))

        a.down([(CUSTOM_TYPE, "payload")])
        assert CUSTOM_TYPE in received

    def test_bridge_completes_itself_on_complete(self):
        """Bridge transitions to terminal when it forwards COMPLETE."""
        a = state(0)
        b = state(0)
        br = bridge(a, b)
        br.subscribe(lambda _: None)

        assert br.status != "completed"
        a.down([(MessageType.COMPLETE,)])

        assert b.status == "completed"
        assert br.status == "completed"

    def test_bridge_errors_itself_on_error(self):
        """Bridge transitions to errored when it forwards ERROR."""
        a = state(0)
        b = state(0)
        br = bridge(a, b)
        br.subscribe(lambda _: None)

        a.down([(MessageType.ERROR, RuntimeError("boom"))])
        assert br.status == "errored"

    def test_does_not_forward_known_excluded_types(self):
        """Known-but-excluded types are consumed without forwarding."""
        a = state(0)
        b = state(0)
        br = bridge(a, b, down=[MessageType.DATA])  # DIRTY excluded
        br.subscribe(lambda _: None)

        received = []
        b.subscribe(lambda msgs: received.extend(m[0] for m in msgs))

        a.down([(MessageType.DIRTY,)])
        assert MessageType.DIRTY not in received

    def test_cleans_up_on_destroy(self):
        g = Graph("cleanup")
        a = state(0)
        b = state(0)
        g.add("a", a)
        g.add("b", b)

        br = bridge(a, b, name="__bridge")
        g.add("__bridge", br)
        g.connect("a", "__bridge")
        br.subscribe(lambda _: None)

        a.down([(MessageType.DATA, 10)])
        assert b.get() == 10

        g.destroy()
        # After destroy, bridge is torn down
