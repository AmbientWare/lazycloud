from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import runpy
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, TypeAlias, TypeGuard

CATALOG_DIGEST_ENV = "LAZYCLOUD_MANAGED_RUNTIME_CATALOG_DIGEST"
ARTIFACT_DIGEST_ENV = "LAZYCLOUD_MANAGED_RUNTIME_DIGEST"
CATALOG_FILE = "catalog.json"
SUPPORTED_PYTHON_VERSIONS = ("3.10", "3.11", "3.12")
JsonScalar: TypeAlias = bool | int | float | str | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class DistributionRecord:
    name: str
    version: str
    requirements: tuple[str, ...]


def main() -> None:
    verify_only = len(sys.argv) == 2 and sys.argv[1] == "--verify"
    if not verify_only and (len(sys.argv) < 2 or not sys.argv[1].startswith("runner.")):
        _fail("a canonical runner module is required")
    root = Path(__file__).resolve().parent
    catalog = _load_json(root / CATALOG_FILE, "catalog")
    expected_catalog_digest = os.environ.get(CATALOG_DIGEST_ENV, "")
    actual_catalog_digest = _json_digest_without_key(catalog, "digest")
    if not expected_catalog_digest or catalog.get("digest") != expected_catalog_digest:
        _fail("catalog digest does not match the worker startup contract")
    if actual_catalog_digest != expected_catalog_digest:
        _fail("catalog content digest verification failed")

    python_version = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if python_version not in SUPPORTED_PYTHON_VERSIONS:
        _fail(
            "Python {} is unsupported; supported managed runtimes are {}".format(
                python_version,
                ", ".join(SUPPORTED_PYTHON_VERSIONS),
            )
        )
    architecture = _linux_architecture(platform.machine())
    artifacts = _object(catalog.get("artifacts"), "catalog artifacts")
    version_artifacts = _object(
        artifacts.get(python_version),
        f"Python {python_version} artifacts",
    )
    artifact = _object(
        version_artifacts.get(architecture),
        f"Python {python_version} {architecture} artifact",
    )
    relative_path = artifact.get("relative_path")
    digest = artifact.get("digest")
    artifact_architecture = artifact.get("architecture")
    if not isinstance(relative_path, str) or not isinstance(digest, str):
        _fail(f"Python {python_version} artifact metadata is invalid")
    if artifact_architecture != architecture:
        _fail(f"Python {python_version} artifact architecture does not match {architecture}")
    artifact_root = (root / relative_path).resolve()
    if root not in artifact_root.parents:
        _fail(f"Python {python_version} artifact path escapes the catalog")

    expected_artifact_digest = os.environ.get(ARTIFACT_DIGEST_ENV, "")
    if not expected_artifact_digest or expected_artifact_digest != digest:
        _fail("selected artifact digest does not match the worker startup contract")

    managed_path = artifact_root / "managed"
    dependency_path = artifact_root / "dependencies"
    managed_records = _installed_distribution_records([str(managed_path)])
    managed_distributions = _distribution_versions(managed_records)
    if managed_distributions != _normalized_versions(artifact.get("managed_distributions")):
        _fail(f"Python {python_version} managed distribution metadata verification failed")
    locked_distributions = _normalized_versions(artifact.get("locked_distributions"))

    original_path = list(sys.path)
    previous_packaging_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "packaging" or name.startswith("packaging.")
    }
    for name in previous_packaging_modules:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(dependency_path))
    try:
        from packaging.requirements import Requirement
        from packaging.utils import canonicalize_name
    except ImportError:
        _fail("the managed runtime dependency verifier is unavailable")
    finally:
        sys.path[:] = original_path
        for name in tuple(sys.modules):
            if name == "packaging" or name.startswith("packaging."):
                sys.modules.pop(name, None)
        sys.modules.update(previous_packaging_modules)

    pending = [
        Requirement(raw_requirement)
        for distribution in managed_records.values()
        for raw_requirement in distribution.requirements
    ]
    checked: set[tuple[str, str, str]] = set()
    expanded: set[str] = set()
    resolved_distributions: dict[str, str] = {}
    user_records: dict[str, DistributionRecord | None] = {}
    locked_records: dict[str, DistributionRecord] | None = None
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = canonicalize_name(requirement.name)
        selected = managed_records.get(name)
        source = "managed"
        if selected is None:
            if name not in user_records:
                user_records[name] = _installed_distribution_record(name)
            selected = user_records[name]
            source = "user"
        if selected is None:
            if locked_records is None:
                locked_records = _installed_distribution_records([str(dependency_path)])
            selected = locked_records.get(name)
            source = "locked fallback"
        if selected is None:
            _fail(f"required dependency {name} is missing")
        if source == "locked fallback" and locked_distributions.get(name) != selected.version:
            _fail(f"locked fallback dependency {name} metadata verification failed")
        check_key = (name, selected.version, str(requirement.specifier))
        if check_key in checked:
            continue
        checked.add(check_key)
        resolved_distributions[name] = selected.version
        if requirement.specifier and selected.version not in requirement.specifier:
            _fail(f"{source} dependency {name}=={selected.version} does not satisfy {requirement}")
        if name not in expanded:
            expanded.add(name)
            pending.extend(Requirement(raw) for raw in selected.requirements)

    os.environ[ARTIFACT_DIGEST_ENV] = digest
    sys.path.append(str(dependency_path))
    sys.path.insert(0, str(managed_path))
    if verify_only:
        print(
            json.dumps(
                {
                    "artifact_digest": digest,
                    "catalog_digest": expected_catalog_digest,
                    "python": python_version,
                    "architecture": architecture,
                    "resolved_distributions": dict(sorted(resolved_distributions.items())),
                },
                sort_keys=True,
            )
        )
        return
    module = sys.argv[1]
    sys.argv = [module, *sys.argv[2:]]
    runpy.run_module(module, run_name="__main__", alter_sys=True)


