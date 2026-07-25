from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import py_compile
import shutil
import sys
from pathlib import Path
from typing import TypeAlias, TypeGuard

from shared.managed_runtime_integrity import (
    managed_package_source_digest,
    managed_runtime_artifact_digest,
)

MANAGED_DISTRIBUTIONS = ("foundation", "runner", "lazycloud", "lazycloud-shared")
SUPPORTED_PYTHON_VERSIONS = ("3.10", "3.11", "3.12")
SUPPORTED_ARCHITECTURES = ("amd64", "arm64")
SCHEMA_VERSION = 3
JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--packages-root", type=Path, required=True)
    build.add_argument("--managed-root", type=Path, required=True)
    build.add_argument("--dependency-root", type=Path, required=True)
    build.add_argument("--requirements", type=Path, action="append", required=True)
    build.add_argument("--architecture", choices=SUPPORTED_ARCHITECTURES, required=True)
    build.add_argument("--output-root", type=Path, required=True)

    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--version-root", type=Path, action="append", required=True)
    assemble.add_argument("--launcher", type=Path, required=True)
    assemble.add_argument("--output-root", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "build":
        build_artifact(
            packages_root=args.packages_root,
            managed_root=args.managed_root,
            dependency_root=args.dependency_root,
            requirements_files=args.requirements,
            architecture=args.architecture,
            output_root=args.output_root,
        )
        return
    assemble_catalog(
        version_roots=args.version_root,
        launcher=args.launcher,
        output_root=args.output_root,
    )


def build_artifact(
    *,
    packages_root: Path,
    managed_root: Path,
    dependency_root: Path,
    requirements_files: list[Path],
    architecture: str,
    output_root: Path,
) -> None:
    python_version = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if python_version not in SUPPORTED_PYTHON_VERSIONS:
        raise RuntimeError(
            "unsupported managed runtime Python {}; expected {}".format(
                python_version,
                ", ".join(SUPPORTED_PYTHON_VERSIONS),
            )
        )
    packages_root = packages_root.resolve()
    managed_root = managed_root.resolve()
    dependency_root = dependency_root.resolve()
    source_digest = managed_package_source_digest(packages_root)
    if len(source_digest) != 64:
        raise RuntimeError("managed package source digest is unavailable")
    managed_distributions = _installed_distributions(managed_root)
    missing = sorted(set(MANAGED_DISTRIBUTIONS) - set(managed_distributions))
    if missing:
        raise RuntimeError("managed distributions are missing: {}".format(", ".join(missing)))
    locked_distributions = _installed_distributions(dependency_root)
    if not locked_distributions:
        raise RuntimeError("locked managed runtime dependencies are missing")
    requirements = _distribution_requirements(managed_root, dependency_root)
    if not requirements:
        raise RuntimeError("managed runtime dependency contract is empty")
    lock_digest = _requirements_digest(requirements_files)
    _compile_python_sources(managed_root, dependency_root)

    staging = output_root.resolve() / "artifact-staging"
    if staging.exists():
        raise RuntimeError(f"managed runtime artifact staging already exists: {staging}")
    staging.mkdir(parents=True)
    shutil.move(str(managed_root), str(staging / "managed"))
    shutil.move(str(dependency_root), str(staging / "dependencies"))
    digest = managed_runtime_artifact_digest(staging)
    destination = output_root.resolve() / "artifacts" / digest
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(destination)
    entry = _json_object(
        {
            "python_major_minor": python_version,
            "architecture": architecture,
            "digest": digest,
            "relative_path": f"artifacts/{digest}",
            "source_digest": source_digest,
            "lock_digest": lock_digest,
            "managed_distributions": managed_distributions,
            "locked_distributions": locked_distributions,
            "requirements": requirements,
        },
        "managed runtime entry",
    )
    (output_root.resolve() / "entry.json").write_text(
        json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def assemble_catalog(
    *,
    version_roots: list[Path],
    launcher: Path,
    output_root: Path,
) -> None:
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, dict[str, dict[str, JsonValue]]] = {}
    source_digests: set[str] = set()
    for version_root in version_roots:
        version_root = version_root.resolve()
        entry = _load_object(version_root / "entry.json")
        version = entry.get("python_major_minor")
        architecture = entry.get("architecture")
        digest = entry.get("digest")
        if (
            not isinstance(version, str)
            or not isinstance(architecture, str)
            or not isinstance(digest, str)
        ):
            raise RuntimeError(f"managed runtime entry is incomplete: {version_root}")
        version_artifacts = artifacts.setdefault(version, {})
        if architecture in version_artifacts:
            raise RuntimeError(
                f"duplicate managed runtime entry for Python {version} {architecture}"
            )
        source_digest = entry.get("source_digest")
        if not isinstance(source_digest, str):
            raise RuntimeError(f"managed runtime entry has no source digest: {version_root}")
        source_digests.add(source_digest)
        source = version_root / "artifacts" / digest
        destination = output_root / "artifacts" / digest
        if destination.exists():
            if managed_runtime_artifact_digest(destination) != digest:
                raise RuntimeError(f"colliding managed runtime artifact: {digest}")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination)
        version_artifacts[architecture] = entry
    if set(artifacts) != set(SUPPORTED_PYTHON_VERSIONS):
        raise RuntimeError(
            "managed runtime versions are incomplete: expected {}, got {}".format(
                ", ".join(SUPPORTED_PYTHON_VERSIONS),
                ", ".join(sorted(artifacts)),
            )
        )
    for version, version_artifacts in artifacts.items():
        if set(version_artifacts) != set(SUPPORTED_ARCHITECTURES):
            raise RuntimeError(
                "managed runtime architectures for Python {} are incomplete: "
                "expected {}, got {}".format(
                    version,
                    ", ".join(SUPPORTED_ARCHITECTURES),
                    ", ".join(sorted(version_artifacts)),
                )
            )
    if len(source_digests) != 1:
        raise RuntimeError("managed runtime artifacts were built from different sources")
    launcher_destination = output_root / "launcher.py"
    shutil.copy2(launcher.resolve(), launcher_destination)
    catalog: dict[str, JsonValue] = {
        "schema_version": SCHEMA_VERSION,
        "source_digest": next(iter(source_digests)),
        "launcher_digest": _file_digest(launcher_destination),
        "artifacts": {
            version: dict(sorted(version_artifacts.items()))
            for version, version_artifacts in sorted(artifacts.items())
        },
    }
    catalog["digest"] = _json_digest(catalog)
    (output_root / "catalog.json").write_text(
        json.dumps(catalog, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _installed_distributions(root: Path) -> dict[str, str]:
    return dict(
        sorted(
            (
                _canonical_name(name),
                distribution.version,
            )
            for distribution in importlib.metadata.distributions(path=[str(root)])
            if (name := distribution.metadata["Name"])
        )
    )


def _distribution_requirements(*roots: Path) -> list[str]:
    requirements: set[str] = set()
    for root in roots:
        for distribution in importlib.metadata.distributions(path=[str(root)]):
            requirements.update(distribution.requires or [])
    return sorted(requirements)


def _requirements_digest(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(path.resolve() for path in paths):
        if not path.is_file():
            raise RuntimeError(f"locked requirements are unavailable: {path}")
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _compile_python_sources(*roots: Path) -> None:
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            py_compile.compile(
                str(path),
                dfile=path.relative_to(root).as_posix(),
                doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
            )


def _canonical_name(value: str) -> str:
    return value.lower().replace("_", "-").replace(".", "-")


def _load_object(path: Path) -> dict[str, JsonValue]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"JSON object expected: {path}") from exc
    return _json_object(value, str(path))


def _json_digest(payload: dict[str, JsonValue]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_object(value: object, label: str) -> dict[str, JsonValue]:
    document = _json_value(value, label)
    if isinstance(document, dict):
        return document
    raise RuntimeError(f"JSON object expected: {label}")


def _json_value(value: object, label: str) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if _is_object_list(value):
        return [_json_value(item, label) for item in value]
    if _is_object_mapping(value):
        document: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RuntimeError(f"JSON object contains a non-string key: {label}")
            document[key] = _json_value(item, label)
        return document
    raise RuntimeError(f"unsupported JSON value: {label}")


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_object_mapping(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


if __name__ == "__main__":
    main()
