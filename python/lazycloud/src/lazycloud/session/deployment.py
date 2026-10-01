"""Deploy app functions, prepare working-tree releases and resolve deployment names."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from shared.api import (
    DeployedWorkload,
    Deployment,
    DeploymentRequest,
    FunctionSpec,
    Release,
    SourceUploadRequest,
)
from shared.image_building.python import python_minor_version

from lazycloud.clients.api import ApiClient
from lazycloud.exceptions import AmbiguousDeploymentError, DeploymentNotFoundError, SdkError
from lazycloud.references import (
    HandlerReferenceError,
    dotted_reference,
    source_root_handler_reference,
)
from lazycloud.source_sync import (
    SOURCE_IGNORE_FILE_WRITTEN_NOTICE,
    SourcePackageArchive,
    build_source_package_archive,
    ensure_source_ignore_file,
)
from lazycloud.terminal import Terminal, TerminalStep, humanize_bytes

if TYPE_CHECKING:
    from lazycloud.abstractions.function import Function

_VERSIONED_NAME = re.compile(r"(?P<name>.+)-v(?P<version>[1-9][0-9]*)")


class DeploymentOperationError(SdkError):
    pass


@dataclass(frozen=True, slots=True)
class AppFunctions:
    """The functions of one app that a deployment makes current."""

    app: str
    functions: tuple[Function[..., Any], ...]
    prune: bool = False


@dataclass(frozen=True, slots=True)
class DeploymentReference:
    """A deployed workload and, for a `NAME-vN` reference, the version it names."""

    deployment: DeployedWorkload
    version: int | None = None


def deploy_functions(
    targets: Sequence[AppFunctions],
    *,
    client: ApiClient,
    workspace: str,
    source_root: str | Path | None = None,
    terminal: Terminal | None = None,
) -> list[Deployment]:
    """Upload the source the functions import from and deploy each app.

    Every function's options are checked before any bytes move, so an
    unsupported option never leaves a half-deployed app.
    """
    terminal = terminal or Terminal(quiet=True)
    functions = [function for target in targets for function in target.functions]
    specs = _function_specs(
        functions, client=client, workspace=workspace, source_root=source_root, terminal=terminal
    )
    deployments: list[Deployment] = []
    for target in targets:
        with ExitStack() as stack:
            steps = [
                stack.enter_context(terminal.step("Runtime", function.resource_name))
                for function in target.functions
            ]
            deployment = client.deploy_app(
                workspace,
                target.app,
                DeploymentRequest(
                    functions=[specs[id(function)] for function in target.functions],
                    prune=target.prune,
                ),
            )
            releases = {release.function: release for release in deployment.releases}
            for function, step in zip(target.functions, steps, strict=True):
                _runtime_done(step, function.resource_name, releases[function.resource_name])
        if target.prune:
            with terminal.step("Prune", target.app) as step:
                step.done(f"{deployment.removed_versions} deployment versions removed")
        deployments.append(deployment)
    return deployments


def prepare_function_release(
    function: Function[..., Any],
    *,
    client: ApiClient,
    workspace: str,
    source_root: str | Path | None = None,
    terminal: Terminal | None = None,
) -> Release:
    """Upload the working tree and return the release that runs this definition of it."""
    terminal = terminal or Terminal(quiet=True)
    spec = _function_specs(
        [function], client=client, workspace=workspace, source_root=source_root, terminal=terminal
    )[id(function)]
    with terminal.step("Runtime", function.resource_name) as step:
        release = client.prepare_function_release(
            workspace, function._app_slug, function.resource_name, spec
        )
        _runtime_done(step, function.resource_name, release)
    return release


def resolve_deployment(client: ApiClient, workspace: str, reference: str) -> DeploymentReference:
    """Find a deployment by id, by exact name, or as `NAME-vN` for version N of NAME."""
    try:
        deployment_id = UUID(reference)
    except ValueError:
        pass
    else:
        return DeploymentReference(client.get_deployment(workspace, deployment_id))
    named = _deployments_named(client, workspace, reference)
    if named is not None:
        return DeploymentReference(named)
    versioned = _VERSIONED_NAME.fullmatch(reference)
    if versioned is not None:
        named = _deployments_named(client, workspace, versioned["name"])
        if named is not None:
            return DeploymentReference(named, int(versioned["version"]))
    raise DeploymentNotFoundError(reference)


def _deployments_named(client: ApiClient, workspace: str, name: str) -> DeployedWorkload | None:
    matches = client.list_deployments(workspace, name=name).deployments
    if len(matches) > 1:
        raise AmbiguousDeploymentError(name, sorted(item.app for item in matches))
    return matches[0] if matches else None


def _function_specs(
    functions: Sequence[Function[..., Any]],
    *,
    client: ApiClient,
    workspace: str,
    source_root: str | Path | None,
    terminal: Terminal,
) -> dict[int, FunctionSpec]:
    """Each function's API definition after its image and source are in place."""
    root = Path(source_root or ".").expanduser().resolve()
    if not root.is_dir():
        raise DeploymentOperationError(f"deployment source root is not a directory: {root}")
    for function in functions:
        function.require_supported()
    placements = {id(function): _source_placement(function, root) for function in functions}
    for version in dict.fromkeys(
        python_minor_version(function.image.python_version) for function in functions
    ):
        with terminal.step("Image", "preparing") as step:
            # Only base images exist, and every host has them.
            step.done(f"python {version} · cached")
    if ensure_source_ignore_file(root):
        terminal.detail(SOURCE_IGNORE_FILE_WRITTEN_NOTICE)
    sources: dict[tuple[str, ...], str] = {}
    for prefix in dict.fromkeys(prefix for _, prefix in placements.values()):
        sources[prefix] = _upload_source(
            client, workspace=workspace, root=root, archive_prefix=prefix, terminal=terminal
        )
    specs: dict[int, FunctionSpec] = {}
    for function in functions:
        handler, prefix = placements[id(function)]
        specs[id(function)] = function.function_spec(handler=handler, source_sha256=sources[prefix])
    return specs


def _runtime_done(step: TerminalStep, name: str, release: Release) -> None:
    step.done(f"{name} · {str(release.id)[:8]}")


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
        step.update(f"syncing {description}")
        _store_source(
            client,
            workspace,
            archive,
            progress=lambda completed: step.update(
                f"syncing {min(100, completed * 100 // archive.size):3}% · {description}"
            ),
        )
        step.done(description)
        return archive.sha256


def _store_source(
    client: ApiClient,
    workspace: str,
    archive: SourcePackageArchive,
    *,
    progress: Callable[[int], None],
) -> None:
    """Make the archive present by digest, sending its bytes only when missing."""
    request = SourceUploadRequest(sha256=archive.sha256, size_bytes=archive.size)
    state = client.create_source_upload(workspace, request)
    if state.present:
        return
    if state.upload is None:
        msg = f"the API reported source {archive.sha256} missing without an upload target"
        raise DeploymentOperationError(msg)
    client.upload_source(state.upload, archive.path, progress=progress)
    if not client.create_source_upload(workspace, request).present:
        msg = f"source {archive.sha256} was not stored after its upload"
        raise DeploymentOperationError(msg)


__all__ = [
    "AppFunctions",
    "DeploymentOperationError",
    "DeploymentReference",
    "deploy_functions",
    "prepare_function_release",
    "resolve_deployment",
]
