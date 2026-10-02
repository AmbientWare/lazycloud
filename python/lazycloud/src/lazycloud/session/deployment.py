"""Deploy app functions, prepare working-tree releases and resolve deployment names."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from contextvars import copy_context
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from shared.api import Deployment as AppDeployment
from shared.api import (
    DeploymentPlanRequest,
    DeploymentRequest,
    Release,
    SourceUploadRequest,
    SubmitTasksRequest,
    Workload,
    WorkloadIdentity,
    WorkloadKind,
    WorkloadSpec,
)

from lazycloud._terminal.formatting import short_id
from lazycloud.clients.api import ApiClient
from lazycloud.control import api_client, require_workspace, resolve_control_client_config
from lazycloud.exceptions import (
    AmbiguousDeploymentError,
    DeploymentNotFoundError,
    SdkError,
)
from lazycloud.references import (
    HandlerReferenceError,
    source_root_handler_reference,
)
from lazycloud.session.task import (
    TERMINAL_STATUSES,
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
    from lazycloud.abstractions.image import Image, ImageBuildResult

MAX_IMAGE_PREPARATIONS = 4

_VERSIONED_NAME = re.compile(r"(?P<name>.+)-v(?P<version>[1-9][0-9]*)")


class WorkloadDefinition(Protocol):
    """What a deployment needs of a function, endpoint, ASGI app, pod or sandbox."""

    _app_slug: str
    image: Image
    terminal: Terminal | None

    @property
    def resource_name(self) -> str: ...

    def require_supported(self) -> None: ...

    def handler_reference(self) -> str | None:
        """The `module:qualname` the runner imports; None for a pod or sandbox."""
        ...

    def workload_spec(
        self, *, handler: Any, source_sha256: str, image: ImageBuildResult
    ) -> WorkloadSpec: ...


class DeploymentOperationError(SdkError):
    pass


class ImageBuildError(DeploymentOperationError):
    pass


@dataclass(frozen=True, slots=True)
class AppFunctions:
    """The functions of one app that a deployment makes current."""

    app: str
    functions: tuple[WorkloadDefinition, ...]
    prune: bool = False


def plan_request(target: AppFunctions, *, name: str | None = None) -> DeploymentPlanRequest:
    """The plan request for a deployment of `target`; `name` overrides each workload's name."""
    return DeploymentPlanRequest(
        workloads=[
            WorkloadIdentity(kind=workload_kind(function), name=name or function.resource_name)
            for function in target.functions
        ],
        prune=target.prune,
    )


def workload_kind(workload: object) -> WorkloadKind:
    from lazycloud.abstractions.endpoint import ASGI, Endpoint
    from lazycloud.abstractions.pod import Pod

    if isinstance(workload, Pod):
        return WorkloadKind.pod
    if isinstance(workload, Endpoint):
        return WorkloadKind.endpoint
    if isinstance(workload, ASGI):
        return WorkloadKind.asgi
    return WorkloadKind.function


@dataclass(frozen=True, slots=True)
class DeploymentReference:
    """A deployed workload and, for a `NAME-vN` reference, the version it names."""

    deployment: Workload
    version: int | None = None


