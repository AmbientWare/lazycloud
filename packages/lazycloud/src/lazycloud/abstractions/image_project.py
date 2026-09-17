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


class _Project(BaseModel):
    requires_python: str = Field(default="", alias="requires-python")


class _PoetryPython(BaseModel):
    version: str


class _PoetryDependencies(BaseModel):
    python: str | _PoetryPython = ""


class _Poetry(BaseModel):
    dependencies: _PoetryDependencies = Field(default_factory=_PoetryDependencies)


class _Tools(BaseModel):
    poetry: _Poetry = Field(default_factory=_Poetry)


class _Pyproject(BaseModel):
    project: _Project | None = None
    tool: _Tools = Field(default_factory=_Tools)


class _PipDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pip: list[str]


class _CondaEnvironment(BaseModel):
    dependencies: list[str | _PipDependencies]
    channels: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class ImageProject:
    root: Path
    python_version: str
    files: tuple[str, ...]
    step: ImageBuildStep


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
        poetry_python = metadata.tool.poetry.dependencies.python
        requirements.append(
            poetry_python if isinstance(poetry_python, str) else poetry_python.version
        )
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
        step=ImageBuildStep(
            kind=kind,
            args=[".", *_selection_names(extras)],
            groups=_selection_names(groups),
        ),
    )


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
