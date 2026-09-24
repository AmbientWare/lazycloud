from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import distributions
from pathlib import Path, PurePosixPath

from pydantic import Field
from shared.contracts import ContractModel
from shared.image_building.authoring import LinuxArchitecture, PythonVersion
from shared.managed_runtime_integrity import managed_package_source_digest

MANAGED_RUNTIME_SCHEMA_VERSION = 3
MANAGED_RUNTIME_PYTHON_VERSIONS = tuple(version.value for version in PythonVersion)
MANAGED_RUNTIME_ARCHITECTURES = (
    LinuxArchitecture.Amd64,
    LinuxArchitecture.Arm64,
)
MANAGED_RUNTIME_DISTRIBUTIONS = ("foundation", "runner", "lazycloud-client", "lazycloud-shared")
MANAGED_RUNTIME_CATALOG_FILE = "catalog.json"
MANAGED_RUNTIME_LAUNCHER_FILE = "launcher.py"


class ManagedRuntimeManifest(ContractModel):
    python_major_minor: str
    architecture: LinuxArchitecture
    digest: str
    relative_path: str
    source_digest: str
    lock_digest: str
    managed_distributions: dict[str, str] = Field(default_factory=dict)
    locked_distributions: dict[str, str] = Field(default_factory=dict)
    requirements: list[str] = Field(default_factory=list)


class ManagedRuntimeCatalogManifest(ContractModel):
    schema_version: int
    digest: str
    source_digest: str
    launcher_digest: str
    artifacts: dict[str, dict[str, ManagedRuntimeManifest]] = Field(default_factory=dict)


class ManagedRuntimeCatalog(ContractModel):
    digest: str
    host_path: str
    selected_python: str
    selected_architecture: LinuxArchitecture
    selected_artifact: ManagedRuntimeManifest


def load_managed_runtime_catalog(
    root: Path,
    python_major_minor: str,
    architecture: LinuxArchitecture,
) -> ManagedRuntimeCatalog:
    artifact_root = root.resolve()
    manifest = _load_manifest(artifact_root)
    if python_major_minor not in MANAGED_RUNTIME_PYTHON_VERSIONS:
        raise RuntimeError(
            f"managed runtime Python {python_major_minor} is unsupported; supported versions are "
            f"{', '.join(MANAGED_RUNTIME_PYTHON_VERSIONS)}"
        )
    return _selected_catalog(artifact_root, manifest, python_major_minor, architecture)


def load_managed_runtime_catalogs(
    root: Path,
    architecture: LinuxArchitecture,
) -> dict[str, ManagedRuntimeCatalog]:
    """Every supported Python's runtime for one architecture, from one read of the catalog.

    The versions are checked concurrently: each reads its own distribution
    metadata, and on a machine just started from a stopped disk those reads
    wait on the volume rather than on this process.
    """
    artifact_root = root.resolve()
    manifest = _load_manifest(artifact_root)
    with ThreadPoolExecutor(
        max_workers=len(MANAGED_RUNTIME_PYTHON_VERSIONS),
        thread_name_prefix="managed-runtime",
    ) as pool:
        loads = {
            version: pool.submit(_selected_catalog, artifact_root, manifest, version, architecture)
            for version in MANAGED_RUNTIME_PYTHON_VERSIONS
        }
        return {version: load.result() for version, load in loads.items()}


def _load_manifest(artifact_root: Path) -> ManagedRuntimeCatalogManifest:
    catalog_path = artifact_root / MANAGED_RUNTIME_CATALOG_FILE
    if not catalog_path.is_file():
        raise RuntimeError(f"managed runtime catalog is unavailable: {catalog_path}")
    try:
        manifest = ManagedRuntimeCatalogManifest.model_validate_json(
            catalog_path.read_text(encoding="utf-8")
        )
    except ValueError as exc:
        raise RuntimeError(f"managed runtime catalog is invalid: {catalog_path}") from exc
    if manifest.schema_version != MANAGED_RUNTIME_SCHEMA_VERSION:
        raise RuntimeError(
            "managed runtime catalog schema mismatch: "
            f"expected {MANAGED_RUNTIME_SCHEMA_VERSION}, got {manifest.schema_version}"
        )
    expected_versions = set(MANAGED_RUNTIME_PYTHON_VERSIONS)
    available_versions = set(manifest.artifacts)
    if available_versions != expected_versions:
        details = [
            *(f"missing {version}" for version in sorted(expected_versions - available_versions)),
            *(
                f"unexpected {version}"
                for version in sorted(available_versions - expected_versions)
            ),
        ]
        raise RuntimeError(f"managed runtime catalog versions are invalid: {', '.join(details)}")
    expected_architectures = {item.value for item in MANAGED_RUNTIME_ARCHITECTURES}
    for version, version_artifacts in manifest.artifacts.items():
        available_architectures = set(version_artifacts)
        if available_architectures != expected_architectures:
            details = [
                *(
                    f"missing {item}"
                    for item in sorted(expected_architectures - available_architectures)
                ),
                *(
                    f"unexpected {item}"
                    for item in sorted(available_architectures - expected_architectures)
                ),
            ]
            raise RuntimeError(
                f"managed runtime catalog architectures for Python {version} are invalid: "
                f"{', '.join(details)}"
            )
    source_digest = managed_package_source_digest()
    if len(source_digest) != 64 or manifest.source_digest != source_digest:
        raise RuntimeError(
            "managed runtime source digest mismatch: "
            f"expected {source_digest or 'unavailable'}, got {manifest.source_digest}"
        )
    launcher_digest = _file_digest(artifact_root / MANAGED_RUNTIME_LAUNCHER_FILE)
    if launcher_digest != manifest.launcher_digest:
        raise RuntimeError(
            "managed runtime launcher digest mismatch: "
            f"expected {manifest.launcher_digest}, got {launcher_digest or 'unavailable'}"
        )
    if manifest.digest != managed_runtime_catalog_digest(manifest):
        raise RuntimeError("managed runtime catalog digest mismatch")
    return manifest


