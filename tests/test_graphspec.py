"""Tests for graphspec (§8.3) — validate, compile, decompile, diff, LLM compose/refine."""

from __future__ import annotations

import json
from typing import Any

import pytest

from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import derived, effect, state
from graphrefly.graph.graph import Graph
from graphrefly.patterns.ai import LLMResponse
from graphrefly.patterns.graphspec import (
    compile_spec,
    decompile_graph,
    llm_compose,
    llm_refine,
    spec_diff,
    validate_spec,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mock_adapter(responses: list[LLMResponse]) -> Any:
    """Create a mock LLM adapter that returns pre-canned responses."""
    idx = 0

    class _Adapter:
        def invoke(self, messages: Any, opts: Any = None) -> LLMResponse:
            nonlocal idx
            resp = responses[idx]
            idx += 1
            return resp

    return _Adapter()


def _test_catalog() -> dict[str, Any]:
    """Catalog with simple fns for testing."""
    return {
        "fns": {
            "double": lambda deps, config: derived(deps, lambda vals, _a: vals[0] * 2),
            "sum": lambda deps, config: derived(deps, lambda vals, _a: sum(vals)),
            "logEffect": lambda deps, config: effect(deps, lambda _d, _a: None),
            "identity": lambda deps, config: derived(deps, lambda vals, _a: vals[0]),
            "timeout": lambda deps, config: derived(deps, lambda vals, _a: vals[0]),
            "retry": lambda deps, config: derived(deps, lambda vals, _a: vals[0]),
            "fallback": lambda deps, config: derived(deps, lambda vals, _a: vals[0]),
        },
        "sources": {
            "rest-api": lambda config: state(None, meta={"source": "rest-api", **config}),
        },
    }


# ---------------------------------------------------------------------------
# validate_spec
# ---------------------------------------------------------------------------


class TestValidateSpec:
    def test_minimal_valid_spec(self) -> None:
        spec = {
            "name": "test",
            "nodes": {
                "a": {"type": "state", "initial": 1},
            },
        }
        result = validate_spec(spec)
        assert result.valid is True
        assert result.errors == ()

    def test_rejects_null(self) -> None:
        assert validate_spec(None).valid is False

    def test_rejects_missing_name(self) -> None:
        result = validate_spec({"nodes": {}})
        assert result.valid is False
        assert any("name" in e for e in result.errors)

    def test_rejects_invalid_type(self) -> None:
        result = validate_spec(
            {
                "name": "t",
                "nodes": {"x": {"type": "bogus"}},
            }
        )
        assert result.valid is False
        assert any("invalid type" in e for e in result.errors)

    def test_rejects_bad_dep(self) -> None:
        result = validate_spec(
            {
                "name": "t",
                "nodes": {
                    "a": {"type": "derived", "deps": ["missing"]},
                },
            }
        )
        assert result.valid is False
        assert any("missing" in e for e in result.errors)

    def test_validates_template_ref(self) -> None:
        spec = {
            "name": "t",
            "templates": {
                "resilient": {
                    "params": ["$source"],
                    "nodes": {
                        "inner": {"type": "derived", "deps": ["$source"], "fn": "identity"},
                    },
                    "output": "inner",
                },
            },
            "nodes": {
                "src": {"type": "state", "initial": 0},
                "wrapped": {
                    "type": "template",
                    "template": "resilient",
                    "bind": {"$source": "src"},
                },
            },
        }
        assert validate_spec(spec).valid is True

    def test_rejects_template_ref_to_nonexistent(self) -> None:
        result = validate_spec(
            {
                "name": "t",
                "nodes": {
                    "wrapped": {"type": "template", "template": "missing", "bind": {}},
                },
            }
        )
        assert result.valid is False
        assert any("not found" in e for e in result.errors)

    def test_validates_feedback_edges(self) -> None:
        spec = {
            "name": "t",
            "nodes": {
                "interval": {"type": "state", "initial": 10000},
                "compute": {"type": "derived", "deps": ["interval"], "fn": "double"},
            },
            "feedback": [{"from": "compute", "to": "interval", "maxIterations": 5}],
        }
        assert validate_spec(spec).valid is True

    def test_rejects_feedback_to_nonexistent(self) -> None:
        result = validate_spec(
            {
                "name": "t",
                "nodes": {"a": {"type": "state"}},
                "feedback": [{"from": "a", "to": "missing"}],
            }
        )
        assert result.valid is False
        assert any("missing" in e for e in result.errors)

    def test_rejects_template_bad_output(self) -> None:
        result = validate_spec(
            {
                "name": "t",
                "templates": {
                    "bad": {
                        "params": [],
                        "nodes": {"x": {"type": "state"}},
                        "output": "nonexistent",
                    },
                },
                "nodes": {},
            }
        )
        assert result.valid is False
        assert any("output" in e for e in result.errors)

    def test_validate_self_referencing_deps(self) -> None:
        result = validate_spec(
            {
                "name": "t",
                "nodes": {"a": {"type": "derived", "deps": ["a"]}},
            }
        )
        assert result.valid is False
        assert any("self-referencing" in e for e in result.errors)

    def test_validate_derived_without_deps(self) -> None:
        result = validate_spec(
            {
                "name": "t",
                "nodes": {"a": {"type": "derived", "fn": "identity"}},
            }
        )
        assert result.valid is False
        assert any("should have" in e for e in result.errors)

    def test_validate_feedback_to_non_state(self) -> None:
        result = validate_spec(
            {
                "name": "t",
                "nodes": {
                    "a": {"type": "state", "initial": 0},
                    "b": {"type": "derived", "deps": ["a"], "fn": "double"},
                },
                "feedback": [{"from": "a", "to": "b"}],
            }
        )
        assert result.valid is False
        assert any("must be a state node" in e for e in result.errors)

    def test_validate_incomplete_template_bind(self) -> None:
        result = validate_spec(
            {
                "name": "t",
                "templates": {
                    "tmpl": {
                        "params": ["$a", "$b"],
                        "nodes": {"x": {"type": "state"}},
                        "output": "x",
                    },
                },
                "nodes": {
                    "src": {"type": "state"},
                    "inst": {"type": "template", "template": "tmpl", "bind": {"$a": "src"}},
                },
            }
        )
        assert result.valid is False
        assert any("$b" in e and "not bound" in e for e in result.errors)


# ---------------------------------------------------------------------------
# compile_spec
# ---------------------------------------------------------------------------


class TestCompileSpec:
    def test_simple_state_derived(self) -> None:
        spec = {
            "name": "calc",
            "nodes": {
                "a": {"type": "state", "initial": 5},
                "b": {"type": "derived", "deps": ["a"], "fn": "double"},
            },
        }
        g = compile_spec(spec, catalog=_test_catalog())
        assert isinstance(g, Graph)
        assert g.name == "calc"
        assert g.get("a") == 5

        # Subscribe to activate derived
        seen: list[int] = []
        g.observe("b").subscribe(
            lambda msgs: [seen.append(msg[1]) for msg in msgs if msg[0] is MessageType.DATA]
        )
        g.set("a", 10)
        assert 20 in seen
        g.destroy()

    def test_producer_from_source_catalog(self) -> None:
        spec = {
            "name": "api",
            "nodes": {
                "src": {
                    "type": "producer",
                    "source": "rest-api",
                    "config": {"url": "https://example.com"},
                },
            },
        }
        g = compile_spec(spec, catalog=_test_catalog())
        assert g.node("src") is not None
        g.destroy()

    def test_multi_dep_derived(self) -> None:
        spec = {
            "name": "multi",
            "nodes": {
                "x": {"type": "state", "initial": 3},
                "y": {"type": "state", "initial": 7},
                "total": {"type": "derived", "deps": ["x", "y"], "fn": "sum"},
            },
        }
        g = compile_spec(spec, catalog=_test_catalog())
        seen: list[int] = []
        g.observe("total").subscribe(
            lambda msgs: [seen.append(msg[1]) for msg in msgs if msg[0] is MessageType.DATA]
        )
        g.set("x", 10)
        assert 17 in seen
        g.destroy()

    def test_effect_nodes(self) -> None:
        spec = {
            "name": "fx",
            "nodes": {
                "src": {"type": "state", "initial": 0},
                "log": {"type": "effect", "deps": ["src"], "fn": "logEffect"},
            },
        }
        g = compile_spec(spec, catalog=_test_catalog())
        assert g.node("log") is not None
        g.destroy()

    def test_throws_on_invalid_spec(self) -> None:
        with pytest.raises(ValueError, match="invalid GraphSpec"):
            compile_spec({"name": "", "nodes": {}})

    def test_throws_on_unresolvable_deps(self) -> None:
        spec = {
            "name": "bad",
            "nodes": {
                "a": {"type": "derived", "deps": ["b"], "fn": "identity"},
                "b": {"type": "derived", "deps": ["a"], "fn": "identity"},
            },
        }
        with pytest.raises(ValueError, match="unresolvable"):
            compile_spec(spec, catalog=_test_catalog())

    def test_template_instantiation(self) -> None:
        spec = {
            "name": "tmpl-test",
            "templates": {
                "resilientSource": {
                    "params": ["$source"],
                    "nodes": {
                        "timed": {
                            "type": "derived",
                            "deps": ["$source"],
                            "fn": "timeout",
                            "config": {"timeoutMs": 2000},
                        },
                        "retried": {
                            "type": "derived",
                            "deps": ["timed"],
                            "fn": "retry",
                            "config": {"maxAttempts": 2},
                        },
                    },
                    "output": "retried",
                },
            },
            "nodes": {
                "api1Source": {"type": "state", "initial": None},
                "api1": {
                    "type": "template",
                    "template": "resilientSource",
                    "bind": {"$source": "api1Source"},
                },
            },
        }
        g = compile_spec(spec, catalog=_test_catalog())
        assert isinstance(g, Graph)
        desc = g.describe(detail="standard")
        assert "api1" in desc["subgraphs"]
        g.destroy()

    def test_feedback_edges(self) -> None:
        spec = {
            "name": "fb-test",
            "nodes": {
                "interval": {"type": "state", "initial": 10000},
                "compute": {"type": "derived", "deps": ["interval"], "fn": "double"},
            },
            "feedback": [{"from": "compute", "to": "interval", "maxIterations": 3}],
        }
        g = compile_spec(spec, catalog=_test_catalog())
        desc = g.describe(detail="standard")
        node_names = list(desc["nodes"].keys())
        assert any(n.startswith("__feedback_") for n in node_names)
        g.destroy()

    def test_placeholder_for_unknown_source(self) -> None:
        spec = {
            "name": "placeholder",
            "nodes": {
                "src": {"type": "producer", "source": "unknown-source"},
            },
        }
        g = compile_spec(spec)
        assert g.node("src") is not None
        g.destroy()


# ---------------------------------------------------------------------------
# decompile_graph
# ---------------------------------------------------------------------------


class TestDecompileGraph:
    def test_simple_graph(self) -> None:
        g = Graph("simple")
        a = state(42, name="a", meta={"description": "input"})
        g.add("a", a)

        spec = decompile_graph(g)
        assert spec["name"] == "simple"
        assert "a" in spec["nodes"]
        assert spec["nodes"]["a"]["type"] == "state"
        assert spec["nodes"]["a"].get("initial") == 42
        g.destroy()

    def test_derived_deps(self) -> None:
        g = Graph("deps")
        a = state(1, name="a")
        b = derived([a], lambda vals, _a: vals[0] + 1, name="b")
        g.add("a", a)
        g.add("b", b)
        b.subscribe(lambda _msgs: None)

        spec = decompile_graph(g)
        assert spec["nodes"]["a"]["type"] == "state"
        assert spec["nodes"]["b"]["type"] == "derived"
        assert spec["nodes"]["b"].get("deps") == ["a"]
        g.destroy()

    def test_skip_meta_nodes(self) -> None:
        g = Graph("meta-skip")
        a = state(1, name="a", meta={"label": "test"})
        g.add("a", a)

        spec = decompile_graph(g)
        paths = list(spec["nodes"].keys())
        assert all("__meta__" not in p for p in paths)
        g.destroy()

    def test_decompile_feedback_edges(self) -> None:
        spec = {
            "name": "fb-decompile",
            "nodes": {
                "interval": {"type": "state", "initial": 10000},
                "compute": {"type": "derived", "deps": ["interval"], "fn": "double"},
            },
            "feedback": [{"from": "compute", "to": "interval", "maxIterations": 5}],
        }

        g = compile_spec(spec, catalog=_test_catalog())
        # Activate the derived node
        g.observe("compute").subscribe(lambda _msgs: None)

        decompiled = decompile_graph(g)
        assert decompiled.get("feedback") is not None
        assert len(decompiled["feedback"]) == 1
        assert decompiled["feedback"][0]["from"] == "compute"
        assert decompiled["feedback"][0]["to"] == "interval"
        assert decompiled["feedback"][0]["maxIterations"] == 5
        g.destroy()


# ---------------------------------------------------------------------------
# spec_diff
# ---------------------------------------------------------------------------


class TestSpecDiff:
    def test_identical_specs(self) -> None:
        spec = {
            "name": "same",
            "nodes": {"a": {"type": "state", "initial": 1}},
        }
        result = spec_diff(spec, spec)
        assert result.entries == ()
        assert result.summary == "no changes"

    def test_added_nodes(self) -> None:
        a = {"name": "g", "nodes": {"x": {"type": "state"}}}
        b = {
            "name": "g",
            "nodes": {"x": {"type": "state"}, "y": {"type": "derived", "deps": ["x"]}},
        }
        result = spec_diff(a, b)
        assert any(e.type == "added" and e.path == "nodes.y" for e in result.entries)

    def test_removed_nodes(self) -> None:
        a = {
            "name": "g",
            "nodes": {"x": {"type": "state"}, "y": {"type": "derived", "deps": ["x"]}},
        }
        b = {"name": "g", "nodes": {"x": {"type": "state"}}}
        result = spec_diff(a, b)
        assert any(e.type == "removed" and e.path == "nodes.y" for e in result.entries)

    def test_changed_node_config(self) -> None:
        a = {"name": "g", "nodes": {"x": {"type": "derived", "fn": "a", "config": {"k": 1}}}}
        b = {"name": "g", "nodes": {"x": {"type": "derived", "fn": "b", "config": {"k": 2}}}}
        result = spec_diff(a, b)
        assert any(e.type == "changed" and e.path == "nodes.x" for e in result.entries)
        assert result.entries[0].detail is not None and "fn" in result.entries[0].detail

    def test_name_change(self) -> None:
        a = {"name": "old", "nodes": {}}
        b = {"name": "new", "nodes": {}}
        result = spec_diff(a, b)
        assert result.entries[0].path == "name"

    def test_template_changes(self) -> None:
        a = {
            "name": "g",
            "nodes": {},
            "templates": {
                "tmpl": {
                    "params": ["$x"],
                    "nodes": {"inner": {"type": "state"}},
                    "output": "inner",
                },
            },
        }
        b = {"name": "g", "nodes": {}}
        result = spec_diff(a, b)
        assert any(e.type == "removed" and e.path == "templates.tmpl" for e in result.entries)

    def test_feedback_edge_changes(self) -> None:
        a = {
            "name": "g",
            "nodes": {"x": {"type": "state"}, "y": {"type": "derived", "deps": ["x"]}},
            "feedback": [{"from": "y", "to": "x", "maxIterations": 5}],
        }
        b = {
            "name": "g",
            "nodes": {"x": {"type": "state"}, "y": {"type": "derived", "deps": ["x"]}},
            "feedback": [{"from": "y", "to": "x", "maxIterations": 10}],
        }
        result = spec_diff(a, b)
        assert any(e.type == "changed" and "feedback" in e.path for e in result.entries)

    def test_summary(self) -> None:
        a = {"name": "g", "nodes": {"x": {"type": "state"}}}
        b = {"name": "g", "nodes": {"x": {"type": "state"}, "y": {"type": "state"}}}
        result = spec_diff(a, b)
        assert "1 added" in result.summary


# ---------------------------------------------------------------------------
# llm_compose
# ---------------------------------------------------------------------------


class TestLLMCompose:
    def test_basic(self) -> None:
        spec = {
            "name": "email_triage",
            "nodes": {
                "inbox": {
                    "type": "producer",
                    "source": "email",
                    "meta": {"description": "Email inbox source"},
                },
                "classify": {
                    "type": "derived",
                    "deps": ["inbox"],
                    "fn": "llmClassify",
                    "meta": {"description": "Classify emails"},
                },
            },
        }
        adapter = _mock_adapter([LLMResponse(content=json.dumps(spec))])
        result = llm_compose("Build an email triage system", adapter)
        assert result["name"] == "email_triage"
        assert result["nodes"]["inbox"]["type"] == "producer"
        assert result["nodes"]["classify"]["type"] == "derived"

    def test_strips_fences(self) -> None:
        spec = {
            "name": "test",
            "nodes": {"a": {"type": "state", "initial": 1, "meta": {"description": "a"}}},
        }
        adapter = _mock_adapter([LLMResponse(content=f"```json\n{json.dumps(spec)}\n```")])
        result = llm_compose("test", adapter)
        assert result["name"] == "test"

    def test_throws_on_invalid_json(self) -> None:
        adapter = _mock_adapter([LLMResponse(content="not json!")])
        with pytest.raises(ValueError, match="not valid JSON"):
            llm_compose("bad", adapter)

    def test_throws_on_invalid_spec(self) -> None:
        adapter = _mock_adapter([LLMResponse(content=json.dumps({"nodes": {}}))])
        with pytest.raises(ValueError, match="invalid GraphSpec"):
            llm_compose("bad", adapter)


# ---------------------------------------------------------------------------
# llm_refine
# ---------------------------------------------------------------------------


class TestLLMRefine:
    def test_basic(self) -> None:
        original = {
            "name": "v1",
            "nodes": {"a": {"type": "state", "initial": 1, "meta": {"description": "src"}}},
        }
        refined = {
            "name": "v2",
            "nodes": {
                "a": {"type": "state", "initial": 1, "meta": {"description": "src"}},
                "b": {
                    "type": "derived",
                    "deps": ["a"],
                    "fn": "double",
                    "meta": {"description": "doubled"},
                },
            },
        }
        adapter = _mock_adapter([LLMResponse(content=json.dumps(refined))])
        result = llm_refine(original, "Add a doubling derived node", adapter)
        assert result["name"] == "v2"
        assert "b" in result["nodes"]
