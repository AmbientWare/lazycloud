from __future__ import annotations

import shlex
from collections.abc import Iterable

from shared.image_building.authoring import ImageBuildStep, ImageBuildStepKind, ImageSpec

from images.building.constants import PIP_GROUP_BOUNDARY_FLAGS
from images.building.models import ImageBuildCommand, ImageInstallCommandMode
from images.building.requirements import sanitize_python_packages


def render_pip_install_command(
    packages: Iterable[str],
    *,
    mode: ImageInstallCommandMode = ImageInstallCommandMode.Dockerfile,
    python_executable: str = "python",
    system: bool = False,
) -> str:
    tokens = _install_tokens(packages)
    if not tokens:
        return ""
    if mode is ImageInstallCommandMode.Runtime:
        command = ["uv", "pip", "install"]
        if system:
            command.append("--system")
    elif mode is ImageInstallCommandMode.DockerfileManagedPython:
        command = [
            "uv",
            "pip",
            "install",
            "--python",
            python_executable,
            "--break-system-packages",
        ]
    else:
        command = [python_executable, "-m", "pip", "install"]
    return " ".join([*command, *tokens])


def render_uv_project_sync_command(
    args: Iterable[str],
    *,
    mode: ImageInstallCommandMode = ImageInstallCommandMode.Dockerfile,
) -> str:
    project_dir, extras = _uv_project_args(args)
    command = ["uv", "sync", "--frozen", "--no-dev", "--no-install-project"]
    for extra in extras:
        command.extend(["--extra", shlex.quote(extra)])
    if mode is ImageInstallCommandMode.Runtime and project_dir not in {"", "."}:
        command.extend(["--project", shlex.quote(project_dir)])
    return " ".join(command)


def render_micromamba_install_command(
    packages: Iterable[str],
    *,
    environment: str = "",
) -> str:
    tokens = _install_tokens(packages)
    if not tokens:
        return ""
    command = ["micromamba", "install", "-y"]
    if environment:
        command.extend(["-n", shlex.quote(environment)])
    return " ".join([*command, *tokens])


def plan_image_build_commands(
    image: ImageSpec,
    *,
    mode: ImageInstallCommandMode = ImageInstallCommandMode.Dockerfile,
    python_executable: str = "python",
    system: bool = False,
) -> list[ImageBuildCommand]:
    commands: list[ImageBuildCommand] = []
    pending_kind: ImageBuildStepKind | None = None
    pending_args: list[str] = []

    def flush(*, isolated: bool = False) -> None:
        nonlocal pending_kind, pending_args
        if pending_kind is None or not pending_args:
            pending_kind = None
            pending_args = []
            return
        command = _render_install_command(
            pending_kind,
            pending_args,
            mode=mode,
            python_executable=python_executable,
            system=system,
        )
        if command:
            commands.append(
                ImageBuildCommand(
                    kind=pending_kind,
                    command=command,
                    args=tuple(pending_args),
                    isolated=isolated,
                )
            )
        pending_kind = None
        pending_args = []

    for step in _ordered_build_steps(image):
        normalized = _normalize_step(step)
        if normalized.kind in {
            ImageBuildStepKind.Pip,
            ImageBuildStepKind.Micromamba,
        }:
            if not normalized.args:
                continue
            isolated = _install_step_requires_isolation(normalized.args)
            if pending_kind is not normalized.kind or isolated:
                flush()
                pending_kind = normalized.kind
                pending_args = list(normalized.args)
            else:
                pending_args.extend(normalized.args)
            if isolated:
                flush(isolated=True)
            continue

        flush()
        if command := _render_non_install_step(normalized, mode=mode):
            commands.append(
                ImageBuildCommand(
                    kind=normalized.kind,
                    command=command,
                    args=tuple(normalized.args),
                )
            )

    flush()
    return commands


def _normalize_step(step: ImageBuildStep) -> ImageBuildStep:
    if step.kind is ImageBuildStepKind.Pip:
        return ImageBuildStep(kind=step.kind, args=sanitize_python_packages(step.args))
    if step.kind is ImageBuildStepKind.UvProject:
        project_dir, extras = _uv_project_args(step.args)
        return ImageBuildStep(
            kind=ImageBuildStepKind.UvProject,
            args=[project_dir, *sanitize_python_packages(extras)],
        )
    if step.kind in {ImageBuildStepKind.Micromamba, ImageBuildStepKind.Apt}:
        args = [value.strip() for value in step.args if value.strip()]
        return ImageBuildStep(kind=step.kind, args=args)
    if step.command is None:
        return ImageBuildStep(kind=step.kind)
    return ImageBuildStep(kind=step.kind, command=step.command.strip())


def _step_has_content(step: ImageBuildStep) -> bool:
    return bool(step.args or (step.command and step.command.strip()))


def _ordered_build_steps(image: ImageSpec) -> list[ImageBuildStep]:
    ordered: list[ImageBuildStep] = []
    if image.packages:
        ordered.append(ImageBuildStep(kind=ImageBuildStepKind.Pip, args=list(image.packages)))
    ordered.extend(image.build_steps)
    ordered.extend(
        ImageBuildStep(kind=ImageBuildStepKind.Shell, command=command) for command in image.commands
    )
    return ordered


def _render_install_command(
    kind: ImageBuildStepKind,
    args: Iterable[str],
    *,
    mode: ImageInstallCommandMode,
    python_executable: str,
    system: bool,
) -> str:
    if kind is ImageBuildStepKind.Pip:
        return render_pip_install_command(
            args,
            mode=mode,
            python_executable=python_executable,
            system=system,
        )
    if kind is ImageBuildStepKind.Micromamba:
        return render_micromamba_install_command(args)
    msg = f"unsupported image install command kind: {kind}"
    raise ValueError(msg)


def _render_non_install_step(
    step: ImageBuildStep,
    *,
    mode: ImageInstallCommandMode,
) -> str:
    if step.kind is ImageBuildStepKind.Shell:
        return step.command or ""
    if step.kind is ImageBuildStepKind.Apt:
        args = " ".join(shlex.quote(value) for value in step.args)
        if not args:
            return ""
        return f"apt-get update && apt-get install -y {args} && rm -rf /var/lib/apt/lists/*"
    if step.kind is ImageBuildStepKind.UvProject:
        return render_uv_project_sync_command(step.args, mode=mode)
    msg = f"unsupported image build step kind: {step.kind}"
    raise ValueError(msg)


def _install_tokens(args: Iterable[str]) -> list[str]:
    flag_tokens: list[str] = []
    package_tokens: list[str] = []
    for value in args:
        item = value.strip()
        if not item:
            continue
        if item.startswith("-"):
            flag_tokens.extend(_split_flag_tokens(item))
        else:
            package_tokens.append(shlex.quote(item))
    return [*flag_tokens, *package_tokens]


def _split_flag_tokens(value: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError as exc:
        msg = f"invalid image install flag line: {value}"
        raise ValueError(msg) from exc


def _install_step_requires_isolation(args: Iterable[str]) -> bool:
    return any(flag in value for value in args for flag in PIP_GROUP_BOUNDARY_FLAGS)


def _uv_project_args(args: Iterable[str]) -> tuple[str, list[str]]:
    values = [value.strip() for value in args if value.strip()]
    if not values:
        return ".", []
    return values[0], values[1:]
