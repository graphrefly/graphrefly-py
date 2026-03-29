#!/usr/bin/env python3
"""Generate Starlight API pages from docstrings in graphrefly.extra.tier1.

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
TIER1 = REPO / "src/graphrefly/extra/tier1.py"
WEBSITE = Path(__file__).resolve().parent.parent
OUT = WEBSITE / "src/content/docs/api"


def _load_all_order(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    elts = getattr(node.value, "elts", [])
                    for elt in elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            names.append(elt.value)
    return names


def _top_level_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _function_signature(source: str, fn: ast.FunctionDef) -> str:
    lines = source.splitlines()
    start = fn.lineno - 1
    end = (
        fn.body[0].lineno - 1
        if fn.body
        else (fn.end_lineno or fn.lineno) - 1
    )
    chunk = "\n".join(lines[start:end]).strip()
    if chunk.endswith(":"):
        chunk = chunk[:-1].strip()
    return chunk


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


def _page_md(name: str, sig: str, doc: str | None) -> str:
    summary = _first_line(doc)
    desc = summary[:160] if summary else f"API reference for `{name}`."
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
        'title: "API (extra)"',
        (
            'description: "Tier-1 pipe operators from graphrefly.extra — '
            'generated from source docstrings."'
        ),
        "---",
        "",
        (
            "Reference pages for each public function in `graphrefly.extra.tier1` "
            "(see `docs/docs-guidance.md`)."
        ),
        "",
        "## Operators",
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

    source = TIER1.read_text(encoding="utf-8")
    tree = ast.parse(source)
    order = _load_all_order(tree)
    funcs = _top_level_functions(tree)
    OUT.mkdir(parents=True, exist_ok=True)

    stale = 0
    doc_names: list[str] = []
    for name in order:
        if name not in funcs:
            continue
        doc_names.append(name)
        fn = funcs[name]
        sig = _function_signature(source, fn)
        md = _page_md(name, sig, ast.get_docstring(fn))
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
