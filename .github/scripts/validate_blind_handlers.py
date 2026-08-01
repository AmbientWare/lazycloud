from __future__ import annotations

import ast
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BROAD_NAMES = frozenset({"Exception", "BaseException"})
LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})
TRACEBACK_CALLS = frozenset({"format_exc", "print_exc"})


def _catches_broadly(handler: ast.ExceptHandler) -> bool:
    """A bare `except`, or one naming Exception/BaseException."""
    if handler.type is None:
        return True
    caught = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(node, ast.Name) and node.id in BROAD_NAMES for node in caught)


def _body_nodes(body: list[ast.stmt]) -> list[ast.AST]:
    return list(ast.walk(ast.Module(body=body, type_ignores=[])))


def _reports(handler: ast.ExceptHandler) -> bool:
    """Does the cause reach a sink outside this handler?

    Re-raising, logging a traceback, or naming the bound exception all carry it
    somewhere a reader can find it. Only a handler that does none of these
    leaves an unexpected failure with no trace anywhere.
    """
    for node in _body_nodes(handler.body):
        if isinstance(node, ast.Raise):
            return True
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not isinstance(function, ast.Attribute):
            continue
        if function.attr == "exception" or function.attr in TRACEBACK_CALLS:
            return True
        if function.attr in LOG_LEVELS and any(
            keyword.arg == "exc_info" for keyword in node.keywords
        ):
            return True
    if handler.name is None:
        return False
    return any(
        isinstance(node, ast.Name) and node.id == handler.name for node in _body_nodes(handler.body)
    )


def _source_files() -> list[Path]:
    files: list[Path] = []
    for group in ("packages", "apps"):
        files.extend(sorted((REPOSITORY_ROOT / group).glob("*/src/**/*.py")))
    return files


def main() -> int:
    findings: list[str] = []
    for path in _source_files():
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as exc:
            print(f"{path}: could not parse ({exc})")
            return 1
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ExceptHandler)
                and _catches_broadly(node)
                and not _reports(node)
            ):
                findings.append(f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}")
    if not findings:
        return 0
    print("Handlers that catch everything and discard the cause:")
    for finding in findings:
        print(f"  {finding}")
    print(
        "\nA broad handler must re-raise, log with the traceback "
        "(`logger.exception` or `exc_info=`), or bind the exception and carry "
        "it into the value it returns."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
