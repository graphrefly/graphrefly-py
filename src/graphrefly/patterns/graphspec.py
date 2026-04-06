"""GraphSpec: declarative graph composition (roadmap §8.3).

Declarative GraphSpec schema + compiler/decompiler for graph topology.
The LLM designs graphs as JSON; ``compile_spec`` instantiates them;
``decompile_graph`` extracts them back. Templates support reusable subgraph
patterns. Feedback edges express bounded cycles via §8.1 ``feedback()``.
"""

from __future__ import annotations

import json
import re as _re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from graphrefly.core.protocol import MessageType
from graphrefly.core.sugar import derived, effect, producer, state
from graphrefly.graph.graph import GRAPH_META_SEGMENT, Graph

if TYPE_CHECKING:
    from collections.abc import Callable

    from graphrefly.patterns.ai import LLMAdapter

# ---------------------------------------------------------------------------
# GraphSpec types (TypedDict for JSON-compatible shapes)
# ---------------------------------------------------------------------------


class GraphSpecNode(TypedDict, total=False):
    """A single node declaration in a GraphSpec."""

    type: str  # "state" | "producer" | "derived" | "effect" | "operator"
    deps: list[str]
    fn: str
    source: str
    config: dict[str, Any]
    initial: Any
    meta: dict[str, Any]


class GraphSpecTemplateRef(TypedDict):
    """Template instantiation node -- expanded at compile time."""

    type: str  # literal "template"
    template: str
    bind: dict[str, str]


class GraphSpecTemplate(TypedDict):
    """A reusable subgraph pattern with parameter substitution."""

    params: list[str]
    nodes: dict[str, GraphSpecNode]
    output: str


class GraphSpecFeedbackEdge(TypedDict, total=False):
    """A feedback edge: bounded cycle from condition to reentry."""

    from_: str  # aliased from "from" for Python keyword avoidance
    to: str
    max_iterations: int


class GraphSpec(TypedDict, total=False):
    """Declarative graph topology for LLM composition (§8.3)."""

    name: str
    nodes: dict[str, GraphSpecNode | GraphSpecTemplateRef]
    templates: dict[str, GraphSpecTemplate]
    feedback: list[dict[str, Any]]  # dicts with "from", "to", "maxIterations" keys


# ---------------------------------------------------------------------------
# Catalog types
# ---------------------------------------------------------------------------

type FnFactory = Callable[[list[Any], dict[str, Any]], Any]
type SourceFactory = Callable[[dict[str, Any]], Any]


class GraphSpecCatalog(TypedDict, total=False):
    """Fn/source lookup table passed to compile_spec."""

    fns: dict[str, FnFactory]
    sources: dict[str, SourceFactory]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphSpecValidation:
    """Validation result from :func:`validate_spec`."""

    valid: bool
    errors: tuple[str, ...]


_VALID_NODE_TYPES = frozenset({"state", "producer", "derived", "effect", "operator", "template"})
_VALID_INNER_TYPES = frozenset({"state", "producer", "derived", "effect", "operator"})