def _load_json(path: Path, label: str) -> dict[str, JsonValue]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"managed runtime {label} is unreadable: {exc}")
    return _object(_json_value(value, label), label)


def _json_digest_without_key(payload: Mapping[str, JsonValue], key: str) -> str:
    content = dict(payload)
    content.pop(key, None)
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _installed_distribution_records(
    path: Sequence[str] | None = None,
) -> dict[str, DistributionRecord]:
    distributions = (
        importlib.metadata.distributions(path=list(path))
        if path is not None
        else importlib.metadata.distributions()
    )
    installed: dict[str, DistributionRecord] = {}
    for distribution in distributions:
        record = _distribution_record(distribution)
        if record is not None:
            installed.setdefault(record.name, record)
    return installed


def _installed_distribution_record(name: str) -> DistributionRecord | None:
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    record = _distribution_record(distribution)
    if record is None or record.name != name:
        return None
    return record


def _distribution_record(
    distribution: importlib.metadata.Distribution,
) -> DistributionRecord | None:
    metadata = distribution.metadata
    name = metadata["Name"]
    if not name:
        return None
    requirements = metadata.get_all("Requires-Dist")
    if requirements is None:
        requirements = distribution.requires or []
    return DistributionRecord(
        name=_canonical_name(name),
        version=metadata["Version"],
        requirements=tuple(requirements),
    )


def _distribution_versions(
    records: Mapping[str, DistributionRecord],
) -> dict[str, str]:
    return {name: distribution.version for name, distribution in records.items()}


def _normalized_versions(value: JsonValue) -> dict[str, str]:
    versions = _object(value, "managed runtime distribution metadata")
    return {
        _canonical_name(name): version
        for name, version in versions.items()
        if isinstance(version, str)
    }


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _linux_architecture(machine: str) -> str:
    normalized = machine.strip().lower()
    if normalized in {"amd64", "x86_64"}:
        return "amd64"
    if normalized in {"arm64", "aarch64"}:
        return "arm64"
    _fail(
        f"managed runtime startup supports only Linux amd64 and arm64; got {machine or '<empty>'}"
    )


def _json_value(value: object, label: str) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if _is_object_list(value):
        return [_json_value(item, label) for item in value]
    if _is_object_mapping(value):
        document: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(f"managed runtime {label} contains a non-string object key")
            document[key] = _json_value(item, label)
        return document
    _fail(f"managed runtime {label} contains an unsupported JSON value")


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_object_mapping(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _object(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return value
    _fail(f"{label} is not an object")


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"managed runtime startup failed: {message}")


if __name__ == "__main__":
    main()
