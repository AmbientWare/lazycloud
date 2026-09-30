from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import tomli
import yaml
from poetry.core.constraints.version import (
    Version,
    VersionConstraint,
    VersionRange,
    parse_constraint,
)
from pydantic import BaseModel, ConfigDict, Field
from shared.image_building.authoring import (
    ImageBuildStep,
    ImageBuildStepKind,
    ImageSpec,
    PythonVersion,
)
from shared.image_building.python import normalize_python_version

from lazycloud.source_sync import collect_source_files


class _Project(BaseModel):
    requires_python: str = Field(default="", alias="requires-python")


class _PoetryDependencySpec(BaseModel):
    version: str = ""
    path: str | None = None


_PoetryDependency = str | _PoetryDependencySpec | list[str | _PoetryDependencySpec]


class _PoetryGroup(BaseModel):
    dependencies: dict[str, _PoetryDependency] = Field(default_factory=dict)


class _Poetry(BaseModel):
    dependencies: dict[str, _PoetryDependency] = Field(default_factory=dict)
    group: dict[str, _PoetryGroup] = Field(default_factory=dict)

    def python_requirement(self) -> str:
        python = self.dependencies.get("python", "")
        if isinstance(python, str):
            return python
        if isinstance(python, _PoetryDependencySpec):
            return python.version
        return ""

    def local_paths(self) -> list[str]:
        dependencies = [
            *self.dependencies.values(),
            *(spec for group in self.group.values() for spec in group.dependencies.values()),
        ]
        paths: list[str] = []
        for dependency in dependencies:
            specs = dependency if isinstance(dependency, list) else [dependency]
            paths.extend(
                spec.path
                for spec in specs
                if isinstance(spec, _PoetryDependencySpec) and spec.path is not None
            )
        return paths


class _UvSource(BaseModel):
    path: str | None = None


class _UvWorkspace(BaseModel):
    members: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class _Uv(BaseModel):
    workspace: _UvWorkspace | None = None
    sources: dict[str, _UvSource | list[_UvSource]] = Field(default_factory=dict)

    def local_paths(self) -> list[str]:
        paths: list[str] = []
        for source in self.sources.values():
            entries = source if isinstance(source, list) else [source]
            paths.extend(entry.path for entry in entries if entry.path is not None)
        return paths


class _Tools(BaseModel):
    poetry: _Poetry = Field(default_factory=_Poetry)
    uv: _Uv = Field(default_factory=_Uv)


class _Pyproject(BaseModel):
    project: _Project | None = None
    tool: _Tools = Field(default_factory=_Tools)


class _UvLockSource(BaseModel):
    editable: str | None = None
    directory: str | None = None
    virtual: str | None = None

    def local_path(self) -> str | None:
        return self.editable or self.directory or self.virtual


class _UvLockPackage(BaseModel):
    name: str
    source: _UvLockSource = Field(default_factory=_UvLockSource)


class _UvLock(BaseModel):
    package: list[_UvLockPackage] = Field(default_factory=list)


class _PoetryLockSource(BaseModel):
    type: str = ""
    url: str = ""


class _PoetryLockPackage(BaseModel):
    name: str
    source: _PoetryLockSource | None = None


class _PoetryLock(BaseModel):
    package: list[_PoetryLockPackage] = Field(default_factory=list)


class _PipDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pip: list[str]


class _CondaEnvironment(BaseModel):
    dependencies: list[str | _PipDependencies]
    channels: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class ImageProject:
    """A project image is built from its manifests and the local dependency
    directories they name; the root project's own sources are never part of it
    and reach the container through source sync."""

    root: Path
    python_version: str
    files: tuple[str, ...]
    local_dependencies: tuple[str, ...]
    step: ImageBuildStep

    @property
    def context_entries(self) -> tuple[str, ...]:
        return (*self.files, *self.local_dependencies)


def project_context_files(root: Path, entries: Sequence[str]) -> list[Path]:
    """Expand context entries into root-relative files. A directory entry is a
    local dependency and contributes its sources under its own ``.lazycloudignore``."""
    files: set[Path] = set()
    for entry in entries:
        source = root / entry
        if source.is_dir():
            files.update(path.relative_to(root) for path in collect_source_files(source))
        else:
            files.add(Path(entry))
    return sorted(files)


def load_python_project(
    path: str | Path,
    *,
    kind: ImageBuildStepKind,
    python_version: str | None,
    extras: Sequence[str],
    groups: Sequence[str],
) -> ImageProject:
    root = Path(path).expanduser().resolve()
    manifest = root / "pyproject.toml"
    if not manifest.is_file():
        raise ValueError(f"project must contain pyproject.toml: {root}")
    files = ["pyproject.toml"]
    lockfile = {
        ImageBuildStepKind.UvProject: "uv.lock",
        ImageBuildStepKind.PoetryProject: "poetry.lock",
    }.get(kind)
    if lockfile:
        if not (root / lockfile).is_file():
            raise ValueError(f"project must contain {lockfile}: {root}")
        files.append(lockfile)
    with manifest.open("rb") as handle:
        metadata = _Pyproject.model_validate(tomli.load(handle))
    if kind is not ImageBuildStepKind.PoetryProject and metadata.project is None:
        raise ValueError(f"pyproject.toml must contain standard [project] metadata: {root}")
    requirements = [metadata.project.requires_python] if metadata.project else []
    if kind is ImageBuildStepKind.PoetryProject:
        requirements.append(metadata.tool.poetry.python_requirement())
    pin = root / ".python-version"
    project_pin = None
    if pin.is_file():
        project_pin = pin.read_text(encoding="utf-8").strip()
        files.append(".python-version")
    selected = select_project_python(
        python_version if python_version is not None else project_pin, requirements
    )
    return ImageProject(
        root=root,
        python_version=selected,
        files=tuple(files),
        local_dependencies=_local_dependencies(root, kind, metadata),
        step=ImageBuildStep(
            kind=kind,
            args=[".", *_selection_names(extras)],
            groups=_selection_names(groups),
        ),
    )