def validate_spec(spec: Any) -> GraphSpecValidation:
    """Validate a GraphSpec JSON object.

    Checks structural validity: required fields, node types, dep references,
    template references, and feedback edge targets.
    """
    errors: list[str] = []

    if spec is None or not isinstance(spec, dict):
        return GraphSpecValidation(valid=False, errors=("GraphSpec must be a non-null object",))

    s: dict[str, Any] = spec

    name = s.get("name")
    if not isinstance(name, str) or len(name) == 0:
        errors.append("Missing or empty 'name' field")

    nodes = s.get("nodes")
    if nodes is None or not isinstance(nodes, dict):
        errors.append("Missing or invalid 'nodes' field (must be an object)")
        return GraphSpecValidation(valid=False, errors=tuple(errors))

    node_names = set(nodes.keys())
    node_types: dict[str, str] = {}
    template_names: set[str] = set()
    template_defs: dict[str, list[str]] = {}  # name -> params

    templates_raw = s.get("templates")
    if templates_raw is not None and isinstance(templates_raw, dict):
        template_names = set(templates_raw.keys())
        for t_name, t_raw in templates_raw.items():
            if isinstance(t_raw, dict) and isinstance(t_raw.get("params"), list):
                template_defs[t_name] = t_raw["params"]

    # Validate templates
    if templates_raw is not None:
        if not isinstance(templates_raw, dict):
            errors.append("'templates' must be an object")
        else:
            for t_name, t_raw in templates_raw.items():
                if t_raw is None or not isinstance(t_raw, dict):
                    errors.append(f'Template "{t_name}": must be an object')
                    continue
                if not isinstance(t_raw.get("params"), list):
                    errors.append(f"Template \"{t_name}\": missing 'params' array")
                t_nodes = t_raw.get("nodes")
                if t_nodes is None or not isinstance(t_nodes, dict):
                    errors.append(f"Template \"{t_name}\": missing or invalid 'nodes' object")
                else:
                    param_set = set(
                        t_raw["params"] if isinstance(t_raw.get("params"), list) else []
                    )
                    inner_names = set(t_nodes.keys())
                    for n_name, n_raw in t_nodes.items():
                        if n_raw is None or not isinstance(n_raw, dict):
                            errors.append(f'Template "{t_name}" node "{n_name}": must be an object')
                            continue
                        n_type = n_raw.get("type")
                        if not isinstance(n_type, str) or n_type not in _VALID_INNER_TYPES:
                            errors.append(f'Template "{t_name}" node "{n_name}": invalid type')
                        deps = n_raw.get("deps")
                        if isinstance(deps, list):
                            for dep in deps:
                                if dep not in inner_names and dep not in param_set:
                                    errors.append(
                                        f'Template "{t_name}" node "{n_name}": dep "{dep}" '
                                        "is not an inner node or param"
                                    )
                    output = t_raw.get("output")
                    if not isinstance(output, str):
                        errors.append(f"Template \"{t_name}\": missing 'output' string")
                    elif output not in t_nodes:
                        errors.append(
                            f'Template "{t_name}": output "{output}" is not a declared node'
                        )

    # Validate nodes
    for nname, raw in nodes.items():
        if raw is None or not isinstance(raw, dict):
            errors.append(f'Node "{nname}": must be an object')
            continue
        ntype = raw.get("type")
        if not isinstance(ntype, str) or ntype not in _VALID_NODE_TYPES:
            valid_str = ", ".join(sorted(_VALID_NODE_TYPES))
            errors.append(f'Node "{nname}": invalid type "{ntype}" (expected: {valid_str})')
            continue

        node_types[nname] = ntype

        if ntype == "template":
            tmpl_name = raw.get("template")
            if not isinstance(tmpl_name, str) or tmpl_name not in template_names:
                errors.append(f'Node "{nname}": template "{tmpl_name}" not found in templates')
            else:
                # Check bind completeness: all template params must be bound
                bind = raw.get("bind")
                if bind is None or not isinstance(bind, dict):
                    errors.append(f"Node \"{nname}\": template ref requires 'bind' object")
                else:
                    tmpl_params = template_defs.get(tmpl_name, [])
                    for param in tmpl_params:
                        if param not in bind:
                            errors.append(
                                f'Node "{nname}": template param "{param}" is not bound '
                                f'(template "{tmpl_name}")'
                            )
                    for _param, target in bind.items():
                        if isinstance(target, str) and target not in node_names:
                            errors.append(
                                f'Node "{nname}": bind target "{target}" does not '
                                f"reference an existing node"
                            )
        else:
            deps = raw.get("deps")
            if isinstance(deps, list):
                for dep in deps:
                    if isinstance(dep, str):
                        if dep == nname:
                            errors.append(f'Node "{nname}": self-referencing dep')
                        elif dep not in node_names:
                            errors.append(
                                f'Node "{nname}": dep "{dep}" does not reference an existing node'
                            )
            # Warn: derived/effect/operator without deps
            if ntype in ("derived", "effect", "operator") and not isinstance(deps, list):
                errors.append(f"Node \"{nname}\": {ntype} node should have a 'deps' array")

    # Validate feedback edges
    fb = s.get("feedback")
    if fb is not None:
        if not isinstance(fb, list):
            errors.append("'feedback' must be an array")
        else:
            for i, edge in enumerate(fb):
                if edge is None or not isinstance(edge, dict):
                    errors.append(f"Feedback [{i}]: must be an object")
                    continue
                e_from = edge.get("from")
                if not isinstance(e_from, str) or e_from not in node_names:
                    errors.append(
                        f"Feedback [{i}]: 'from' \"{e_from}\" does not reference an existing node"
                    )
                if isinstance(e_from, str) and e_from == edge.get("to"):
                    errors.append(f"Feedback [{i}]: 'from' and 'to' must be different nodes")
                e_to = edge.get("to")
                if not isinstance(e_to, str) or e_to not in node_names:
                    errors.append(
                        f"Feedback [{i}]: 'to' \"{e_to}\" does not reference an existing node"
                    )
                elif isinstance(e_to, str) and node_types.get(e_to) != "state":
                    errors.append(
                        f"Feedback [{i}]: 'to' node \"{e_to}\" must be a state node "
                        f'(got "{node_types.get(e_to, "unknown")}")'
                    )

    return GraphSpecValidation(valid=len(errors) == 0, errors=tuple(errors))


# ---------------------------------------------------------------------------
# compile_spec
# ---------------------------------------------------------------------------


