"""Enforce Python file and function size limits across source and tests."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILE_LIMIT = 500
FUNCTION_LIMIT = 80
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


def main() -> int:
    """Report files or functions that exceed the configured size limits."""
    failures = _measure(ROOT)
    if failures:
        print("\n".join(failures))
        return 1
    print("Python file and function sizes are within limits.")
    return 0


def _measure(root: Path) -> list[str]:
    """Check file and function lengths, including non-exempt test functions."""
    failures = []
    for path in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        source = path.read_text()
        line_count = len(source.splitlines())
        if line_count > FILE_LIMIT:
            failures.append(f"{relative} has {line_count} lines; limit is {FILE_LIMIT}")
        tree = ast.parse(source)
        for name, length in _functions(tree):
            if length > FUNCTION_LIMIT:
                failures.append(
                    f"{relative}:{name} has {length} lines; limit is {FUNCTION_LIMIT}"
                )
    return failures


def _functions(tree: ast.AST) -> list[tuple[str, int]]:
    """Collect qualified function names and physical line counts."""
    found: list[tuple[str, int]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._stack.append(node.name)
            self.generic_visit(node)
            self._stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._record(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._record(node)

        def _record(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            qualname = ".".join([*self._stack, node.name])
            end = node.end_lineno or node.lineno
            found.append((f"{qualname}:{node.lineno}", end - node.lineno + 1))
            self._stack.append(node.name)
            self.generic_visit(node)
            self._stack.pop()

    Visitor().visit(tree)
    return found


if __name__ == "__main__":
    sys.exit(main())
