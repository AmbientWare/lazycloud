"""Decide how the installed client upgrades itself.

The distribution is ``lazycloud-client``; the import and command stay
``lazycloud``. The installer that put it here is the one that can replace it,
so the upgrade command follows the installation rather than a fixed tool.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from pydantic import BaseModel, ValidationError
from shared.enums import StringEnum

DISTRIBUTION = "lazycloud-client"
PYPI_RELEASE_URL = f"https://pypi.org/pypi/{DISTRIBUTION}/json"


class InstallerKind(StringEnum):
    UV_TOOL = "uv tool"
    PIPX = "pipx"
    UV = "uv pip"
    PIP = "pip"


@dataclass(frozen=True, slots=True)
class Installation:
    kind: InstallerKind
    command: tuple[str, ...]


class SelfUpdateError(RuntimeError):
    pass


class _PypiInfo(BaseModel):
    version: str


class _PypiDocument(BaseModel):
    info: _PypiInfo


def release_is_newer(candidate: str, installed: str) -> bool:
    """Compare release versions; an unparsable version counts as different, not newer."""
    try:
        return _release_tuple(candidate) > _release_tuple(installed)
    except ValueError:
        return False


def _release_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.strip().split("."))


def detect_installation(
    *,
    prefix: Path,
    executable: Path,
    installer: str,
    environ: Mapping[str, str],
    home: Path,
) -> Installation:
    """Name the installer that owns ``prefix`` and the command that upgrades it."""
    resolved_prefix = prefix.resolve()
    if _within(resolved_prefix, _uv_tool_dir(environ, home)):
        return Installation(InstallerKind.UV_TOOL, ("uv", "tool", "upgrade", DISTRIBUTION))
    if any(_within(resolved_prefix, root) for root in _pipx_venv_dirs(environ, home)):
        return Installation(InstallerKind.PIPX, ("pipx", "upgrade", DISTRIBUTION))
    if installer.strip().lower() == "uv":
        return Installation(
            InstallerKind.UV,
            ("uv", "pip", "install", "--upgrade", "--python", str(executable), DISTRIBUTION),
        )
    return Installation(
        InstallerKind.PIP,
        (str(executable), "-m", "pip", "install", "--upgrade", DISTRIBUTION),
    )


def current_installation() -> Installation:
    try:
        distribution = metadata.distribution(DISTRIBUTION)
    except metadata.PackageNotFoundError as exc:
        msg = f"{DISTRIBUTION} is not installed as a package; update the checkout instead"
        raise SelfUpdateError(msg) from exc
    return detect_installation(
        prefix=Path(sys.prefix),
        executable=Path(sys.executable),
        installer=distribution.read_text("INSTALLER") or "",
        environ=os.environ,
        home=Path.home(),
    )


def installed_version() -> str:
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError as exc:
        msg = f"{DISTRIBUTION} is not installed as a package; update the checkout instead"
        raise SelfUpdateError(msg) from exc


def latest_version(*, timeout_seconds: float = 10.0) -> str:
    request = urllib.request.Request(PYPI_RELEASE_URL, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        msg = f"could not read the latest {DISTRIBUTION} release from PyPI: {exc}"
        raise SelfUpdateError(msg) from exc
    try:
        document = _PypiDocument.model_validate_json(raw)
    except ValidationError as exc:
        msg = f"PyPI returned no version for {DISTRIBUTION}"
        raise SelfUpdateError(msg) from exc
    return document.info.version


def _within(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _uv_tool_dir(environ: Mapping[str, str], home: Path) -> Path:
    configured = environ.get("UV_TOOL_DIR")
    if configured:
        return Path(configured)
    data_home = environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else home / ".local" / "share"
    return base / "uv" / "tools"


def _pipx_venv_dirs(environ: Mapping[str, str], home: Path) -> tuple[Path, ...]:
    configured = environ.get("PIPX_HOME")
    if configured:
        return (Path(configured) / "venvs",)
    data_home = environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else home / ".local" / "share"
    return (base / "pipx" / "venvs", home / ".local" / "pipx" / "venvs")
