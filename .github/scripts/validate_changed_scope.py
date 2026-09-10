from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DIRECT_SCOPE_ROOTS = frozenset({".github", "benchmarks", "deploy", "examples", "tests"})


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate only Python files and owners changed from a Git base revision."
    )
    parser.add_argument("--base", required=True, help="Git revision used as the comparison base")
    parser.add_argument("--check", choices=("all", "types", "tests"), default="all")
    parser.add_argument("--junitxml", type=Path)
    parser.add_argument(
        "--list", action="store_true", help="Print selected paths without running checks"
    )
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
        ancestor = _output(["git", "merge-base", base, "HEAD"]).strip()
        names = _output(["git", "diff", "--name-only", "--no-renames", ancestor, "--"])
    else:
        names = _output(
            ["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "HEAD"]
        )
    untracked = _output(["git", "ls-files", "--others", "--exclude-standard"])
    return sorted({Path(name) for name in (names + untracked).splitlines() if name})


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


def _workspace_dependents(owners: set[Path]) -> set[Path]:
    """Workspace members that depend, directly or through others, on any owner.

    A change in a package is only proven safe once the members built on top of
    it still import and boot, so their owner tests join the changed scope.
    """
    manifests = [
        *REPOSITORY_ROOT.glob("packages/*/pyproject.toml"),
        *REPOSITORY_ROOT.glob("packages/providers/*/pyproject.toml"),
        *REPOSITORY_ROOT.glob("apps/*/pyproject.toml"),
    ]
    directory_of: dict[str, Path] = {}
    requirements: dict[str, set[str]] = {}
    for manifest in manifests:
        project = tomllib.loads(manifest.read_text(encoding="utf-8")).get("project", {})
        name = str(project.get("name", "")).strip().lower()
        if not name:
            continue
        directory_of[name] = manifest.parent.relative_to(REPOSITORY_ROOT)
        requirements[name] = {
            _requirement_name(item)
            for item in project.get("dependencies", [])
            if isinstance(item, str)
        }
    dependents_of: dict[str, set[str]] = {name: set() for name in directory_of}
    for name, required in requirements.items():
        for requirement in required & directory_of.keys():
            dependents_of[requirement].add(name)
    pending = [name for name, directory in directory_of.items() if directory in owners]
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(dependents_of[name])
    return {directory_of[name] for name in seen} - owners


def _requirement_name(requirement: str) -> str:
    name = requirement.strip()
    for separator in ("[", ">", "<", "=", "!", "~", ";", " "):
        name = name.split(separator, 1)[0]
    return name.strip().lower()


def _existing(paths: set[Path]) -> list[str]:
    existing = {path for path in paths if (REPOSITORY_ROOT / path).exists()}
    return sorted(str(path) for path in existing if not any(p in existing for p in path.parents))


def _validate(args: argparse.Namespace) -> None:
    changed = _comparison(args.base)
    python_files = {
        path for path in changed if path.suffix == ".py" and (REPOSITORY_ROOT / path).is_file()
    }

    if (
        args.check == "all"
        and not args.list
        and any(path.name == "pyproject.toml" or path == Path("uv.lock") for path in changed)
    ):
        _run(["uv", "lock", "--check"])

    if args.check == "all" and not args.list and python_files:
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

    for path in changed:
        if _is_opt_in_e2e(path):
            continue
        owner = _owner(path)
        if owner is not None:
            if _is_owner_test(path, owner):
                if path.suffix == ".py":
                    typing_targets.add(path)
                if path.name.startswith("test_") and path in python_files:
                    test_targets.add(path)
                else:
                    test_targets.add(owner / "tests")
            elif path.suffix == ".py":
                production_owners.add(owner)
            continue
        if path.suffix == ".py" and path.parts and path.parts[0] in DIRECT_SCOPE_ROOTS:
            typing_targets.add(path)
            if path.name.startswith("test_") and path in python_files:
                test_targets.add(path)
            elif path.parts[0] == "tests" and len(path.parts) > 2:
                test_targets.add(path.parent)

    global_paths = {
        Path("pyproject.toml"),
        Path("uv.lock"),
        Path("conftest.py"),
        Path(".python-version"),
        Path("compose.test.yaml"),
        Path(".github/scripts/validate_changed_scope.py"),
        Path(".github/workflows/ci.yml"),
    }
    if any(
        path in global_paths
        or (path.parts[0] == "tests" and (len(path.parts) == 2 or path.parts[1] == "contracts"))
        or (path.name == "pyproject.toml" and not (REPOSITORY_ROOT / path).exists())
        for path in changed
    ):
        typing_targets.update(Path(name) for name in ("apps", "packages", "tests"))
        test_targets.update(Path(name) for name in ("apps", "packages", "tests"))

    for owner in production_owners:
        typing_targets.add(owner)
        tests = owner / "tests"
        if (REPOSITORY_ROOT / tests).is_dir():
            test_targets.add(tests)
    for dependent in _workspace_dependents(production_owners):
        tests = dependent / "tests"
        if (REPOSITORY_ROOT / tests).is_dir():
            test_targets.add(tests)
    if production_owners:
        test_targets.add(Path("tests"))

    typing = _existing(typing_targets)
    tests = _existing(test_targets)
    if args.list:
        print(json.dumps({"typing": typing, "tests": tests}, indent=2))
        return
    if typing and args.check in ("all", "types"):
        _run(["uv", "run", "--group", "dev", "basedpyright", *typing])

    if tests and args.check in ("all", "tests"):
        command = ["uv", "run", "--group", "dev", "pytest", "-x", "-q"]
        if args.junitxml:
            command.extend(["--junitxml", str(args.junitxml)])
        _run([*command, *tests])

    if not python_files and not typing_targets and not test_targets:
        print("No changed Python validation scope.", flush=True)


if __name__ == "__main__":
    _validate(_arguments())