def compile_spec(
    spec: dict[str, Any],
    *,
    catalog: GraphSpecCatalog | None = None,
) -> Graph:
    """Instantiate a Graph from a GraphSpec.

    Handles template expansion (mounted subgraphs), feedback wiring via §8.1
    ``feedback()``, node factory lookup from the catalog, and topology validation.

    Args:
        spec: Declarative graph topology dict.
        catalog: Fn/source catalog for resolving named factories.

    Returns:
        A running Graph.

    Raises:
        ValueError: On validation failure, missing catalog entries, or unresolvable deps.
    """
    from graphrefly.patterns.reduction import feedback as feedback_primitive

    validation = validate_spec(spec)
    if not validation.valid:
        detail = "\n".join(validation.errors)
        msg = f"compile_spec: invalid GraphSpec:\n{detail}"
        raise ValueError(msg)

    cat: dict[str, Any] = dict(catalog) if catalog else {}
    fns: dict[str, FnFactory] = cat.get("fns") or {}
    sources: dict[str, SourceFactory] = cat.get("sources") or {}
    g = Graph(spec["name"])
    templates: dict[str, Any] = spec.get("templates") or {}

    # Phase 1: Create non-template nodes (state/producer first, then derived/effect/operator)
    created: dict[str, Any] = {}
    deferred: list[tuple[str, dict[str, Any]]] = []

    for name, raw in spec["nodes"].items():
        if raw.get("type") == "template":
            continue  # handled in Phase 2

        n = raw
        if n["type"] == "state":
            nd = state(
                n.get("initial"),
                name=name,
                meta=dict(n["meta"]) if n.get("meta") else None,
            )
            g.add(name, nd)
            created[name] = nd
        elif n["type"] == "producer":
            source_factory = sources.get(n["source"]) if n.get("source") else None
            fn_factory = fns.get(n["fn"]) if n.get("fn") else None
            if source_factory:
                nd = source_factory(n.get("config") or {})
                g.add(name, nd)
                created[name] = nd
            elif fn_factory:
                nd = fn_factory([], n.get("config") or {})
                g.add(name, nd)
                created[name] = nd
            else:
                # No catalog entry -- create a bare producer placeholder
                nd = producer(
                    lambda _deps, _actions: None,
                    name=name,
                    meta={
                        **(n.get("meta") or {}),
                        "_spec_fn": n.get("fn"),
                        "_spec_source": n.get("source"),
                    },
                )
                g.add(name, nd)
                created[name] = nd
        else:
            deferred.append((name, n))

    # Resolve deferred nodes (derived/effect/operator) in dependency order
    pending = dict(deferred)
    progressed = True
    while pending and progressed:
        progressed = False
        for name in list(pending):
            n = pending[name]
            deps = n.get("deps") or []
            if not all(dep in created for dep in deps):
                continue

            resolved_deps = [created[dep] for dep in deps]
            fn_factory = fns.get(n["fn"]) if n.get("fn") else None

            if fn_factory:
                nd = fn_factory(resolved_deps, n.get("config") or {})
            elif n["type"] == "effect":
                nd = effect(resolved_deps, lambda _deps, _actions: None)
            else:
                # derived/operator without catalog fn -- identity passthrough
                nd = derived(resolved_deps, lambda vals, _actions: vals[0] if vals else None)

            if n.get("meta"):
                for k, v in n["meta"].items():
                    meta_node = nd.meta.get(k)
                    if meta_node is not None:
                        meta_node.down([(MessageType.DATA, v)])

            g.add(name, nd)
            created[name] = nd
            del pending[name]
            progressed = True

    if pending:
        unresolved = ", ".join(sorted(pending.keys()))
        msg = f"compile_spec: unresolvable deps for nodes: {unresolved}"
        raise ValueError(msg)

    # Phase 2: Expand template instantiations as mounted subgraphs
    for name, raw in spec["nodes"].items():
        if raw.get("type") != "template":
            continue
        ref = raw
        tmpl = templates[ref["template"]]

        sub = Graph(name)
        sub_created: dict[str, Any] = {}
        sub_deferred: list[tuple[str, dict[str, Any]]] = []

        # Create inner nodes, resolving $params to bound nodes
        for n_name, n_spec in tmpl["nodes"].items():
            resolved_deps = []
            for dep in n_spec.get("deps") or []:
                if dep.startswith("$") and dep in ref.get("bind", {}):
                    resolved_deps.append(ref["bind"][dep])
                else:
                    resolved_deps.append(dep)
            spec_with_resolved = {**n_spec, "deps": resolved_deps}

            if n_spec["type"] == "state":
                nd = state(
                    n_spec.get("initial"),
                    name=n_name,
                    meta=dict(n_spec["meta"]) if n_spec.get("meta") else None,
                )
                sub.add(n_name, nd)
                sub_created[n_name] = nd
            elif n_spec["type"] == "producer":
                # Handle producer nodes inside templates
                source_factory = sources.get(n_spec["source"]) if n_spec.get("source") else None
                fn_factory = fns.get(n_spec["fn"]) if n_spec.get("fn") else None
                if source_factory:
                    nd = source_factory(n_spec.get("config") or {})
                elif fn_factory:
                    nd = fn_factory([], n_spec.get("config") or {})
                else:
                    nd = producer(
                        lambda _deps, _actions: None,
                        name=n_name,
                        meta={
                            **(n_spec.get("meta") or {}),
                            "_specFn": n_spec.get("fn"),
                            "_specSource": n_spec.get("source"),
                        },
                    )
                sub.add(n_name, nd)
                sub_created[n_name] = nd
            else:
                sub_deferred.append((n_name, spec_with_resolved))

        # Resolve deferred inner nodes
        sub_pending = dict(sub_deferred)
        sub_progressed = True
        while sub_pending and sub_progressed:
            sub_progressed = False
            for n_name in list(sub_pending):
                n_spec = sub_pending[n_name]
                deps = n_spec.get("deps") or []
                all_ready = all(dep in sub_created or dep in created for dep in deps)
                if not all_ready:
                    continue

                resolved_deps = [
                    sub_created[dep] if dep in sub_created else created[dep] for dep in deps
                ]
                fn_factory = fns.get(n_spec["fn"]) if n_spec.get("fn") else None

                if fn_factory:
                    nd = fn_factory(resolved_deps, n_spec.get("config") or {})
                elif n_spec["type"] == "effect":
                    nd = effect(resolved_deps, lambda _deps, _actions: None)
                else:
                    nd = derived(
                        resolved_deps,
                        lambda vals, _actions: vals[0] if vals else None,
                    )
                sub.add(n_name, nd)
                sub_created[n_name] = nd
                del sub_pending[n_name]
                sub_progressed = True

        if sub_pending:
            unresolved = ", ".join(sorted(sub_pending.keys()))
            msg = f'compile_spec: template "{ref["template"]}" has unresolvable deps: {unresolved}'
            raise ValueError(msg)

        g.mount(name, sub)
        # Register template output as a reachable node path
        output_path = f"{name}::{tmpl['output']}"
        created[name] = g.resolve(output_path)

        # Store template origin meta on the mounted subgraph's output node
        # so decompile_graph can recover it without structural fingerprinting.
        try:
            output_node = g.resolve(output_path)
            tmpl_name_meta = output_node.meta.get("_templateName")
            if tmpl_name_meta is not None:
                tmpl_name_meta.down([(MessageType.DATA, ref["template"])])
            tmpl_bind_meta = output_node.meta.get("_templateBind")
            if tmpl_bind_meta is not None:
                tmpl_bind_meta.down([(MessageType.DATA, ref.get("bind", {}))])
        except Exception:  # noqa: BLE001
            pass  # meta nodes may not exist; template origin is best-effort

    # Phase 3: Wire edges from deps
    for name, raw in spec["nodes"].items():
        if raw.get("type") == "template":
            continue
        for dep in raw.get("deps") or []:
            try:
                g.connect(dep, name)
            except (ValueError, KeyError) as exc:
                msg = str(exc)
                if "constructor deps" not in msg and "already" not in msg:
                    raise

    # Phase 4: Wire feedback edges via §8.1 feedback()
    for fb in spec.get("feedback") or []:
        feedback_primitive(
            g,
            fb["from"],
            fb["to"],
            max_iterations=fb.get("maxIterations", 10),
        )

    return g


