"""Patterns messaging tests (roadmap 4.2 initial slice)."""

from __future__ import annotations

import pytest

from graphrefly.patterns import messaging


def test_topic_retains_events_and_updates_latest() -> None:
    t = messaging.topic("events")
    t.publish(1)
    t.publish(2)
    assert t.get("latest") == 2
    assert t.retained() == (1, 2)


def test_subscription_cursor_ack_flow() -> None:
    t = messaging.topic("events")
    t.publish(10)
    t.publish(20)
    sub = messaging.subscription("sub", t)
    assert sub.pull() == (10, 20)
    sub.ack(1)
    assert sub.pull() == (20,)
    assert sub.pull(ack=True) == (20,)
    assert sub.pull() == ()
    assert ("topic::events", "source") in sub.edges()


def test_job_queue_enqueue_claim_ack_nack() -> None:
    q = messaging.job_queue("emails")
    id1 = q.enqueue(1)
    id2 = q.enqueue(2)
    assert q.get("depth") == 2
    claimed = q.claim(2)
    assert tuple(job.id for job in claimed) == (id1, id2)
    assert all(job.state == "inflight" for job in claimed)
    assert q.ack(id1) is True
    assert q.nack(id2, requeue=True) is True
    second = q.claim(1)
    assert len(second) == 1
    assert second[0].id == id2


def test_job_queue_nack_without_requeue_drops_job() -> None:
    q = messaging.job_queue("emails")
    job_id = q.enqueue(1)
    claimed = q.claim(1)
    assert len(claimed) == 1
    assert claimed[0].id == job_id
    assert q.nack(job_id, requeue=False) is True
    assert q.claim(1) == ()


def test_job_queue_rejects_duplicate_job_ids() -> None:
    q = messaging.job_queue("emails")
    q.enqueue(1, job_id="fixed")
    with pytest.raises(ValueError, match="duplicate job id"):
        q.enqueue(2, job_id="fixed")


def test_job_queue_claim_zero_is_noop() -> None:
    q = messaging.job_queue("emails")
    q.enqueue(1)
    assert q.claim(0) == ()
    assert q.get("depth") == 1


def test_rejects_invalid_non_negative_integer_parameters() -> None:
    t = messaging.topic("events")
    with pytest.raises(ValueError, match="non-negative integer"):
        messaging.subscription("sub_bad_cursor", t, cursor=-1)
    sub = messaging.subscription("sub_ok", t)
    with pytest.raises(ValueError, match="non-negative integer"):
        sub.ack(-1)
    with pytest.raises(ValueError, match="non-negative integer"):
        sub.pull(-1)
    q = messaging.job_queue("emails")
    with pytest.raises(ValueError, match="non-negative integer"):
        q.claim(-1)
    with pytest.raises(ValueError, match="non-negative integer"):
        messaging.job_flow("flow_bad", max_per_pump=-1)
    with pytest.raises(ValueError, match="non-negative integer"):
        messaging.topic_bridge("bridge_bad", t, messaging.topic("dst_bad"), max_per_pump=-1)


def test_job_queue_metadata_is_immutable_after_enqueue() -> None:
    q = messaging.job_queue("emails")
    meta = {"lane": "high"}
    job_id = q.enqueue(1, metadata=meta)
    meta["lane"] = "low"
    claimed = q.claim(1)
    assert len(claimed) == 1
    assert claimed[0].id == job_id
    assert claimed[0].metadata["lane"] == "high"


def test_topic_bridge_relays_source_events_to_target() -> None:
    source = messaging.topic("src")
    target = messaging.topic("dst")
    bridge = messaging.topic_bridge("bridge", source, target)
    source.publish(3)
    source.publish(4)
    assert target.retained() == (3, 4)
    assert bridge.get("bridgedCount") == 2


def test_topic_bridge_supports_map_and_drop() -> None:
    source = messaging.topic("src")
    target = messaging.topic("dst")

    def map_fn(value: int) -> int | None:
        if value % 2 == 0:
            return None
        return value * 10

    messaging.topic_bridge("bridge", source, target, map_fn=map_fn)
    source.publish(1)
    source.publish(2)
    source.publish(3)
    assert target.retained() == (10, 30)


def test_job_flow_auto_advances_jobs_to_completed() -> None:
    flow = messaging.job_flow("flow", stages=("incoming", "work", "done"))
    flow.enqueue(10)
    flow.enqueue(20)
    assert tuple(job.payload for job in flow.retained_completed()) == (10, 20)
    assert flow.get("completedCount") == 2
    assert flow.queue("incoming").get("depth") == 0
    assert flow.queue("work").get("depth") == 0
    assert flow.queue("done").get("depth") == 0
