from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

from images.managed_runtime import (
    MANAGED_RUNTIME_ARCHITECTURES,
    MANAGED_RUNTIME_DISTRIBUTIONS,
    MANAGED_RUNTIME_PYTHON_VERSIONS,
    MANAGED_RUNTIME_SCHEMA_VERSION,
    ManagedRuntimeCatalogManifest,
    ManagedRuntimeManifest,
    managed_runtime_catalog_digest,
)
from shared.managed_runtime_integrity import (
    managed_package_source_digest,
    managed_runtime_artifact_digest,
)

_MANAGED_PACKAGE_MODULES = ("foundation", "runner", "lazycloud", "shared")


def managed_runtime_catalog_root(
    tmp_path: Path,
    *,
    managed_distributions: dict[str, str] | None = None,
    locked_distributions: dict[str, str] | None = None,
    requirements: list[str] | None = None,
    locked_requirements: dict[str, list[str]] | None = None,
    probe_source: str | None = None,
) -> tuple[Path, ManagedRuntimeCatalogManifest]:
    root = tmp_path / "artifacts"
    root.mkdir()
    source_digest = managed_package_source_digest()
    managed_versions = managed_distributions or {
        name: "0.1.0" for name in MANAGED_RUNTIME_DISTRIBUTIONS
    }
    locked_versions = locked_distributions or {"packaging": "25.0"}
    dependency_requirements = requirements or ["packaging>=24,<27"]
    catalog_requirements = sorted(
        {
            *dependency_requirements,
            *(
                requirement
                for values in (locked_requirements or {}).values()
                for requirement in values
            ),
        }
    )
    artifacts: dict[str, dict[str, ManagedRuntimeManifest]] = {}
    for python_version in MANAGED_RUNTIME_PYTHON_VERSIONS:
        version_artifacts: dict[str, ManagedRuntimeManifest] = {}
        for architecture in MANAGED_RUNTIME_ARCHITECTURES:
            staging = tmp_path / f"staging-{python_version}-{architecture.value}"
            managed = staging / "managed"
            dependencies = staging / "dependencies"
            (managed / "runner").mkdir(parents=True)
            (managed / "runner" / "__init__.py").write_text("", encoding="utf-8")
            (managed / "runner" / "probe.py").write_text(
                probe_source
                or (
                    "import sys\n"
                    "print(f'probe:{sys.version_info.major}.{sys.version_info.minor}')\n"
                ),
                encoding="utf-8",
            )
            for module_name in _MANAGED_PACKAGE_MODULES:
                module = managed / module_name
                module.mkdir(exist_ok=True)
                (module / "__init__.py").write_text(
                    'ORIGIN = "managed"\n',
                    encoding="utf-8",
                )
            for distribution_name, version in managed_versions.items():
                _write_distribution(
                    managed,
                    distribution_name,
                    version,
                    dependency_requirements,
                )
            for distribution_name, version in locked_versions.items():
                _write_distribution(
                    dependencies,
                    distribution_name,
                    version,
                    (locked_requirements or {}).get(distribution_name, []),
                    write_module=True,
                )
            (staging / "python-version").write_text(
                python_version + "\n",
                encoding="utf-8",
            )
            (staging / "architecture").write_text(
                architecture.value + "\n",
                encoding="utf-8",
            )
            digest = managed_runtime_artifact_digest(staging)
            destination = root / "content" / digest
            destination.parent.mkdir(exist_ok=True)
            staging.rename(destination)
            version_artifacts[architecture.value] = ManagedRuntimeManifest(
                python_major_minor=python_version,
                architecture=architecture,
                digest=digest,
                relative_path=f"content/{digest}",
                source_digest=source_digest,
                lock_digest=hashlib.sha256(
                    f"{python_version}:{architecture.value}".encode()
                ).hexdigest(),
                managed_distributions={
                    _canonical_name(name): version for name, version in managed_versions.items()
                },
                locked_distributions={
                    _canonical_name(name): version for name, version in locked_versions.items()
                },
                requirements=catalog_requirements,
            )
        artifacts[python_version] = version_artifacts
    launcher_source = (
        Path(__file__).resolve().parents[1]
        / "packages/runner/src/runner/managed_runtime_launcher.py"
    )
    launcher = root / "launcher.py"
    shutil.copy2(launcher_source, launcher)
    manifest = ManagedRuntimeCatalogManifest(
        schema_version=MANAGED_RUNTIME_SCHEMA_VERSION,
        digest="",
        source_digest=source_digest,
        launcher_digest=hashlib.sha256(launcher.read_bytes()).hexdigest(),
        artifacts=artifacts,
    )
    manifest.digest = managed_runtime_catalog_digest(manifest)
    (root / "catalog.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return root, manifest


def _write_distribution(
    root: Path,
    distribution_name: str,
    version: str,
    requirements: list[str],
    *,
    write_module: bool = False,
) -> None:
    metadata = root / f"{distribution_name.replace('-', '_')}-{version}.dist-info"
    metadata.mkdir(parents=True, exist_ok=True)
    requires = "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements)
    (metadata / "METADATA").write_text(
        f"Metadata-Version: 2.3\nName: {distribution_name}\nVersion: {version}\n{requires}",
        encoding="utf-8",
    )
    if write_module:
        module = root / distribution_name.replace("-", "_")
        if distribution_name == "packaging":
            packaging_spec = importlib.util.find_spec("packaging")
            if packaging_spec is None or packaging_spec.origin is None:
                raise RuntimeError("test packaging dependency is unavailable")
            shutil.copytree(Path(packaging_spec.origin).parent, module)
        else:
            module.mkdir(exist_ok=True)
            (module / "__init__.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")


def _canonical_name(value: str) -> str:
    return value.lower().replace("_", "-").replace(".", "-")


__all__ = ["managed_runtime_catalog_root"]
