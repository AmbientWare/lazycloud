"""Deploy app functions: one source archive upload, then one deployment per app."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from shared.api import Deployment, DeploymentRequest, FunctionSpec, SourceUploadRequest

from lazycloud.clients.api import ApiClient
from lazycloud.exceptions import SdkError
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
from lazycloud.terminal import Terminal, humanize_bytes

if TYPE_CHECKING:
    from lazycloud.abstractions.function import Function


class DeploymentOperationError(SdkError):
    pass


@dataclass(frozen=True, slots=True)
class AppFunctions:
    """The functions of one app that a deployment makes current."""

    app: str
    functions: tuple[Function[..., object], ...]
    prune: bool = False


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
            specs.append(function.function_spec(handler=handler, source_sha256=sources[prefix]))
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


def _source_placement(function: Function[..., object], root: Path) -> tuple[str, tuple[str, ...]]:
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
        uploaded = _store_source(client, workspace, archive)
        step.done(f"{description} {'uploaded' if uploaded else 'already stored'}")
        return archive.sha256


def _store_source(client: ApiClient, workspace: str, archive: SourcePackageArchive) -> bool:
    """Make the archive present by digest; True when its bytes had to be sent."""
    request = SourceUploadRequest(sha256=archive.sha256, size_bytes=archive.size)
    state = client.create_source_upload(workspace, request)
    if state.present:
        return False
    if state.upload is None:
        msg = f"the API reported source {archive.sha256} missing without an upload target"
        raise DeploymentOperationError(msg)
    client.upload_source(state.upload, archive.path)
    if not client.create_source_upload(workspace, request).present:
        msg = f"source {archive.sha256} was not stored after its upload"
        raise DeploymentOperationError(msg)
    return True


__all__ = ["AppFunctions", "DeploymentOperationError", "deploy_functions"]