# ---------------------------------------------------------------------------
# decompile_graph
# ---------------------------------------------------------------------------


def decompile_graph(graph: Graph) -> dict[str, Any]:
    """Extract a GraphSpec from a running graph.

    Uses ``describe(detail="standard")`` as a starting point, then enriches with:

    - Feedback edges recovered from counter node meta (``feedbackFrom``/``feedbackTo``)
    - Template refs recovered from output node meta (``_templateName``/``_templateBind``)
    - Structural fingerprinting as fallback for 2+ identical mounted subgraphs

    Args:
        graph: Running graph to decompile.

    Returns:
        A GraphSpec dict representation.
    """
    desc = graph.describe(detail="standard")
    nodes: dict[str, dict[str, Any]] = {}
    feedback_edges: list[dict[str, Any]] = []
    meta_segment = f"::{GRAPH_META_SEGMENT}::"

    # Internal meta keys used by compile_spec/feedback -- stripped from output
    internal_meta_keys = {
        "reduction",
        "reduction_type",
        "_specFn",
        "_specSource",
        "_templateName",
        "_templateBind",
        "feedbackFrom",
        "feedbackTo",
    }

    # Detect feedback counter nodes and extract feedback edges from meta
    feedback_counter_pattern = _re.compile(r"^__feedback_(.+)$")
    feedback_conditions: set[str] = set()

    for path in list(desc["nodes"].keys()):
        if meta_segment in path:
            continue
        match = feedback_counter_pattern.match(path)
        if match:
            feedback_conditions.add(match.group(1))
            node_desc = desc["nodes"][path]
            meta = node_desc.get("meta") if isinstance(node_desc, dict) else None
            if isinstance(meta, dict) and meta.get("feedbackFrom") and meta.get("feedbackTo"):
                fb_edge: dict[str, Any] = {
                    "from": meta["feedbackFrom"],
                    "to": meta["feedbackTo"],
                }
                if meta.get("maxIterations") is not None:
                    fb_edge["maxIterations"] = meta["maxIterations"]
                feedback_edges.append(fb_edge)

    # Build nodes map, skipping meta and feedback internals
    for path, node_desc in desc["nodes"].items():
        if meta_segment in path:
            continue
        if feedback_counter_pattern.match(path):
            continue
        # Skip subgraph-internal nodes (they belong to templates)
        if "::" in path:
            continue

        spec_node: dict[str, Any] = {
            "type": node_desc["type"],
        }

        deps = node_desc.get("deps", [])
        if deps:
            # Filter out cross-subgraph deps (keep only local)
            spec_node["deps"] = [d for d in deps if "::" not in d]

        if node_desc.get("type") == "state" and node_desc.get("value") is not None:
            spec_node["initial"] = node_desc["value"]

        raw_meta = node_desc.get("meta")
        if isinstance(raw_meta, dict) and raw_meta:
            meta = {k: v for k, v in raw_meta.items() if k not in internal_meta_keys}
            if meta:
                spec_node["meta"] = meta

        nodes[path] = spec_node

    # Detect templates: first from compile-time meta (option B), then structural fallback
    templates: dict[str, dict[str, Any]] = {}
    template_refs: dict[str, dict[str, Any]] = {}
    meta_detected_subgraphs: set[str] = set()

    # Option B: recover template origin from meta stored by compile_spec
    for sub_name in desc.get("subgraphs", []):
        prefix = f"{sub_name}::"
        for path, node_desc in desc["nodes"].items():
            if not path.startswith(prefix):
                continue
            if meta_segment in path:
                continue
            meta = node_desc.get("meta") if isinstance(node_desc, dict) else None
            if isinstance(meta, dict) and meta.get("_templateName") and meta.get("_templateBind"):
                template_name = meta["_templateName"]
                bind = meta["_templateBind"]

                # Reconstruct template definition from the subgraph's nodes
                if template_name not in templates:
                    tmpl_nodes: dict[str, dict[str, Any]] = {}
                    tmpl_inner_names: set[str] = set()
                    for p, nd in desc["nodes"].items():
                        if not p.startswith(prefix) or meta_segment in p:
                            continue
                        local_name = p[len(prefix) :]
                        if "::" in local_name:
                            continue
                        tmpl_inner_names.add(local_name)
                        nd_deps = nd.get("deps", [])
                        tmpl_nodes[local_name] = {
                            "type": nd["type"],
                            **(
                                {
                                    "deps": [
                                        d[len(prefix) :] if d.startswith(prefix) else d
                                        for d in nd_deps
                                    ]
                                }
                                if nd_deps
                                else {}
                            ),
                        }
                    # Detect params (external deps) and output
                    tmpl_params: list[str] = []
                    tmpl_param_map: dict[str, str] = {}
                    for n in tmpl_nodes.values():
                        for dep in n.get("deps", []):
                            if dep not in tmpl_inner_names and dep not in tmpl_param_map:
                                param = f"${dep}"
                                tmpl_params.append(param)
                                tmpl_param_map[dep] = param
                    # Substitute external deps with $params
                    for n in tmpl_nodes.values():
                        if n.get("deps"):
                            n["deps"] = [tmpl_param_map.get(d, d) for d in n["deps"]]
                    # Find output
                    depended: set[str] = set()
                    for n in tmpl_nodes.values():
                        for dep in n.get("deps", []):
                            if dep in tmpl_inner_names:
                                depended.add(dep)
                    output_candidates = sorted(n for n in tmpl_inner_names if n not in depended)
                    tmpl_output = (
                        output_candidates[0] if output_candidates else sorted(tmpl_inner_names)[-1]
                    )

                    templates[template_name] = {
                        "params": tmpl_params,
                        "nodes": tmpl_nodes,
                        "output": tmpl_output,
                    }

                nodes.pop(sub_name, None)
                template_refs[sub_name] = {
                    "type": "template",
                    "template": template_name,
                    "bind": bind,
                }
                meta_detected_subgraphs.add(sub_name)
                break

    # Structural fallback: group remaining mounted subgraphs by fingerprint
    structure_map: dict[str, list[dict[str, Any]]] = {}
    for sub_name in desc.get("subgraphs", []):
        if sub_name in meta_detected_subgraphs:
            continue
        sub_nodes: dict[str, dict[str, Any]] = {}
        prefix = f"{sub_name}::"
        for path, node_desc in desc["nodes"].items():
            if meta_segment in path:
                continue
            if not path.startswith(prefix):
                continue
            local_name = path[len(prefix) :]
            if "::" in local_name:
                continue  # skip deeper nesting
            sub_nodes[local_name] = {
                "type": node_desc["type"],
                **(
                    {
                        "deps": [
                            d[len(prefix) :] if d.startswith(prefix) else d
                            for d in node_desc.get("deps", [])
                        ]
                    }
                    if node_desc.get("deps")
                    else {}
                ),
            }
        fingerprint = json.dumps(
            {
                k: {"type": v["type"], "deps": v.get("deps", [])}
                for k, v in sorted(sub_nodes.items())
            },
            sort_keys=True,
        )
        if fingerprint not in structure_map:
            structure_map[fingerprint] = []
        structure_map[fingerprint].append({"name": sub_name, "nodes": sub_nodes})

    # Subgraphs with identical structure (2+ instances) -> templates
    for group in structure_map.values():
        if len(group) < 2:
            continue
        template_name = f"{group[0]['name']}_template"
        ref_nodes = group[0]["nodes"]
        inner_names = set(ref_nodes.keys())

        # Detect external deps as params (from first member)
        params: list[str] = []
        base_param_map: dict[str, str] = {}
        for n in ref_nodes.values():
            for dep in n.get("deps", []):
                if dep not in inner_names and dep not in base_param_map:
                    param = f"${dep}"
                    params.append(param)
                    base_param_map[dep] = param

        # Find output node (node with no inner dependents)
        depended_set: set[str] = set()
        for n in ref_nodes.values():
            for dep in n.get("deps", []):
                if dep in inner_names:
                    depended_set.add(dep)
        output_candidates = sorted(n for n in inner_names if n not in depended_set)
        output = output_candidates[0] if output_candidates else sorted(inner_names)[-1]

        tmpl_nodes_fb: dict[str, dict[str, Any]] = {}
        for n_name, n_spec in ref_nodes.items():
            tmpl_nodes_fb[n_name] = {
                **n_spec,
                **(
                    {"deps": [base_param_map.get(d, d) for d in n_spec["deps"]]}
                    if n_spec.get("deps")
                    else {}
                ),
            }

        templates[template_name] = {
            "params": params,
            "nodes": tmpl_nodes_fb,
            "output": output,
        }

        # Build per-member bind maps (each member may bind to different external nodes)
        for member in group:
            nodes.pop(member["name"], None)
            member_bind: dict[str, str] = {}
            member_inner_names = set(member["nodes"].keys())
            for n in member["nodes"].values():
                for dep in n.get("deps", []):
                    if dep not in member_inner_names:
                        param = base_param_map.get(dep, f"${dep}")
                        member_bind[param] = dep
            template_refs[member["name"]] = {
                "type": "template",
                "template": template_name,
                "bind": member_bind,
            }

    all_nodes: dict[str, Any] = {**nodes, **template_refs}

    result: dict[str, Any] = {"name": desc["name"], "nodes": all_nodes}
    if templates:
        result["templates"] = templates
    if feedback_edges:
        result["feedback"] = feedback_edges

    return result


