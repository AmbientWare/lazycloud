from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

from images.managed_runtime import ManagedRuntimeCatalogManifest
from tests.managed_runtime_fakes import managed_runtime_catalog_root
from worker.managed_runtime import (
    MANAGED_RUNTIME_CATALOG_DIGEST_ENV,
    MANAGED_RUNTIME_DIGEST_ENV,
)


def _artifact_digest(catalog: ManagedRuntimeCatalogManifest) -> str:
    machine = platform.machine().strip().lower()
    architecture = "amd64" if machine in {"amd64", "x86_64"} else "arm64"
    return catalog.artifacts["3.12"][architecture].digest


def test_launcher_rejects_incompatible_user_dependency(tmp_path: Path) -> None:
    root, catalog = managed_runtime_catalog_root(
        tmp_path,
        locked_distributions={"packaging": "25.0", "sample-dependency": "1.0"},
        requirements=["packaging>=24,<27", "sample-dependency>=1,<3"],
    )
    user_root = tmp_path / "user"
    _write_user_distribution(user_root, "sample-dependency", "4.0")

    result = _run_launcher(root, catalog.digest, _artifact_digest(catalog), user_root)

    assert result.returncode != 0
    assert "user dependency sample-dependency==4.0 does not satisfy" in result.stderr


def test_launcher_validates_selected_user_dependency_graph(tmp_path: Path) -> None:
    root, catalog = managed_runtime_catalog_root(
        tmp_path,
        locked_distributions={
            "packaging": "25.0",
            "sample-parent": "1.0",
            "sample-child": "1.0",
        },
        requirements=["packaging>=24,<27", "sample-parent>=1,<3"],
        locked_requirements={"sample-parent": ["sample-child==1.0"]},
    )
    user_root = tmp_path / "user"
    _write_user_distribution(user_root, "sample-parent", "2.0", ["sample-child==2.0"])
    _write_user_distribution(user_root, "sample-child", "1.0")

    result = _run_launcher(root, catalog.digest, _artifact_digest(catalog), user_root)

    assert result.returncode != 0
    assert "user dependency sample-child==1.0 does not satisfy sample-child==2.0" in result.stderr


def test_launcher_uses_locked_verifier_without_changing_user_precedence(tmp_path: Path) -> None:
    root, catalog = managed_runtime_catalog_root(
        tmp_path,
        probe_source="from packaging import ORIGIN\nprint(f'packaging:{ORIGIN}')\n",
    )
    user_root = tmp_path / "user"
    _write_user_distribution(user_root, "packaging", "25.0")
    (user_root / "packaging" / "__init__.py").write_text('ORIGIN = "user"\n', encoding="utf-8")

    result = _run_launcher(root, catalog.digest, _artifact_digest(catalog), user_root)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "packaging:user"


def test_launcher_places_managed_packages_before_user_package_shadows(
    tmp_path: Path,
) -> None:
    root, catalog = managed_runtime_catalog_root(
        tmp_path,
        probe_source=(
            "import foundation\n"
            "import runner\n"
            "import lazycloud\n"
            "import shared\n"
            "print(','.join((foundation.ORIGIN, runner.ORIGIN, lazycloud.ORIGIN, shared.ORIGIN)))\n"
        ),
    )
    user_root = tmp_path / "user"
    for module_name in ("foundation", "runner", "lazycloud", "shared"):
        module = user_root / module_name
        module.mkdir(parents=True, exist_ok=True)
        (module / "__init__.py").write_text('ORIGIN = "user"\n', encoding="utf-8")

    result = _run_launcher(
        root,
        catalog.digest,
        _artifact_digest(catalog),
        user_root,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "managed,managed,managed,managed"


def _run_launcher(
    root: Path,
    catalog_digest: str,
    artifact_digest: str,
    user_root: Path,
    *,
    disable_site_packages: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(user_root),
            MANAGED_RUNTIME_CATALOG_DIGEST_ENV: catalog_digest,
            MANAGED_RUNTIME_DIGEST_ENV: artifact_digest,
        }
    )
    command = [sys.executable]
    if disable_site_packages:
        command.append("-S")
    command.extend((str(root / "launcher.py"), "runner.probe"))
    return subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_user_distribution(
    root: Path,
    name: str,
    version: str,
    requirements: list[str] | None = None,
) -> None:
    module = root / name.replace("-", "_")
    module.mkdir(parents=True, exist_ok=True)
    (module / "__init__.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    metadata = root / f"{name.replace('-', '_')}-{version}.dist-info"
    metadata.mkdir()
    requires = "".join(f"Requires-Dist: {value}\n" for value in requirements or [])
    (metadata / "METADATA").write_text(
        f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n{requires}",
        encoding="utf-8",
    )