def _selected_catalog(
    artifact_root: Path,
    manifest: ManagedRuntimeCatalogManifest,
    python_major_minor: str,
    architecture: LinuxArchitecture,
) -> ManagedRuntimeCatalog:
    selected_artifact = manifest.artifacts[python_major_minor][architecture.value]
    _validate_artifact(
        artifact_root,
        python_major_minor,
        architecture,
        selected_artifact,
        manifest.source_digest,
    )
    return ManagedRuntimeCatalog(
        digest=manifest.digest,
        host_path=str(artifact_root),
        selected_python=python_major_minor,
        selected_architecture=architecture,
        selected_artifact=selected_artifact,
    )


def managed_runtime_catalog_digest(manifest: ManagedRuntimeCatalogManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"digest"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_artifact(
    root: Path,
    version: str,
    architecture: LinuxArchitecture,
    artifact: ManagedRuntimeManifest,
    source_digest: str,
) -> None:
    if artifact.python_major_minor != version:
        raise RuntimeError(
            f"managed runtime artifact version mismatch: expected {version}, "
            f"got {artifact.python_major_minor}"
        )
    if artifact.architecture is not architecture:
        raise RuntimeError(
            f"managed runtime artifact architecture mismatch: expected {architecture.value}, "
            f"got {artifact.architecture.value}"
        )
    if artifact.source_digest != source_digest:
        raise RuntimeError(f"managed runtime artifact {version} has a stale source digest")
    if len(artifact.digest) != 64 or len(artifact.lock_digest) != 64:
        raise RuntimeError(f"managed runtime artifact {version} has an invalid digest")
    relative_path = PurePosixPath(artifact.relative_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise RuntimeError(f"managed runtime artifact {version} has an unsafe path")
    artifact_path = root.joinpath(*relative_path.parts)
    # The artifact's content digest names the directory it was built into, and
    # the build computed it from these bytes. The tree reaches this machine inside
    # the worker image, whose layers the container engine verifies against their
    # own digests, so hashing it again here re-proves the pull. It read about
    # 360 MB and 18,000 files on every start, 16 s from a disk resumed cold.
    if artifact_path.name != artifact.digest or not artifact_path.is_dir():
        raise RuntimeError(f"managed runtime artifact {version} is missing: {artifact_path}")
    managed = _installed_distributions(artifact_path / "managed")
    locked = _installed_distributions(artifact_path / "dependencies")
    missing = sorted(set(MANAGED_RUNTIME_DISTRIBUTIONS) - managed.keys())
    if missing:
        raise RuntimeError(
            f"managed runtime artifact {version} distributions are missing: {', '.join(missing)}"
        )
    if managed != _normalized_distributions(artifact.managed_distributions):
        raise RuntimeError(
            f"managed runtime artifact {version} managed distribution metadata mismatch"
        )
    if locked != _normalized_distributions(artifact.locked_distributions):
        raise RuntimeError(
            f"managed runtime artifact {version} locked distribution metadata mismatch"
        )
    if not artifact.requirements:
        raise RuntimeError(f"managed runtime artifact {version} has no dependency contract")


def _normalized_distributions(values: dict[str, str]) -> dict[str, str]:
    return {_canonical_name(name): version for name, version in values.items() if name.strip()}


def _installed_distributions(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    return {
        _canonical_name(name): distribution.version
        for distribution in distributions(path=[str(root)])
        if (name := distribution.metadata["Name"])
    }


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip()).lower()


def _file_digest(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "MANAGED_RUNTIME_ARCHITECTURES",
    "MANAGED_RUNTIME_CATALOG_FILE",
    "MANAGED_RUNTIME_DISTRIBUTIONS",
    "MANAGED_RUNTIME_LAUNCHER_FILE",
    "MANAGED_RUNTIME_PYTHON_VERSIONS",
    "MANAGED_RUNTIME_SCHEMA_VERSION",
    "ManagedRuntimeCatalog",
    "ManagedRuntimeCatalogManifest",
    "ManagedRuntimeManifest",
    "load_managed_runtime_catalog",
    "load_managed_runtime_catalogs",
    "managed_runtime_catalog_digest",
]