# ---------------------------------------------------------------------------
# spec_diff
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpecDiffEntry:
    """A single change in a spec diff."""

    type: str  # "added" | "removed" | "changed"
    path: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SpecDiffResult:
    """Structural diff between two GraphSpecs."""

    entries: tuple[SpecDiffEntry, ...]
    summary: str


def spec_diff(
    spec_a: dict[str, Any],
    spec_b: dict[str, Any],
) -> SpecDiffResult:
    """Compute a structural diff between two GraphSpecs.

    Template-aware: reports 'changed template definition' vs 'changed
    instantiation bindings.' No runtime needed -- pure JSON comparison.

    Args:
        spec_a: The 'before' spec.
        spec_b: The 'after' spec.

    Returns:
        Diff entries and a human-readable summary.
    """
    entries: list[SpecDiffEntry] = []

    # Diff name
    if spec_a.get("name") != spec_b.get("name"):
        entries.append(
            SpecDiffEntry(
                type="changed",
                path="name",
                detail=f'"{spec_a.get("name")}" -> "{spec_b.get("name")}"',
            )
        )

    # Diff nodes
    nodes_a = set((spec_a.get("nodes") or {}).keys())
    nodes_b = set((spec_b.get("nodes") or {}).keys())

    for name in sorted(nodes_b - nodes_a):
        n = spec_b["nodes"][name]
        entries.append(
            SpecDiffEntry(
                type="added",
                path=f"nodes.{name}",
                detail=f"type: {n.get('type')}",
            )
        )
    for name in sorted(nodes_a - nodes_b):
        entries.append(SpecDiffEntry(type="removed", path=f"nodes.{name}"))

    for name in sorted(nodes_a & nodes_b):
        a = spec_a["nodes"][name]
        b = spec_b["nodes"][name]
        if json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True):
            details: list[str] = []
            if a.get("type") != b.get("type"):
                details.append(f"type: {a.get('type')} -> {b.get('type')}")
            if json.dumps(a.get("deps"), sort_keys=True) != json.dumps(
                b.get("deps"), sort_keys=True
            ):
                details.append("deps changed")
            if a.get("fn") != b.get("fn"):
                details.append(f"fn: {a.get('fn')} -> {b.get('fn')}")
            if json.dumps(a.get("config"), sort_keys=True) != json.dumps(
                b.get("config"), sort_keys=True
            ):
                details.append("config changed")
            entries.append(
                SpecDiffEntry(
                    type="changed",
                    path=f"nodes.{name}",
                    detail="; ".join(details) if details else "modified",
                )
            )

    # Diff templates
    tmpl_a = spec_a.get("templates") or {}
    tmpl_b = spec_b.get("templates") or {}
    tmpl_names_a = set(tmpl_a.keys())
    tmpl_names_b = set(tmpl_b.keys())

    for name in sorted(tmpl_names_b - tmpl_names_a):
        entries.append(SpecDiffEntry(type="added", path=f"templates.{name}"))
    for name in sorted(tmpl_names_a - tmpl_names_b):
        entries.append(SpecDiffEntry(type="removed", path=f"templates.{name}"))
    for name in sorted(tmpl_names_a & tmpl_names_b):
        if json.dumps(tmpl_a[name], sort_keys=True) != json.dumps(tmpl_b[name], sort_keys=True):
            entries.append(
                SpecDiffEntry(
                    type="changed",
                    path=f"templates.{name}",
                    detail="template definition changed",
                )
            )

    # Diff feedback
    fb_a: list[dict[str, Any]] = spec_a.get("feedback") or []
    fb_b: list[dict[str, Any]] = spec_b.get("feedback") or []

    def _fb_key(fb: dict[str, Any]) -> str:
        return f"{fb.get('from')}->{fb.get('to')}"

    fb_keys_a = {_fb_key(fb) for fb in fb_a}
    fb_keys_b = {_fb_key(fb) for fb in fb_b}

    for fb in fb_b:
        key = _fb_key(fb)
        if key not in fb_keys_a:
            entries.append(
                SpecDiffEntry(
                    type="added",
                    path=f"feedback.{key}",
                    detail=f"maxIterations: {fb.get('maxIterations', 10)}",
                )
            )
    for fb in fb_a:
        key = _fb_key(fb)
        if key not in fb_keys_b:
            entries.append(SpecDiffEntry(type="removed", path=f"feedback.{key}"))
    for fb in fb_a:
        key = _fb_key(fb)
        counterpart = next(
            (b for b in fb_b if b.get("from") == fb.get("from") and b.get("to") == fb.get("to")),
            None,
        )
        if counterpart and json.dumps(fb, sort_keys=True) != json.dumps(
            counterpart, sort_keys=True
        ):
            entries.append(
                SpecDiffEntry(
                    type="changed",
                    path=f"feedback.{key}",
                    detail=(
                        f"maxIterations: {fb.get('maxIterations', 10)} "
                        f"-> {counterpart.get('maxIterations', 10)}"
                    ),
                )
            )

    # Build summary
    added = sum(1 for e in entries if e.type == "added")
    removed = sum(1 for e in entries if e.type == "removed")
    changed = sum(1 for e in entries if e.type == "changed")
    parts: list[str] = []
    if added:
        parts.append(f"{added} added")
    if removed:
        parts.append(f"{removed} removed")
    if changed:
        parts.append(f"{changed} changed")
    summary = ", ".join(parts) if parts else "no changes"

    return SpecDiffResult(entries=tuple(entries), summary=summary)


