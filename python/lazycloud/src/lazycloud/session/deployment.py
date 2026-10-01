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
    DeploymentRequest,
    FunctionSpec,
    Release,
    SourceUploadRequest,
    SubmitTasksRequest,
)
from shared.api import Deployment as AppDeployment

from lazycloud.clients.api import ApiClient
from lazycloud.control import api_client, require_workspace, resolve_control_client_config
from lazycloud.exceptions import (
    AmbiguousDeploymentError,
    DeploymentNotFoundError,
    SdkError,
    UnsupportedFeatureError,
)
from lazycloud.references import (
    HandlerReferenceError,
    dotted_reference,
    source_root_handler_reference,
)
from lazycloud.session.task import (
    Task,
    TaskSubscription,
    decode_payload,
    parent_task_id,
    task_input,
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
    from lazycloud.abstractions.image import ImageBuildResult

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


@dataclass(frozen=True, slots=True)
class DeploymentSubmission:
    """A task submitted to a deployment's active version."""

    task: Task

    @property
    def task_id(self) -> str:
        return self.task.task_id

    def result(self, *, wait: bool = False) -> object:
        """The task's value; a task that did not succeed raises."""
        result = self.task.result(wait=wait)
        if not result.ok:
            detail = f": {result.error}" if result.error else ""
            raise DeploymentOperationError(
                f"deployment task {result.id} is {result.status.value}{detail}"
            )
        return decode_payload(result.value) if result.value is not None else None

    def subscribe(self) -> TaskSubscription:
        return self.task.subscribe()


@dataclass(slots=True)
class Deployment:
    """A deployed workload, as `DeploymentClient.handle` finds it."""

    deployment: DeployedWorkload
    client: DeploymentClient

    @property
    def id(self) -> str:
        return str(self.deployment.id)

    @property
    def name(self) -> str:
        return self.deployment.name

    @property
    def stub_id(self) -> str:
        """The active release, which deployed calls run on."""
        release = self.deployment.release_id
        return str(release) if release is not None else ""

    def invoke_url(self, *, port: int | None = None, url_type: str | None = None) -> str:
        raise UnsupportedFeatureError(f"deployment {self.name}", ["invoke_url"])

    def submit(
        self, *args: object, kwargs: dict[str, object] | None = None
    ) -> DeploymentSubmission:
        return self.client.submit(self.deployment, *args, kwargs=kwargs)

    def subscribe(self, *args: object, kwargs: dict[str, object] | None = None) -> TaskSubscription:
        return self.submit(*args, kwargs=kwargs).subscribe()


@dataclass(slots=True)
class DeploymentClient:
    """Find and manage deployments in one workspace with the active profile."""

    workspace: str | None = None
    client: ApiClient | None = None

    def list(
        self, *, app: str | None = None, name: str | None = None, limit: int = 100
    ) -> list[DeployedWorkload]:
        """Deployments by app and name, up to `limit`."""
        client, workspace = self._session()
        deployments: list[DeployedWorkload] = []
        cursor: str | None = None
        while len(deployments) < limit:
            page = client.list_deployments(
                workspace,
                app=app,
                name=name,
                limit=min(1000, limit - len(deployments)),
                cursor=cursor,
            )
            deployments.extend(page.deployments)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return deployments

    def get(self, deployment_id_or_name: str) -> DeployedWorkload:
        client, workspace = self._session()
        return resolve_deployment(client, workspace, deployment_id_or_name).deployment

    def handle(self, deployment_id_or_name: str) -> Deployment:
        return Deployment(deployment=self.get(deployment_id_or_name), client=self)

    def stop(self, deployment_id_or_name: str) -> DeployedWorkload:
        """Stop a deployment; `NAME-vN` must name its active version."""
        client, workspace = self._session()
        reference = resolve_deployment(client, workspace, deployment_id_or_name)
        deployment = reference.deployment
        if reference.version is not None and reference.version != deployment.version:
            msg = (
                f"version {reference.version} of {deployment.name} is not active; "
                f"stop {deployment.name} to stop its active version"
            )
            raise DeploymentOperationError(msg)
        return client.stop_deployment(workspace, deployment.id)

    def start(self, deployment_id_or_name: str) -> DeployedWorkload:
        """Start a deployment; `NAME-vN` makes version N active first."""
        client, workspace = self._session()
        reference = resolve_deployment(client, workspace, deployment_id_or_name)
        return client.start_deployment(
            workspace, reference.deployment.id, version=reference.version
        )

    def delete(self, deployment_id_or_name: str) -> DeployedWorkload:
        """Delete a deployment and every version of it; a single version cannot be deleted."""
        client, workspace = self._session()
        reference = resolve_deployment(client, workspace, deployment_id_or_name)
        if reference.version is not None:
            msg = (
                f"{deployment_id_or_name} names one version; delete "
                f"{reference.deployment.name} to remove the deployment and every version"
            )
            raise DeploymentOperationError(msg)
        return client.delete_deployment(workspace, reference.deployment.id)

    def submit(
        self,
        deployment: DeployedWorkload | str,
        *args: object,
        kwargs: dict[str, object] | None = None,
    ) -> DeploymentSubmission:
        """Submit one task with these arguments to the deployment's active version."""
        client, workspace = self._session()
        selected = self.get(deployment) if isinstance(deployment, str) else deployment
        request = SubmitTasksRequest(
            inputs=[task_input(args, kwargs or {}, workspace=workspace)],
            parent_task_id=parent_task_id(),
        )
        response = client.submit_tasks(workspace, selected.app, selected.name, request)
        return DeploymentSubmission(Task(str(response.tasks[0].id), workspace, client))

    def _session(self) -> tuple[ApiClient, str]:
        config = resolve_control_client_config(workspace=self.workspace)
        if self.client is None:
            self.client = api_client(config)
        return self.client, require_workspace(config)


def deploy_functions(
    targets: Sequence[AppFunctions],
    *,
    client: ApiClient,
    workspace: str,
    source_root: str | Path | None = None,
    terminal: Terminal | None = None,
) -> list[AppDeployment]:
    """Upload the source the functions import from and deploy each app.

    Every function's options are checked before any bytes move, so an
    unsupported option never leaves a half-deployed app.
    """
    terminal = terminal or Terminal(quiet=True)
    functions = [function for target in targets for function in target.functions]
    specs = _function_specs(
        functions, client=client, workspace=workspace, source_root=source_root, terminal=terminal
    )
    deployments: list[AppDeployment] = []
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
    images = _prepare_images(functions, client=client, workspace=workspace, terminal=terminal)
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
        specs[id(function)] = function.function_spec(
            handler=handler, source_sha256=sources[prefix], image=images[id(function)]
        )
    return specs


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
    "Deployment",
    "DeploymentClient",
    "DeploymentOperationError",
    "DeploymentReference",
    "DeploymentSubmission",
    "deploy_functions",
    "prepare_function_release",
    "resolve_deployment",
]
