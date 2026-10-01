"""Deploy app functions: ready their images, upload source once, then deploy each app."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shared.api import Deployment, DeploymentRequest, FunctionSpec

from lazycloud.clients.api import ApiClient
from lazycloud.exceptions import SdkError
from lazycloud.references import (
    HandlerReferenceError,
    dotted_reference,
    source_root_handler_reference,
)
from lazycloud.source_sync import (
    SOURCE_IGNORE_FILE_WRITTEN_NOTICE,
    build_source_package_archive,
    ensure_source_ignore_file,
)
from lazycloud.terminal import Terminal, humanize_bytes

if TYPE_CHECKING:
    from lazycloud.abstractions.function import Function
    from lazycloud.abstractions.image import ImageBuildResult


class DeploymentOperationError(SdkError):
    pass


@dataclass(frozen=True, slots=True)
class AppFunctions:
    """The functions of one app that a deployment makes current."""

    app: str
    functions: tuple[Function[..., Any], ...]
    prune: bool = False


def deploy_functions(
    targets: Sequence[AppFunctions],
    *,
    client: ApiClient,
    workspace: str,
    source_root: str | Path | None = None,
    terminal: Terminal | None = None,
) -> list[Deployment]:
    """Ready the functions' images, upload the source they import from and deploy each app.

    Every function's options are checked before any bytes move, so an
    unsupported option never leaves a half-deployed app.
    """
    terminal = terminal or Terminal(quiet=True)
    root = Path(source_root or ".").expanduser().resolve()
    if not root.is_dir():
        raise DeploymentOperationError(f"deployment source root is not a directory: {root}")
    for target in targets:
        for function in target.functions:
            function.require_supported()
    placements = {
        id(function): _source_placement(function, root)
        for target in targets
        for function in target.functions
    }
    images = _prepare_images(
        [function for target in targets for function in target.functions],
        client=client,
        workspace=workspace,
        terminal=terminal,
    )
    if ensure_source_ignore_file(root):
        terminal.detail(SOURCE_IGNORE_FILE_WRITTEN_NOTICE)
    sources: dict[tuple[str, ...], str] = {}
    for prefix in dict.fromkeys(prefix for _, prefix in placements.values()):
        sources[prefix] = _upload_source(
            client, workspace=workspace, root=root, archive_prefix=prefix, terminal=terminal
        )
    deployments: list[Deployment] = []
    for target in targets:
        specs: list[FunctionSpec] = []
        for function in target.functions:
            handler, prefix = placements[id(function)]
            specs.append(
                function.function_spec(
                    handler=handler, source_sha256=sources[prefix], image=images[id(function)]
                )
            )
        with terminal.step("Deploy", target.app) as step:
            deployment = client.deploy_app(
                workspace,
                target.app,
                DeploymentRequest(functions=specs, prune=target.prune),
            )
            summary = ", ".join(
                f"{release.function} v{release.version}" for release in deployment.releases
            )
            if deployment.pruned:
                summary += f"; stopped {', '.join(item.root for item in deployment.pruned)}"
            step.done(summary)
        deployments.append(deployment)
    return deployments


def _source_placement(function: Function[..., Any], root: Path) -> tuple[str, tuple[str, ...]]:
    """The handler reference inside the archive and the archive's module prefix."""
    try:
        reference = source_root_handler_reference(dotted_reference(function.func), root)
    except HandlerReferenceError as exc:
        raise DeploymentOperationError(str(exc)) from exc
    return reference.handler, reference.archive_prefix


def _upload_source(
    client: ApiClient,
    *,
    workspace: str,
    root: Path,
    archive_prefix: tuple[str, ...],
    terminal: Terminal,
) -> str:
    with (
        terminal.step("Source", "collecting files") as step,
        build_source_package_archive(
            root, archive_prefix=archive_prefix, progress=step.update
        ) as archive,
    ):
        plural = "s" if len(archive.files) != 1 else ""
        description = f"{len(archive.files):,} file{plural}, {humanize_bytes(archive.size)}"
        step.update(f"uploading {description}")
        uploaded = client.store_source(workspace, archive.sha256, archive.path)
        step.done(f"{description} {'uploaded' if uploaded else 'already stored'}")
        return archive.sha256


def _prepare_images(
    functions: Sequence[Function[..., Any]],
    *,
    client: ApiClient,
    workspace: str,
    terminal: Terminal,
) -> dict[int, ImageBuildResult]:
    """Make each distinct image ready once, keyed by the function's id()."""
    by_definition: dict[str, ImageBuildResult] = {}
    results: dict[int, ImageBuildResult] = {}
    for function in functions:
        image = function.image
        key = image.explicit_image_id or image.definition().model_dump_json()
        result = by_definition.get(key)
        if result is None:
            result = image.build(client, workspace=workspace, terminal=terminal)
            if not result.success:
                build = f" build {result.build_id}" if result.build_id else ""
                image_name = f" {result.image_id}" if result.image_id else ""
                msg = f"image{image_name}{build} failed: {result.error or 'unknown error'}"
                raise DeploymentOperationError(msg)
            by_definition[key] = result
        results[id(function)] = result
    return results


__all__ = ["AppFunctions", "DeploymentOperationError", "deploy_functions"]
