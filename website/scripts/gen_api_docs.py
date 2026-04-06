#!/usr/bin/env python3
"""Generate Starlight API pages from docstrings in graphrefly.extra modules.

Scanned modules are listed in ``EXTRA_MODULES`` (tier1, tier2, sources, backoff,
checkpoint, resilience, data_structures, …). When the same export name appears in
more than one scanned module, set ``EXPORT_DOC_STEM`` so each page gets a unique
filename.

Emits one page per ``__all__`` name (after ``EXPORT_DOC_STEM`` remapping) that maps
to a top-level function, async
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
    ("composite", REPO / "src/graphrefly/extra/composite.py"),
    ("backpressure", REPO / "src/graphrefly/extra/backpressure.py"),
    ("reactive_layout", REPO / "src/graphrefly/patterns/reactive_layout/reactive_layout.py"),
    ("reactive_block_layout", REPO / "src/graphrefly/patterns/reactive_layout/reactive_block_layout.py"),
]

# One markdown file per stem below. When two scanned modules use the same export name,
# map (module label, export name) -> distinct filename stem.
EXPORT_DOC_STEM: dict[tuple[str, str], str] = {
    # ``graphrefly.extra.timeout`` is the tier2 pipe operator; resilience exposes a
    # node-level DATA deadline combinator under the same local name.
    ("resilience", "timeout"): "timeout_node",
}

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


# ---------------------------------------------------------------------------
# Google-style docstring parser
# ---------------------------------------------------------------------------

_SECTION_HEADERS = ("Args:", "Returns:", "Raises:", "Example:", "Examples:", "Notes:", "Note:")


def _parse_docstring(doc: str) -> dict[str, str]:
    """Split a Google-style docstring into {summary, body, args, returns, raises, example, notes}.

    Each value is the raw text block (leading indent stripped).
    """
    lines = doc.strip().splitlines()

    # Collect summary — everything before the first blank line or section header.
    summary_lines: list[str] = []
    idx = 0
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped in _SECTION_HEADERS:
            break
        if not stripped and summary_lines:
            idx += 1
            break
        if stripped:
            summary_lines.append(stripped)
    else:
        idx = len(lines)

    result: dict[str, str] = {"summary": " ".join(summary_lines)}

    # Collect body paragraphs between summary and first section header.
    body_lines: list[str] = []
    while idx < len(lines):
        stripped = lines[idx].strip()
        if stripped in _SECTION_HEADERS:
            break
        body_lines.append(lines[idx])
        idx += 1
    body_text = "\n".join(body_lines).strip()
    if body_text:
        result["body"] = body_text

    # Parse named sections.
    while idx < len(lines):
        header = lines[idx].strip()
        if header not in _SECTION_HEADERS:
            idx += 1
            continue
        key = header.rstrip(":").lower()
        # Normalize "examples" -> "example"
        if key == "examples":
            key = "example"
        if key == "notes":
            key = "note"
        idx += 1
        section_lines: list[str] = []
        while idx < len(lines):
            if lines[idx].strip() in _SECTION_HEADERS:
                break
            section_lines.append(lines[idx])
            idx += 1
        # Dedent section body.
        result[key] = _dedent_block(section_lines)

    return result


def _dedent_block(lines: list[str]) -> str:
    """Remove common leading whitespace from a block of lines."""
    non_empty = [ln for ln in lines if ln.strip()]
    if not non_empty:
        return ""
    min_indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
    return "\n".join(ln[min_indent:] for ln in lines).strip()


def _parse_args_section(text: str) -> list[tuple[str, str]]:
    """Parse 'Args:' section into (name, description) pairs."""
    params: list[tuple[str, str]] = []
    current_name = ""
    current_desc_lines: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current_name:
                current_desc_lines.append("")
            continue
        # New param: starts with "name:" or "name (type):" at base indent
        indent = len(line) - len(line.lstrip())
        if indent == 0 and ":" in stripped:
            # Save previous param
            if current_name:
                params.append((current_name, " ".join(current_desc_lines).strip()))
            colon_pos = stripped.index(":")
            current_name = stripped[:colon_pos].strip()
            # Strip leading ** from **name style
            current_name = current_name.strip("*")
            current_desc_lines = [stripped[colon_pos + 1 :].strip()]
        elif current_name:
            current_desc_lines.append(stripped)

    if current_name:
        params.append((current_name, " ".join(current_desc_lines).strip()))

    return params


def _render_params_table(args_text: str) -> str:
    """Render a Parameters table from parsed args."""
    params = _parse_args_section(args_text)
    if not params:
        return ""
    lines = [
        "## Parameters",
        "",
        "| Parameter | Description |",
        "|-----------|-------------|",
    ]
    for pname, desc in params:
        lines.append(f"| `{_escape_html(pname)}` | {_escape_html(desc)} |")
    return "\n".join(lines)


def _extract_code_block(text: str) -> tuple[str, str]:
    """Split text into (code_block, remaining_text).

    Handles both ```python fenced blocks and >>> doctest lines.
    """
    lines = text.splitlines()

    # Check for fenced code block.
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            # Find the closing fence.
            for j in range(i + 1, len(lines)):
                if lines[j].strip().startswith("```"):
                    lang = stripped[3:].strip() or "python"
                    code = "\n".join(lines[i + 1 : j])
                    remaining = "\n".join(lines[j + 1 :]).strip()
                    return f"```{lang}\n{code}\n```", remaining
            break

    # Check for doctest style (>>>).
    doctest_lines: list[str] = []
    rest_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(">>>") or stripped.startswith("..."):
            doctest_lines.append(stripped.lstrip(">").lstrip(".").lstrip(" ") if stripped.startswith(">>>") else stripped.lstrip(".").lstrip(" "))
            rest_start = i + 1
        elif doctest_lines and not stripped:
            rest_start = i + 1
            break
        elif doctest_lines:
            rest_start = i
            break

    if doctest_lines:
        code = "\n".join(doctest_lines)
        remaining = "\n".join(lines[rest_start:]).strip()
        return f"```python\n{code}\n```", remaining

    # No code block found — return the text as-is description.
    return "", text


