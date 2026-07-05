"""Check that exported Python APIs have source docstrings for generated docs."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "graphrefly"
MIN_WORDS = 4

SOURCE_BY_IMPORT = {
    "graphrefly._facade": PACKAGE / "_facade.py",
    "graphrefly.exceptions": PACKAGE / "exceptions.py",
    "graphrefly.issues": PACKAGE / "issues.py",
}

ALIAS_OR_CONSTANT_EXPORTS = {
    "GraphCheckpoint",
    "HttpStreamDriverEvent",
    "Message",
    "RestoreRegistry",
    "SENTINEL",
    "__version__",
}


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def exported_names() -> tuple[list[str], dict[str, Path]]:
    module = parse(PACKAGE / "__init__.py")
    imports: dict[str, Path] = {}
    names: list[str] = []
    for node in module.body:
        if isinstance(node, ast.ImportFrom) and node.module in SOURCE_BY_IMPORT:
            source = SOURCE_BY_IMPORT[node.module]
            for alias in node.names:
                imports[alias.asname or alias.name] = source
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    names = [
                        item.value
                        for item in node.value.elts
                        if isinstance(item, ast.Constant)
                    ]
    if not names:
        raise SystemExit("graphrefly.__all__ is missing or not a literal string list")
    return names, imports


def has_useful_docstring(node: ast.AST) -> bool:
    doc = ast.get_docstring(node)
    if doc is None:
        return False
    return len(doc.split()) >= MIN_WORDS


def is_overload(node: ast.FunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Name) and decorator.id == "overload"
        or isinstance(decorator, ast.Attribute) and decorator.attr == "overload"
        for decorator in node.decorator_list
    )


def is_property_setter(node: ast.FunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Attribute) and decorator.attr in {"setter", "deleter"}
        for decorator in node.decorator_list
    )


def public_members(class_node: ast.ClassDef) -> list[ast.FunctionDef]:
    by_name: dict[str, ast.FunctionDef] = {}
    for item in class_node.body:
        if not isinstance(item, ast.FunctionDef):
            continue
        if item.name.startswith("_") or item.name in {"__enter__", "__exit__"}:
            continue
        if is_overload(item) or is_property_setter(item):
            continue
        by_name[item.name] = item
    return list(by_name.values())


def definitions_by_source(paths: set[Path]) -> dict[Path, dict[str, ast.AST]]:
    definitions: dict[Path, dict[str, ast.AST]] = {}
    for path in paths:
        module = parse(path)
        definitions[path] = {
            node.name: node
            for node in module.body
            if isinstance(node, ast.ClassDef | ast.FunctionDef)
        }
    return definitions


def main() -> None:
    names, imports = exported_names()
    definitions = definitions_by_source(set(imports.values()))
    failures: list[str] = []

    for name in names:
        if name in ALIAS_OR_CONSTANT_EXPORTS:
            continue
        source = imports.get(name)
        if source is None:
            failures.append(f"{name}: exported without a known source import")
            continue
        node = definitions[source].get(name)
        if node is None:
            failures.append(f"{name}: exported but no class/function definition found in {source}")
            continue
        if isinstance(node, ast.ClassDef | ast.FunctionDef) and not has_useful_docstring(node):
            failures.append(
                f"{source.relative_to(ROOT)}:{node.lineno}: {name} lacks a useful docstring"
            )
        if isinstance(node, ast.ClassDef):
            for member in public_members(node):
                if not has_useful_docstring(member):
                    failures.append(
                        f"{source.relative_to(ROOT)}:{member.lineno}: "
                        f"{name}.{member.name} lacks a useful docstring"
                    )

    if failures:
        print("API documentation coverage failed:")
        for failure in failures:
            print(f"- {failure}")
        raise SystemExit(1)
    print(f"API documentation coverage ok: {len(names)} exports checked")


if __name__ == "__main__":
    main()