# ---------------------------------------------------------------------------
# LLM compose / refine
# ---------------------------------------------------------------------------

_FENCE_PATTERN = _re.compile(r"^```(?:json)?\s*([\s\S]*?)\s*```[\s\S]*$")


def _strip_fences(text: str) -> str:
    """Strip markdown code fences, handling trailing commentary."""
    m = _FENCE_PATTERN.match(text)
    return m.group(1) if m else text


_LLM_COMPOSE_SYSTEM_PROMPT = """\
You are a graph architect for GraphReFly, a reactive graph protocol.

Given a natural-language description, produce a JSON GraphSpec with this structure:

{
  "name": "<graph_name>",
  "nodes": {
    "<node_name>": {
      "type": "state" | "derived" | "producer" | "effect" | "operator",
      "deps": ["<dep_node_name>", ...],
      "fn": "<catalog_function_name>",
      "source": "<catalog_source_name>",
      "config": { ... },
      "initial": <value>,
      "meta": { "description": "<purpose>" }
    },
    "<template_instance>": {
      "type": "template",
      "template": "<template_name>",
      "bind": { "$param": "node_name" }
    }
  },
  "templates": {
    "<template_name>": {
      "params": ["$param1", "$param2"],
      "nodes": { ... },
      "output": "<output_node>"
    }
  },
  "feedback": [
    { "from": "<condition_node>", "to": "<state_node>", "maxIterations": 10 }
  ]
}

Rules:
- "state" nodes hold user/LLM-writable values (knobs). Use "initial" for default values.
- "derived" nodes compute from deps using a named "fn".
- "effect" nodes produce side effects from deps.
- "producer" nodes generate values from a named "source".
- Use "templates" when the same subgraph pattern repeats (e.g., per-source resilience).
- Use "feedback" for bounded cycles where a derived value writes back to a state node.
- meta.description is required for every node.
- Return ONLY valid JSON, no markdown fences or commentary."""


