from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path

from pydantic import JsonValue
from shared.image_building.authoring import ImageBuildStepKind, ImageSpec
from shared.image_building.context import fingerprint_build_context
from shared.image_building.credentials import image_secret_names
from shared.image_building.planning import ImageBuildPlan
from shared.image_building.requirements import sanitize_python_packages

from images.building.commands import _normalize_step, plan_image_build_commands
from images.building.constants import DEFAULT_IMAGE_BASE
from images.building.models import ImageInstallCommandMode, PythonRuntimeSetupAction
from images.building.python_runtime import plan_python_runtime_setup

IMAGE_BUILD_IDENTITY_CONTRACT_VERSION = 2


def build_image_plan(image: ImageSpec) -> ImageBuildPlan:
    context_digest = image.context_digest
    if image.context_path and not context_digest:
        context_digest = fingerprint_build_context(image.context_path)

    normalized = _normalize_image_spec(image, context_digest=context_digest)
    _validate_image_spec(normalized)
    dockerfile = render_image_dockerfile(normalized)
    cache_payload: dict[str, JsonValue] = {
        "contract_version": IMAGE_BUILD_IDENTITY_CONTRACT_VERSION,
        "architecture": normalized.architecture.value,
        "dockerfile": dockerfile,
        "context_digest": normalized.context_digest or "",
        "gpu": normalized.gpu or "",
        "secret_versions": dict(sorted(normalized.build_secret_versions.items())),
    }
    cache_key = _sha256_json(cache_payload)
    image_id = normalized.image_id or f"img_{cache_key[:24]}"
    return ImageBuildPlan(
        spec=normalized,
        image_id=image_id,
        cache_key=cache_key,
        dockerfile=dockerfile,
        context_digest=normalized.context_digest,
        credential_keys=normalized.credential_keys,
    )


def render_image_dockerfile(image: ImageSpec) -> str:
    _validate_image_spec(image)
    lines = _initial_dockerfile_lines(image)

    _append_env_and_build_args(lines, image)

    python_setup = plan_python_runtime_setup(image)
    lines.extend(python_setup.dockerfile_instructions)
    lines.extend(f"RUN {command}" for command in python_setup.commands)

    install_mode = (
        ImageInstallCommandMode.DockerfileManagedPython
        if python_setup.action is PythonRuntimeSetupAction.InstallManagedPython
        else ImageInstallCommandMode.Dockerfile
    )
    for build_command in plan_image_build_commands(
        image,
        mode=install_mode,
        python_executable=python_setup.python_executable,
    ):
        if build_command.kind is ImageBuildStepKind.UvProject:
            lines.extend(_uv_project_copy_lines(image, build_command.args))
        lines.append(f"RUN {build_command.command}")

    return "\n".join(lines).rstrip() + "\n"


def _normalize_image_spec(image: ImageSpec, *, context_digest: str | None) -> ImageSpec:
    steps = [_normalize_step(step) for step in image.build_steps]
    return image.model_copy(
        update={
            "packages": sanitize_python_packages(image.packages),
            "commands": [command.strip() for command in image.commands if command.strip()],
            "build_steps": [step for step in steps if step.args or step.command],
            "credential_keys": _dedupe(image.credential_keys),
            "secrets": image_secret_names(image.secrets),
            "build_secret_versions": dict(sorted(image.build_secret_versions.items())),
            "context_digest": context_digest,
        }
    )


def _validate_image_spec(image: ImageSpec) -> None:
    if image.dockerfile and image.base not in {"", DEFAULT_IMAGE_BASE}:
        msg = "dockerfile builds cannot also set a custom base image"
        raise ValueError(msg)
    if image.image_id is not None and not image.image_id.strip():
        msg = "image_id cannot be blank"
        raise ValueError(msg)
    for name in image_secret_names(image.secrets):
        if not _valid_env_name(name):
            msg = f"invalid image build secret name: {name}"
            raise ValueError(msg)


def _initial_dockerfile_lines(image: ImageSpec) -> list[str]:
    if image.dockerfile:
        return image.dockerfile.rstrip().splitlines()

    lines = [f"FROM {image.base}"]
    if image.workdir:
        lines.append(f"WORKDIR {image.workdir}")
    return lines


def _append_env_and_build_args(lines: list[str], image: ImageSpec) -> None:
    for key, value in sorted(image.env.items()):
        if not _valid_env_name(key):
            msg = f"invalid image environment variable name: {key}"
            raise ValueError(msg)
        lines.append(f"ENV {key}={_docker_value(value)}")

    for secret in image_secret_names(image.secrets):
        lines.append(f"ARG {secret}")


def _uv_project_copy_lines(image: ImageSpec, args: Iterable[str]) -> list[str]:
    project_dir = next((value for value in args if value.strip()), ".")
    source_prefix = "" if project_dir in {"", "."} else project_dir.rstrip("/") + "/"
    metadata_files = ["pyproject.toml", "uv.lock"]
    context_path = Path(image.context_path) if image.context_path else None
    if context_path is not None:
        source_dir = context_path / ("" if project_dir in {"", "."} else project_dir)
        if (source_dir / ".python-version").is_file():
            metadata_files.append(".python-version")
    return [f"COPY {source_prefix}{name} ./{name}" for name in metadata_files]


def _docker_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@+-]+", value):
        return value
    return json.dumps(value)


def _valid_env_name(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None


def _sha256_json(value: JsonValue) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result
