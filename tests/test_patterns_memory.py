"""Patterns memory tests (roadmap 4.3 initial slice)."""

from __future__ import annotations

from graphrefly.patterns.memory import (
    collection,
    decay,
    knowledge_graph,
    light_collection,
    vector_index,
)


def test_light_collection_fifo_eviction() -> None:
    c = light_collection(max_size=2, policy="fifo")
    c.upsert("a", 1)
    c.upsert("b", 2)
    c.upsert("c", 3)
    assert c.has("a") is False
    assert c.has("b") is True
    assert c.has("c") is True


def test_light_collection_lru_eviction() -> None:
    c = light_collection(max_size=2, policy="lru")
    c.upsert("a", 1)
    c.upsert("b", 2)
    assert c.get("a") == 1
    c.upsert("c", 3)
    assert c.has("a") is True
    assert c.has("b") is False
    assert c.has("c") is True


def test_collection_graph_exposes_ranked_nodes() -> None:
    g = collection("mem", max_size=2, score=lambda value: float(value))
    g.upsert("x", 1)
    g.upsert("y", 5)
    g.upsert("z", 3)
    assert g.get("size") == 2
    ranked = g.get("ranked")
    assert isinstance(ranked, list)
    assert len(ranked) == 2
    assert ranked[0].score >= ranked[1].score


def test_vector_index_flat_search() -> None:
    idx = vector_index(backend="flat", dimension=2)
    idx.upsert("a", [1, 0], {"label": "x-axis"})
    idx.upsert("b", [0, 1], {"label": "y-axis"})
    out = idx.search([0.9, 0.1], 1)
    assert out[0].id == "a"
    assert out[0].meta == {"label": "x-axis"}


def test_vector_index_hnsw_requires_optional_adapter() -> None:
    try:
        vector_index(backend="hnsw")
    except ValueError as err:
        assert "optional dependency adapter" in str(err).lower()
    else:
        raise AssertionError("expected ValueError for missing hnsw adapter")


def test_vector_index_flat_cosine_zero_pads_to_max_length_without_dimension() -> None:
    import math

    idx = vector_index(backend="flat")
    idx.upsert("long", [1.0, 0.0, 0.0, 1.0], {"label": "four"})
    out = idx.search([1.0, 0.0], 1)
    assert abs(out[0].score - 1.0 / math.sqrt(2.0)) < 1e-5


def test_knowledge_graph_entities_and_edges() -> None:
    kg = knowledge_graph("kg")
    kg.upsert_entity("reactive_map", {"name": "reactive_map"})
    kg.upsert_entity("reactive_index", {"name": "reactive_index"})
    kg.link("reactive_map", "reactive_index", "composes")
    related = kg.related("reactive_map")
    assert len(related) == 1
    assert related[0].relation == "composes"


def test_decay_with_floor() -> None:
    score = decay(10.0, 10.0, 0.5, 2.0)
    assert score >= 2.0
    assert score < 10.0