@dataclass(frozen=True, slots=True)
class DeploymentSubmission:
    """A task submitted to a deployment's active version."""

    task: Task

    @property
    def task_id(self) -> str:
        return self.task.task_id

    @property
    def output(self) -> str:
        """What the task printed so far."""
        return self.task.output()

    @property
    def done(self) -> bool:
        return self.task.get().status in TERMINAL_STATUSES

    @property
    def exit_code(self) -> int | None:
        """Always None: tasks fail with a typed failure rather than an exit code."""
        return None

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

    deployment: Workload
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
        """Where the workload answers, following its active release.

        `url_type="stub"` pins the active release's own host instead, and
        `port` picks one of a pod's ports.
        """
        if url_type not in (None, "deployment", "stub"):
            raise ValueError(f"url_type must be 'deployment' or 'stub', not {url_type!r}")
        workload = self.deployment
        if port is not None and workload.kind is not WorkloadKind.pod:
            raise ValueError("only a pod's URL takes a port")
        url = workload.url
        if url is None:
            client, workspace = self.client._session()
            detail = client.get_workload(workspace, workload.app, workload.kind, workload.name)
            url = detail.release.url
        if url is None:
            raise LookupError(f"deployment {self.name} has no URL")
        parts = urlsplit(url)
        label, _, base = (parts.hostname or "").partition(".")
        if url_type == "stub" and workload.kind is not WorkloadKind.pod:
            label = self.stub_id
        if port is not None:
            # A pod port answers on <release>-<port>.<base>.
            label = f"{label.rsplit('-', 1)[0]}-{port}"
        netloc = f"{label}.{base}" + (f":{parts.port}" if parts.port else "")
        return urlunsplit(parts._replace(netloc=netloc))

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
    ) -> list[Workload]:
        """Deployments by app and name, up to `limit`."""
        client, workspace = self._session()
        deployments: list[Workload] = []
        cursor: str | None = None
        while len(deployments) < limit:
            page = client.list_workloads(
                workspace,
                app=app,
                name=name,
                limit=min(1000, limit - len(deployments)),
                cursor=cursor,
            )
            deployments.extend(page.workloads)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return deployments

    def get(self, deployment_id_or_name: str, *, app: str | None = None) -> Workload:
        client, workspace = self._session()
        return resolve_deployment(client, workspace, deployment_id_or_name, app=app).deployment

    def handle(self, deployment_id_or_name: str) -> Deployment:
        return Deployment(deployment=self.get(deployment_id_or_name), client=self)

    def stop(self, deployment_id_or_name: str, *, app: str | None = None) -> Workload:
        """Stop a deployment; `NAME-vN` must name its active version."""
        client, workspace = self._session()
        deployment = self._active(client, workspace, deployment_id_or_name, app, "stop")
        return client.stop_workload(workspace, deployment.app, deployment.kind, deployment.name)

    def start(self, deployment_id_or_name: str, *, app: str | None = None) -> Workload:
        """Start a deployment; `NAME-vN` makes version N active first."""
        client, workspace = self._session()
        reference = resolve_deployment(client, workspace, deployment_id_or_name, app=app)
        deployment = reference.deployment
        return client.start_workload(
            workspace, deployment.app, deployment.kind, deployment.name, version=reference.version
        )

    def scale(
        self, deployment_id_or_name: str, containers: int, *, app: str | None = None
    ) -> Workload:
        """Hold a pod at `containers` containers; `NAME-vN` must name its active version."""
        client, workspace = self._session()
        deployment = self._active(client, workspace, deployment_id_or_name, app, "scale")
        return client.scale_workload(
            workspace, deployment.app, deployment.kind, deployment.name, containers
        )

    def delete(self, deployment_id_or_name: str, *, app: str | None = None) -> Workload:
        """Delete a deployment and every version of it; a single version cannot be deleted."""
        client, workspace = self._session()
        reference = resolve_deployment(client, workspace, deployment_id_or_name, app=app)
        if reference.version is not None:
            msg = (
                f"{deployment_id_or_name} names one version; delete "
                f"{reference.deployment.name} to remove the deployment and every version"
            )
            raise DeploymentOperationError(msg)
        deployment = reference.deployment
        return client.delete_workload(workspace, deployment.app, deployment.kind, deployment.name)

    def _active(
        self, client: ApiClient, workspace: str, reference: str, app: str | None, action: str
    ) -> Workload:
        resolved = resolve_deployment(client, workspace, reference, app=app)
        deployment = resolved.deployment
        if resolved.version is not None and resolved.version != deployment.version:
            msg = (
                f"version {resolved.version} of {deployment.name} is not active; "
                f"{action} {deployment.name} to {action} its active version"
            )
            raise DeploymentOperationError(msg)
        return deployment

    def submit(
        self,
        deployment: Workload | str,
        *args: object,
        kwargs: dict[str, object] | None = None,
    ) -> DeploymentSubmission:
        """Submit one task with these arguments to the deployment's active version."""
        client, workspace = self._session()
        selected = self.get(deployment) if isinstance(deployment, str) else deployment
        parent = parent_task_id()
        request = SubmitTasksRequest(
            inputs=[task_input(args, kwargs or {}, workspace=workspace)],
            **({"parent_task_id": parent} if parent is not None else {}),
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
    specs = _workload_specs(
        functions, client=client, workspace=workspace, source_root=source_root, terminal=terminal
    )
    # With several apps, every app takes its new workloads before any app
    # loses an omitted one, so a refused later app leaves nothing pruned.
    staged = len(targets) > 1 and any(target.prune for target in targets)

    def request(target: AppFunctions, *, prune: bool) -> DeploymentRequest:
        return DeploymentRequest(
            workloads=[specs[id(function)] for function in target.functions], prune=prune
        )

    deployments: list[AppDeployment] = []
    for target in targets:
        with ExitStack() as stack:
            steps = [
                stack.enter_context(terminal.step("Runtime", function.resource_name))
                for function in target.functions
            ]
            deployment = client.deploy_app(
                workspace, target.app, request(target, prune=target.prune and not staged)
            )
            releases = {release.name: release for release in deployment.releases}
            for function, step in zip(target.functions, steps, strict=True):
                _runtime_done(step, function.resource_name, releases[function.resource_name])
        deployments.append(deployment)
    for n, target in enumerate(targets):
        if not target.prune:
            continue
        with terminal.step("Prune", target.app) as step:
            if staged:
                # The specs are unchanged, so this reuses the releases just made.
                deployments[n] = client.deploy_app(
                    workspace, target.app, request(target, prune=True)
                )
            step.done(f"{deployments[n].removed_versions} deployment versions removed")
    return deployments


def prepare_release(
    function: WorkloadDefinition,
    *,
    client: ApiClient,
    workspace: str,
    source_root: str | Path | None = None,
    terminal: Terminal | None = None,
) -> Release:
    """Upload the working tree and return the release that runs this definition of it."""
    terminal = terminal or Terminal(quiet=True)
    spec = _workload_specs(
        [function], client=client, workspace=workspace, source_root=source_root, terminal=terminal
    )[id(function)]
    with terminal.step("Runtime", function.resource_name) as step:
        release = client.prepare_release(workspace, function._app_slug, spec)
        _runtime_done(step, function.resource_name, release)
    return release


def prepare_spec(
    workload: WorkloadDefinition,
    *,
    client: ApiClient,
    workspace: str,
    source_root: str | Path | None = None,
    terminal: Terminal | None = None,
) -> tuple[WorkloadSpec, tuple[str, ...]]:
    """Ready one workload's image and source, as a deploy does, without deploying it.

    Returns the definition and the module prefix its files sit under in the
    container's workspace.
    """
    terminal = terminal or Terminal(quiet=True)
    spec = _workload_specs(
        [workload], client=client, workspace=workspace, source_root=source_root, terminal=terminal
    )[id(workload)]
    prefix = _source_placement(workload, Path(source_root or ".").expanduser().resolve())[1]
    return spec, prefix or ()


def resolve_deployment(
    client: ApiClient, workspace: str, reference: str, *, app: str | None = None
) -> DeploymentReference:
    """Find a deployment by id, by exact name, or as `NAME-vN` for version N of NAME.

    `app` limits the names to one app's deployments.
    """
    try:
        workload_id = UUID(reference)
    except ValueError:
        named = _deployment_named(client, workspace, reference, app)
    else:
        found = client.list_workloads(workspace, workload_id=workload_id).workloads
        named = found[0] if found else None
    if named is not None:
        return DeploymentReference(named)
    versioned = _VERSIONED_NAME.fullmatch(reference)
    if versioned is not None:
        named = _deployment_named(client, workspace, versioned["name"], app)
        if named is not None:
            return DeploymentReference(named, int(versioned["version"]))
    raise DeploymentNotFoundError(reference)


def _deployment_named(
    client: ApiClient, workspace: str, name: str, app: str | None
) -> Workload | None:
    matches = client.list_workloads(workspace, app=app, name=name).workloads
    if len(matches) > 1:
        raise AmbiguousDeploymentError(name, sorted(item.app for item in matches))
    return matches[0] if matches else None


def _workload_specs(
    functions: Sequence[WorkloadDefinition],
    *,
    client: ApiClient,
    workspace: str,
    source_root: str | Path | None,
    terminal: Terminal,
) -> dict[int, WorkloadSpec]:
    """Each function's API definition after its image and source are in place."""
    root = Path(source_root or ".").expanduser().resolve()
    if not root.is_dir():
        raise DeploymentOperationError(f"deployment source root is not a directory: {root}")
    for function in functions:
        function.require_supported()
    placements = {id(function): _source_placement(function, root) for function in functions}
    images = _prepare_images(functions, client=client, workspace=workspace, terminal=terminal)
    prefixes = list(dict.fromkeys(prefix for _, prefix in placements.values()))
    if any(prefix is not None for prefix in prefixes) and ensure_source_ignore_file(root):
        terminal.detail(SOURCE_IGNORE_FILE_WRITTEN_NOTICE)
    sources: dict[tuple[str, ...] | None, str] = {}
    for prefix in prefixes:
        sources[prefix] = (
            _store_empty_source(client, workspace)
            if prefix is None
            else _upload_source(
                client, workspace=workspace, root=root, archive_prefix=prefix, terminal=terminal
            )
        )
    specs: dict[int, WorkloadSpec] = {}
    for function in functions:
        handler, prefix = placements[id(function)]
        specs[id(function)] = function.workload_spec(
            handler=handler, source_sha256=sources[prefix], image=images[id(function)]
        )
    return specs


def _prepare_images(
    functions: Sequence[WorkloadDefinition],
    *,
    client: ApiClient,
    workspace: str,
    terminal: Terminal,
) -> dict[int, ImageBuildResult]:
    """Make each distinct image ready once, keyed by the function's id().

    Up to `MAX_IMAGE_PREPARATIONS` distinct images build at once.
    """
    distinct: dict[str, Image] = {}
    keys: dict[int, str] = {}
    for function in functions:
        image = function.image
        key = image.explicit_image_id or image.definition().model_dump_json()
        distinct.setdefault(key, image)
        keys[id(function)] = key

    def build(image: Image) -> ImageBuildResult:
        result = image.build(client, workspace=workspace, terminal=terminal)
        if not result.success:
            build = f" build {result.build_id}" if result.build_id else ""
            image_name = f" {result.image_id}" if result.image_id else ""
            msg = f"image{image_name}{build} failed: {result.error or 'unknown error'}"
            raise ImageBuildError(msg)
        return result

    with ThreadPoolExecutor(
        max_workers=MAX_IMAGE_PREPARATIONS, thread_name_prefix="deployment-image"
    ) as executor:
        futures = {
            key: executor.submit(copy_context().run, build, image)
            for key, image in distinct.items()
        }
        try:
            for future in as_completed(futures.values()):
                future.result()
        except BaseException:
            for future in futures.values():
                future.cancel()
            raise
    return {function_id: futures[key].result() for function_id, key in keys.items()}


def _runtime_done(step: TerminalStep, name: str, release: Release) -> None:
    step.done(f"{name} · {short_id(release.id)}")


def _source_placement(
    function: WorkloadDefinition, root: Path
) -> tuple[str | None, tuple[str, ...] | None]:
    """The handler reference inside the archive and the archive's module prefix.

    A pod has no handler and gets the source root as its workspace. A sandbox
    gets it only with `sync_local_dir`; otherwise its prefix is None and it
    runs on an empty archive.
    """
    handler = function.handler_reference()
    if handler is None:
        return None, () if getattr(function, "sync_local_dir", True) else None
    try:
        reference = source_root_handler_reference(handler, root)
    except HandlerReferenceError as exc:
        raise DeploymentOperationError(str(exc)) from exc
    return reference.handler, reference.archive_prefix


def _store_empty_source(client: ApiClient, workspace: str) -> str:
    buffer = io.BytesIO()
    zipfile.ZipFile(buffer, "w").close()
    archive = buffer.getvalue()
    sha256 = hashlib.sha256(archive).hexdigest()
    client.store_source(workspace, sha256, archive)
    return sha256


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
    "prepare_release",
    "prepare_spec",
    "resolve_deployment",
]
