"""Read standalone example projects shipped with this client release."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ExampleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    description: str
    python_version: str = Field(default="3.12", pattern=r"^3\.\d+$")
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExampleProject:
    manifest: ExampleManifest
    content: dict[str, bytes]

    @property
    def size_bytes(self) -> int:
        return sum(len(value) for value in self.content.values())


def example_catalog() -> dict[str, ExampleProject]:
    catalog: dict[str, ExampleProject] = {}
    root = files("lazycloud").joinpath("_examples")
    sdk_version = version("lazycloud-client")
    for directory in sorted(root.iterdir(), key=lambda entry: entry.name):
        if not directory.is_dir() or directory.name == "__pycache__":
            continue
        manifest = ExampleManifest.model_validate_json(
            directory.joinpath("example.json").read_text(encoding="utf-8")
        )
        if manifest.name in catalog:
            raise ValueError(f"duplicate example name: {manifest.name}")
        content = _project_files(f"_examples/{directory.name}/project")
        content["pyproject.toml"] = _project_toml(manifest, sdk_version).encode("utf-8")
        content[".python-version"] = f"{manifest.python_version}\n".encode()
        content[".gitignore"] = b".venv/\n__pycache__/\n*.pyc\n.env\n"
        catalog[manifest.name] = ExampleProject(manifest, content)
    return catalog


def write_projects(projects: dict[Path, ExampleProject], *, force: bool = False) -> list[str]:
    destinations: dict[Path, bytes] = {}
    for target, project in projects.items():
        root = target.expanduser().resolve()
        for relative, content in project.content.items():
            destination = root / relative
            for path in (destination, *destination.parents):
                if path == root.parent:
                    break
                if path.is_symlink():
                    raise ValueError(f"refusing to write through symlink: {path}")
                if path != destination and path.exists() and not path.is_dir():
                    raise ValueError(f"not a directory: {path}")
            if destination.exists() and (not force or not destination.is_file()):
                raise ValueError(f"file already exists: {destination}")
            destinations[destination] = content
    for path, content in destinations.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return [str(path) for path in destinations]


def _project_files(resource_path: str, prefix: str = "") -> dict[str, bytes]:
    directory = files("lazycloud").joinpath(resource_path)
    result: dict[str, bytes] = {}
    for entry in sorted(directory.iterdir(), key=lambda child: child.name):
        if entry.name == "__pycache__" or entry.name.endswith(".pyc"):
            continue
        name = f"{prefix}{entry.name}"
        if entry.is_dir():
            result.update(_project_files(f"{resource_path}/{entry.name}", f"{name}/"))
        else:
            result[name] = entry.read_bytes()
    return result


def _project_toml(manifest: ExampleManifest, sdk_version: str) -> str:
    dependencies = [f"lazycloud-client=={sdk_version}", *manifest.dependencies]
    return (
        "[project]\n"
        f'name = "lazycloud-example-{manifest.name}"\n'
        'version = "0.1.0"\n'
        f"description = {json.dumps(manifest.description)}\n"
        f'requires-python = ">={manifest.python_version}"\n'
        f"dependencies = {json.dumps(dependencies)}\n\n"
        "[tool.uv]\npackage = false\n"
    )
