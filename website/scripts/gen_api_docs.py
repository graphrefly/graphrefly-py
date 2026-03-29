#!/usr/bin/env python3
"""Generate Starlight API pages from docstrings in graphrefly.extra modules.

Scanned modules are listed in ``EXTRA_MODULES`` (tier1, tier2, sources, backoff,
checkpoint, resilience, data_structures, …).

Emits one page per ``__all__`` name that maps to a top-level function, async
function, class, or :pep:`695` type alias (``type X = ...``).

Usage (from repo root):
  uv run python website/scripts/gen_api_docs.py
  uv run python website/scripts/gen_api_docs.py --check

Output: website/src/content/docs/api/<name>.md and index.md
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
# Core must use defining modules — ``__init__.py`` only re-exports (no top-level defs for the scanner).
EXTRA_MODULES: list[tuple[str, Path]] = [
    ("core_node", REPO / "src/graphrefly/core/node.py"),
    ("core_sugar", REPO / "src/graphrefly/core/sugar.py"),
    ("core_protocol", REPO / "src/graphrefly/core/protocol.py"),
    ("tier1", REPO / "src/graphrefly/extra/tier1.py"),
    ("tier2", REPO / "src/graphrefly/extra/tier2.py"),
    ("sources", REPO / "src/graphrefly/extra/sources.py"),
    ("backoff", REPO / "src/graphrefly/extra/backoff.py"),
    ("checkpoint", REPO / "src/graphrefly/extra/checkpoint.py"),
    ("resilience", REPO / "src/graphrefly/extra/resilience.py"),
    ("data_structures", REPO / "src/graphrefly/extra/data_structures.py"),
]
WEBSITE = Path(__file__).resolve().parent.parent
OUT = WEBSITE / "src/content/docs/api"


def _all_literal_strings(value: ast.expr | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (ast.List, ast.Tuple)):
        out: list[str] = []
        for elt in value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
        return out
    return []


def _load_all_order(tree: ast.Module) -> list[str]:
    """Parse ``__all__`` from ``=`` or annotated assignment."""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    names.extend(_all_literal_strings(node.value))
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
            and node.value is not None
        ):
            names.extend(_all_literal_strings(node.value))
    return names


def _type_alias_name(node: ast.TypeAlias) -> str:
    n = node.name
    if isinstance(n, ast.Name):
        return n.id
    return str(n)


def _collect_top_level_exports(tree: ast.Module) -> dict[str, ast.AST]:
    """Map export name -> AST node (function, async function, class, or type alias)."""
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = node
        elif isinstance(node, ast.TypeAlias):
            out[_type_alias_name(node)] = node
    return out


def _function_signature(source: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    lines = source.splitlines()
    start = fn.lineno - 1
    end = fn.body[0].lineno - 2 if fn.body else (fn.end_lineno or fn.lineno) - 1
    chunk = "\n".join(lines[start : end + 1]).strip()
    if chunk.endswith(":"):
        chunk = chunk[:-1].strip()
    prefix = "async " if isinstance(fn, ast.AsyncFunctionDef) else ""
    if not chunk.startswith("async ") and prefix:
        chunk = prefix + chunk
    return chunk


def _class_signature(source: str, cls: ast.ClassDef) -> str:
    lines = source.splitlines()
    start = cls.lineno - 1
    if not cls.body:
        end = (cls.end_lineno or cls.lineno) - 1
    else:
        # Lines through the end of the class header (before first body statement).
        end_exclusive = cls.body[0].lineno - 1
        end = end_exclusive - 1
    chunk = "\n".join(lines[start : end + 1]).strip()
    if chunk.endswith(":"):
        chunk = chunk[:-1].strip()
    return chunk


def _type_alias_signature(node: ast.TypeAlias) -> str:
    return ast.unparse(node).strip()


def _export_signature(source: str, node: ast.AST) -> str:
    if isinstance(node, ast.TypeAlias):
        return _type_alias_signature(node)
    if isinstance(node, ast.ClassDef):
        return _class_signature(source, node)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _function_signature(source, node)
    msg = f"unsupported export AST {type(node).__name__}"
    raise TypeError(msg)


def _export_docstring(node: ast.AST) -> str | None:
    if isinstance(node, ast.TypeAlias):
        return None
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return ast.get_docstring(node)
    return None


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _first_line(doc: str | None) -> str:
    if not doc:
        return ""
    for line in doc.strip().splitlines():
        s = line.strip()
        if s:
            return s
    return ""


def _page_md(name: str, sig: str, doc: str | None, *, kind: str) -> str:
    summary = _first_line(doc)
    desc = summary[:160] if summary else f"API reference for `{name}` ({kind})."
    parts = [
        "---",
        f"title: {name!r}",
        f"description: {desc!r}",
        "---",
        "",
    ]
    if summary:
        parts.append(_escape_html(summary))
        parts.append("")
    parts.append("## Signature")
    parts.append("")
    parts.append("```python")
    parts.append(sig)
    parts.append("```")
    parts.append("")
    if doc and doc.strip():
        parts.append("## Documentation")
        parts.append("")
        parts.append(_escape_html(doc.strip()))
        parts.append("")
    return "\n".join(parts)


def _index_md(names: list[str]) -> str:
    lines = [
        "---",
        'title: "API reference"',
        (
            'description: "Core primitives (``graphrefly.core``) and extra operators '
            '(``graphrefly.extra``) — generated from source docstrings."'
        ),
        "---",
        "",
        (
            "Reference pages for modules listed in "
            "`website/scripts/gen_api_docs.py` (`EXTRA_MODULES`, including core); "
            "see `docs/docs-guidance.md`."
        ),
        "",
        "## API index",
        "",
    ]
    for n in names:
        lines.append(f"- [{n}](./{n}/)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if generated output would change",
    )
    args = parser.parse_args()

    doc_names: list[str] = []
    nodes: dict[str, ast.AST] = {}
    kinds: dict[str, str] = {}
    sources: dict[str, str] = {}

    for label, mod_path in EXTRA_MODULES:
        source = mod_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        order = _load_all_order(tree)
        exports = _collect_top_level_exports(tree)
        for name in order:
            if name not in exports:
                continue
            # Case-insensitive FS: Node.md vs node.md collide; keep `node()` as primary API page.
            if name == "Node":
                continue
            if name in nodes:
                msg = f"duplicate export {name!r} across scanned modules"
                raise ValueError(msg)
            node = exports[name]
            doc_names.append(name)
            nodes[name] = node
            sources[name] = source
            kinds[name] = type(node).__name__

    OUT.mkdir(parents=True, exist_ok=True)

    stale = 0
    for name in doc_names:
        node = nodes[name]
        sig = _export_signature(sources[name], node)
        doc = _export_docstring(node)
        md = _page_md(name, sig, doc, kind=kinds[name])
        path = OUT / f"{name}.md"
        if args.check:
            if not path.exists():
                print(f"  missing {path.name}")
                stale += 1
            elif path.read_text(encoding="utf-8") != md:
                print(f"  stale {path.name}")
                stale += 1
            else:
                print(f"  ok {path.name}")
        else:
            path.write_text(md, encoding="utf-8")
            print(f"  wrote {path.name}")

    idx = _index_md(doc_names)
    index_path = OUT / "index.md"
    if args.check:
        if not index_path.exists() or index_path.read_text(encoding="utf-8") != idx:
            print("  stale or missing index.md")
            stale += 1
        elif stale == 0:
            print("  ok index.md")
    else:
        index_path.write_text(idx, encoding="utf-8")
        print("  wrote index.md")

    if args.check and stale:
        print(f"\n{stale} file(s) stale or missing. Regenerate without --check.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
