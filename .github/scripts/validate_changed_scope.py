from __future__ import annotations

import argparse
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DIRECT_SCOPE_ROOTS = frozenset({".github", "benchmarks", "deploy", "examples", "tests"})


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate only Python files and owners changed from a Git base revision."
    )
    parser.add_argument("--base", required=True, help="Git revision used as the comparison base")
    return parser.parse_args()


def _run(command: Sequence[str]) -> None:
    print(f"+ {shlex.join(command)}", flush=True)
    subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)


def _output(command: Sequence[str]) -> str:
    result = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def _comparison(base: str) -> list[Path]:
    if base and set(base) != {"0"}:
        names = _output(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD", "--"]
        )
    else:
        names = _output(
            ["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "HEAD"]
        )
    return [Path(name) for name in names.splitlines() if name]


def _owner(path: Path) -> Path | None:
    for depth in range(len(path.parts) - 1, 0, -1):
        candidate = Path(*path.parts[:depth])
        if (REPOSITORY_ROOT / candidate / "pyproject.toml").is_file():
            return candidate
    return None


def _is_owner_test(path: Path, owner: Path) -> bool:
    return len(path.parts) > len(owner.parts) and path.parts[len(owner.parts)] == "tests"


def _is_opt_in_e2e(path: Path) -> bool:
    return path.parts[:2] == ("tests", "e2e")


def _existing(paths: set[Path]) -> list[str]:
    return sorted(str(path) for path in paths if (REPOSITORY_ROOT / path).exists())


def _validate(base: str) -> None:
    changed = _comparison(base)
    python_files = {
        path for path in changed if path.suffix == ".py" and (REPOSITORY_ROOT / path).is_file()
    }

    if any(path.name == "pyproject.toml" or path == Path("uv.lock") for path in changed):
        _run(["uv", "lock", "--check"])

    if python_files:
        files = _existing(python_files)
        _run(["uv", "run", "--group", "dev", "ruff", "check", *files])
        _run(["uv", "run", "--group", "dev", "ruff", "format", "--check", *files])

    typing_targets: set[Path] = set()
    test_targets: set[Path] = set()
    production_owners: set[Path] = set()

    for path in changed:
        owner = _owner(path)
        if owner is not None and path.name == "pyproject.toml":
            production_owners.add(owner)

    for path in python_files:
        if _is_opt_in_e2e(path):
            continue
        owner = _owner(path)
        if owner is not None:
            if _is_owner_test(path, owner):
                typing_targets.add(path)
                if path.name.startswith("test_"):
                    test_targets.add(path)
            else:
                production_owners.add(owner)
            continue
        if path.parts and path.parts[0] in DIRECT_SCOPE_ROOTS:
            typing_targets.add(path)
            if path.name.startswith("test_"):
                test_targets.add(path)

    for owner in production_owners:
        typing_targets.add(owner)
        tests = owner / "tests"
        if (REPOSITORY_ROOT / tests).is_dir():
            test_targets.add(tests)

    if Path("pyproject.toml") in changed:
        typing_targets.update(
            path
            for name in ("apps", "packages", "tests")
            if (REPOSITORY_ROOT / (path := Path(name))).exists()
        )

    typing = _existing(typing_targets)
    if typing:
        _run(["uv", "run", "--group", "dev", "basedpyright", *typing])

    tests = _existing(test_targets)
    if tests:
        _run(["uv", "run", "--group", "dev", "pytest", "-q", *tests])

    if not python_files and not typing_targets and not test_targets:
        print("No changed Python validation scope.", flush=True)


if __name__ == "__main__":
    _validate(_arguments().base)