def _page_md(name: str, sig: str, doc: str | None, *, kind: str) -> str:
    summary_text = _first_line(doc)
    desc = summary_text[:160] if summary_text else f"API reference for `{name}` ({kind})."
    parts = [
        "---",
        f"title: {name!r}",
        f"description: {_escape_html(desc)!r}",
        "---",
        "",
    ]

    if not doc or not doc.strip():
        if summary_text:
            parts.append(_escape_html(summary_text))
            parts.append("")
        parts.append("## Signature")
        parts.append("")
        parts.append("```python")
        parts.append(sig)
        parts.append("```")
        parts.append("")
        return "\n".join(parts)

    parsed = _parse_docstring(doc)

    # Summary
    if parsed.get("summary"):
        parts.append(_escape_html(parsed["summary"]))
        parts.append("")

    # Body (extra description paragraphs)
    if parsed.get("body"):
        parts.append(_escape_html(parsed["body"]))
        parts.append("")

    # Signature
    parts.append("## Signature")
    parts.append("")
    parts.append("```python")
    parts.append(sig)
    parts.append("```")
    parts.append("")

    # Parameters table
    if parsed.get("args"):
        table = _render_params_table(parsed["args"])
        if table:
            parts.append(table)
            parts.append("")

    # Returns
    if parsed.get("returns"):
        parts.append("## Returns")
        parts.append("")
        parts.append(_escape_html(parsed["returns"]))
        parts.append("")

    # Raises
    if parsed.get("raises"):
        parts.append("## Raises")
        parts.append("")
        parts.append(_escape_html(parsed["raises"]))
        parts.append("")

    # Example / Basic Usage
    if parsed.get("example"):
        code_block, remaining = _extract_code_block(parsed["example"])
        if code_block:
            parts.append("## Basic Usage")
            parts.append("")
            parts.append(code_block)
            parts.append("")
            if remaining.strip():
                parts.append(_escape_html(remaining))
                parts.append("")
        else:
            # No code block found, render as description
            parts.append("## Example")
            parts.append("")
            parts.append(_escape_html(parsed["example"]))
            parts.append("")

    # Notes
    if parsed.get("note"):
        parts.append("## Notes")
        parts.append("")
        parts.append(_escape_html(parsed["note"]))
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
            doc_stem = EXPORT_DOC_STEM.get((label, name), name)
            if doc_stem in nodes:
                msg = f"duplicate export doc stem {doc_stem!r} across scanned modules"
                raise ValueError(msg)
            node = exports[name]
            doc_names.append(doc_stem)
            nodes[doc_stem] = node
            sources[doc_stem] = source
            kinds[doc_stem] = type(node).__name__

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