def _local_dependencies(
    root: Path, kind: ImageBuildStepKind, metadata: _Pyproject
) -> tuple[str, ...]:
    if kind is ImageBuildStepKind.UvProject:
        with (root / "uv.lock").open("rb") as handle:
            lock = _UvLock.model_validate(tomli.load(handle))
        candidates = [
            path for package in lock.package if (path := package.source.local_path()) is not None
        ]
        return _resolve_local_dependencies(root, candidates)
    if kind is ImageBuildStepKind.PoetryProject:
        with (root / "poetry.lock").open("rb") as handle:
            lock = _PoetryLock.model_validate(tomli.load(handle))
        candidates = [
            package.source.url
            for package in lock.package
            if package.source is not None and package.source.type == "directory"
        ]
        candidates.extend(metadata.tool.poetry.local_paths())
        return _resolve_local_dependencies(root, candidates)
    uv = metadata.tool.uv
    candidates = list(uv.local_paths())
    if uv.workspace is not None:
        candidates.extend(_workspace_members(root, uv.workspace))
    return _resolve_local_dependencies(root, candidates)


def _workspace_members(root: Path, workspace: _UvWorkspace) -> list[str]:
    excluded = {match.resolve() for pattern in workspace.exclude for match in root.glob(pattern)}
    members: list[str] = []
    for pattern in workspace.members:
        matches = sorted(root.glob(pattern))
        if not matches:
            if any(char in pattern for char in "*?["):
                continue
            raise ValueError(f"uv workspace member does not exist: {pattern}")
        members.extend(
            match.relative_to(root).as_posix()
            for match in matches
            if match.resolve() not in excluded and (match / "pyproject.toml").is_file()
        )
    return members


def _resolve_local_dependencies(root: Path, candidates: Sequence[str]) -> tuple[str, ...]:
    resolved: dict[str, None] = {}
    for candidate in candidates:
        target = (root / candidate).resolve()
        if target == root:
            continue
        try:
            relative = target.relative_to(root)
        except ValueError:
            raise ValueError(
                f"local dependency {candidate!r} is outside the project root {root}; "
                "move it inside the project directory so the image build can read it"
            ) from None
        if not target.exists():
            raise ValueError(f"local dependency does not exist: {candidate!r} in {root}")
        resolved.setdefault(relative.as_posix())
    return tuple(resolved)


def load_conda_environment(path: str | Path, *, python_version: str | None) -> ImageProject:
    manifest = Path(path).expanduser().resolve()
    if not manifest.is_file():
        raise ValueError(f"environment file does not exist: {manifest}")
    environment = _CondaEnvironment.model_validate(yaml.safe_load(manifest.read_text()))
    constraints: list[str] = []
    pin = None
    for dependency in environment.dependencies:
        if not isinstance(dependency, str):
            continue
        match = re.fullmatch(r"python\s*([=<>!~].*)?", dependency.rsplit("::", 1)[-1])
        if match is None:
            continue
        constraint = (match[1] or "").strip()
        exact = re.fullmatch(r"={1,2}(3\.\d+(?:\.\d+)?)(?:\.\*)?", constraint)
        if exact:
            pin = exact[1]
            constraint = f"=={pin}.*" if pin.count(".") == 1 else f"=={pin}"
        constraints.append(constraint)
    selected = select_project_python(
        python_version if python_version is not None else pin, constraints
    )
    return ImageProject(
        root=manifest.parent,
        python_version=f"micromamba{selected}",
        files=(manifest.name,),
        local_dependencies=(),
        step=ImageBuildStep(
            kind=ImageBuildStepKind.MicromambaEnvironment,
            args=[manifest.name, selected],
        ),
    )


def select_project_python(pin: str | None, requirements: Sequence[str]) -> str:
    constraints = [parse_constraint(value) for value in requirements if value]

    def compatible(value: str) -> bool:
        release = Version.parse(value)
        if value.count(".") == 2:
            return all(constraint.allows(release) for constraint in constraints)
        allowed: VersionConstraint = VersionRange(release, release.next_minor(), include_min=True)
        for constraint in constraints:
            allowed = allowed.intersect(constraint)
        return not allowed.is_empty()

    if pin is not None:
        selected = normalize_python_version(pin)
        if selected.startswith("micromamba"):
            raise ValueError("project Python pins must specify a Python version, not an installer")
        if not compatible(selected):
            raise ValueError(
                f"Python {selected} conflicts with project requirements: {requirements}"
            )
        return selected
    for candidate in dict.fromkeys(
        [ImageSpec().python_version, *(version.value for version in PythonVersion)]
    ):
        if compatible(candidate):
            return candidate
    raise ValueError(f"no supported Python version satisfies project requirements: {requirements}")


def _selection_names(values: Sequence[str]) -> list[str]:
    names = list(dict.fromkeys(values))
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) for name in names):
        raise ValueError("extras and dependency groups must be valid names")
    return names
