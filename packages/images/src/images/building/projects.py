from __future__ import annotations

import shlex

from shared.image_building.authoring import ImageBuildStep, ImageBuildStepKind
from shared.image_building.python import normalize_python_version

PROJECT_ENVIRONMENT = "/opt/lazycloud/.venv"
POETRY_ENVIRONMENT = "/opt/lazycloud/poetry"
POETRY_VERSION = "2.2.1"


def render_project_install(step: ImageBuildStep, *, python_executable: str) -> str:
    if not step.args:
        raise ValueError(f"{step.kind} requires a project path")
    path, *extras = step.args
    if step.kind is ImageBuildStepKind.MicromambaEnvironment:
        if len(extras) != 1:
            raise ValueError("micromamba environment requires the selected Python version")
        version = normalize_python_version(extras[0])
        return (
            shlex.join(
                [
                    "micromamba",
                    "create",
                    "-y",
                    "-n",
                    "base",
                    "--file",
                    path,
                    f"python={version}",
                    "pip",
                ]
            )
            + " && micromamba clean --all --yes"
        )
    if step.kind in {ImageBuildStepKind.UvProject, ImageBuildStepKind.Pyproject}:
        command = [
            "uv",
            "sync",
            "--no-default-groups",
            "--no-install-project",
            "--no-editable",
            "--no-python-downloads",
            "--python",
            python_executable,
            "--project",
            path,
        ]
        if step.kind is ImageBuildStepKind.UvProject:
            command.append("--locked")
        else:
            command.extend(["--no-config", "--no-sources", "--upgrade"])
        for extra in extras:
            command.extend(["--extra", extra])
        for group in step.groups:
            command.extend(["--group", group])
        return shlex.join(command)
    if step.kind is ImageBuildStepKind.PoetryProject:
        tool_python = f"{POETRY_ENVIRONMENT}/bin/python"
        poetry = f"{POETRY_ENVIRONMENT}/bin/poetry"
        command = [
            poetry,
            "--directory",
            path,
            "sync",
            "--no-root",
            "--no-interaction",
            "--only",
            ",".join(["main", *step.groups]),
        ]
        for extra in extras:
            command.extend(["--extras", extra])
        environment = f"VIRTUAL_ENV={PROJECT_ENVIRONMENT} POETRY_VIRTUALENVS_CREATE=false "
        return " && ".join(
            [
                shlex.join(
                    [
                        "uv",
                        "venv",
                        "--python",
                        python_executable,
                        "--no-python-downloads",
                        POETRY_ENVIRONMENT,
                    ]
                ),
                shlex.join(
                    ["uv", "pip", "install", "--python", tool_python, f"poetry=={POETRY_VERSION}"]
                ),
                shlex.join(
                    [
                        "uv",
                        "venv",
                        "--python",
                        python_executable,
                        "--no-python-downloads",
                        PROJECT_ENVIRONMENT,
                    ]
                ),
                environment + shlex.join([poetry, "--directory", path, "check", "--lock"]),
                environment + shlex.join(command),
            ]
        )
    raise ValueError(f"unsupported project installer: {step.kind}")