def llm_compose(
    problem: str,
    adapter: LLMAdapter,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    system_prompt_extra: str | None = None,
    catalog_description: str | None = None,
) -> dict[str, Any]:
    """Ask an LLM to compose a GraphSpec from a natural-language problem description.

    The LLM generates a GraphSpec (with templates + feedback), validated before
    returning. The spec is for human review before compilation via ``compile_spec()``.

    Args:
        problem: Natural language problem description.
        adapter: LLM adapter for the generation call.
        model: Optional model override.
        temperature: Optional temperature (default 0).
        max_tokens: Optional max tokens.
        system_prompt_extra: Extra instructions appended to the system prompt.
        catalog_description: Available fn/source catalog names for the LLM to reference.

    Returns:
        A validated GraphSpec dict.

    Raises:
        ValueError: On invalid LLM output or validation failure.
    """
    from graphrefly.patterns.ai import (
        ChatMessage,
        LLMInvokeOptions,
        LLMResponse,
    )

    sys_prompt = _LLM_COMPOSE_SYSTEM_PROMPT
    if catalog_description:
        sys_prompt = f"{sys_prompt}\n\nAvailable catalog:\n{catalog_description}"
    if system_prompt_extra:
        sys_prompt = f"{sys_prompt}\n\n{system_prompt_extra}"

    messages = [
        ChatMessage(role="system", content=sys_prompt),
        ChatMessage(role="user", content=problem),
    ]

    raw_result = adapter.invoke(
        messages,
        LLMInvokeOptions(
            model=model,
            temperature=temperature if temperature is not None else 0.0,
            max_tokens=max_tokens,
        ),
    )

    response = raw_result
    if not isinstance(response, LLMResponse):
        msg = f"llm_compose: expected LLMResponse, got {type(response).__name__}"
        raise ValueError(msg)

    content = response.content.strip()
    if content.startswith("```"):
        content = _strip_fences(content)

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        msg = f"llm_compose: LLM response is not valid JSON: {content[:200]}"
        raise ValueError(msg) from exc

    validation = validate_spec(parsed)
    if not validation.valid:
        detail = "\n".join(validation.errors)
        msg = f"llm_compose: invalid GraphSpec:\n{detail}"
        raise ValueError(msg)

    result: dict[str, Any] = parsed
    return result


def llm_refine(
    current_spec: dict[str, Any],
    feedback: str,
    adapter: LLMAdapter,
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    system_prompt_extra: str | None = None,
    catalog_description: str | None = None,
) -> dict[str, Any]:
    """Ask an LLM to modify an existing GraphSpec based on feedback.

    Args:
        current_spec: The current GraphSpec to modify.
        feedback: Natural language feedback or changed requirements.
        adapter: LLM adapter for the generation call.
        model: Optional model override.
        temperature: Optional temperature (default 0).
        max_tokens: Optional max tokens.
        system_prompt_extra: Extra instructions appended to the system prompt.
        catalog_description: Available fn/source catalog names for the LLM to reference.

    Returns:
        A new GraphSpec dict incorporating the feedback.

    Raises:
        ValueError: On invalid LLM output or validation failure.
    """
    from graphrefly.patterns.ai import (
        ChatMessage,
        LLMInvokeOptions,
        LLMResponse,
    )

    sys_prompt = _LLM_COMPOSE_SYSTEM_PROMPT
    if catalog_description:
        sys_prompt = f"{sys_prompt}\n\nAvailable catalog:\n{catalog_description}"
    if system_prompt_extra:
        sys_prompt = f"{sys_prompt}\n\n{system_prompt_extra}"

    messages = [
        ChatMessage(role="system", content=sys_prompt),
        ChatMessage(
            role="user",
            content=(
                f"Current GraphSpec:\n{json.dumps(current_spec, indent=2)}\n\n"
                f"Modification request: {feedback}\n\n"
                "Return the complete modified GraphSpec as JSON."
            ),
        ),
    ]

    raw_result = adapter.invoke(
        messages,
        LLMInvokeOptions(
            model=model,
            temperature=temperature if temperature is not None else 0.0,
            max_tokens=max_tokens,
        ),
    )

    response = raw_result
    if not isinstance(response, LLMResponse):
        msg = f"llm_refine: expected LLMResponse, got {type(response).__name__}"
        raise ValueError(msg)

    content = response.content.strip()
    if content.startswith("```"):
        content = _strip_fences(content)

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        msg = f"llm_refine: LLM response is not valid JSON: {content[:200]}"
        raise ValueError(msg) from exc

    validation = validate_spec(parsed)
    if not validation.valid:
        detail = "\n".join(validation.errors)
        msg = f"llm_refine: invalid GraphSpec:\n{detail}"
        raise ValueError(msg)

    result: dict[str, Any] = parsed
    return result


__all__ = [
    "FnFactory",
    "GraphSpec",
    "GraphSpecCatalog",
    "GraphSpecFeedbackEdge",
    "GraphSpecNode",
    "GraphSpecTemplate",
    "GraphSpecTemplateRef",
    "GraphSpecValidation",
    "SourceFactory",
    "SpecDiffEntry",
    "SpecDiffResult",
    "compile_spec",
    "decompile_graph",
    "llm_compose",
    "llm_refine",
    "spec_diff",
    "validate_spec",
]
