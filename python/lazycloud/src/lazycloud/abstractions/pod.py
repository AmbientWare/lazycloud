from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import UUID

from pydantic import ValidationError
from shared.api import (
    AllowListItem,
    CheckpointSpec,
    CommandItem,
    ContainerState,
    CreateInstanceRequest,
    DiskMountSpec,
    HealthCheck,
    PodKind,
    PodSpec,
    StopReason,
    Workload,
    WorkloadKind,
    WorkloadSpec,
)
from shared.api import Container as ApiContainer
from shared.deployment_records import (
    DEFAULT_DISK,
    DEFAULT_WORKLOAD_PREEMPTIBLE,
    CpuRequest,
    DeploymentSpec,
    MemoryRequest,
    Resources,
    VolumeMount,
)
from shared.deployments import DeploymentKind, PodRole
from shared.disks import DISK_ROOT_MOUNT_PATH, DiskMount
from shared.gpu import GpuInput, gpu_preference
from shared.image_building.python import python_minor_version
from shared.placement import ProductRegion

from lazycloud.abstractions.disk import disk_mounts
from lazycloud.abstractions.image import Image
from lazycloud.abstractions.metadata import MachineInput, build_resource_metadata
from lazycloud.abstractions.volume import volume_mounts
from lazycloud.clients.workloads import WorkloadsClient
from lazycloud.control import resolve_control_client_config, workloads_client
from lazycloud.exceptions import SdkError
from lazycloud.session.task import follow_log_stream
from lazycloud.terminal import Terminal

if TYPE_CHECKING:
    from shared.api import Deployment

    from lazycloud.abstractions.image import ImageBuildResult
    from lazycloud.abstractions.shell import ShellSession

# Idle seconds before a devbox stops when its keep_warm is unset.
DEVBOX_KEEP_WARM_SECONDS = 1800


class PodOperationError(SdkError):
    pass


class PodOptions(TypedDict, total=False):
    name: str
    image: Image
    command: list[str]
    ports: dict[str, int]
    env: dict[str, str]
    cpu: CpuRequest | None
    memory: MemoryRequest | None
    disk: str | None
    gpu: GpuInput
    gpu_count: int
    keep_warm: int | None
    secrets: list[str]
    volumes: list[VolumeMount]
    disks: list[DiskMount]
    authorized: bool
    checkpoint_enabled: bool
    checkpoint_readiness_path: str | None
    checkpoint_readiness_port: int | None
    checkpoint_readiness_timeout_seconds: int
    checkpoint_readiness_interval_seconds: float
    health_check_path: str | None
    health_check_port: int | None
    tcp: bool
    ssh: bool | None
    block_network: bool
    allow_list: list[str] | None
    docker_enabled: bool
    preemptible: bool | None
    role: PodRole | None
    root_disk_bytes: int | None
    region: str | None
    availability_zone: str
    machine: MachineInput
    metadata: dict[str, Any]


@dataclass(slots=True)
class PodInstance:
    container_id: str
    stub_id: str = ""
    url: str = ""
    timeout_seconds: int = 0
    expires_at: datetime | None = None
    _client: WorkloadsClient | None = field(default=None, repr=False)

    def terminate(self) -> bool:
        if self._client is None:
            msg = "a workloads client is required to terminate a pod instance"
            raise PodOperationError(msg)
        try:
            self._client.api.stop_container(self._client.workspace, UUID(self.container_id))
        except SdkError as exc:
            raise PodOperationError(str(exc)) from exc
        return True


@dataclass(frozen=True, slots=True)
class ContainerAttachment:
    """What an attached container wrote and how it ended."""

    container_id: str
    output: str
    exit_code: int | None
    stop_reason: StopReason | None
    exit_message: str | None = None


@dataclass(slots=True)
class Container:
    container_id: str
    client: WorkloadsClient | None = None
    workspace: str | None = None
    timeout_seconds: float = 10.0
    terminal: Terminal | None = None

    @property
    def control_client(self) -> WorkloadsClient:
        if self.client is None:
            self.client = workloads_client(
                resolve_control_client_config(
                    workspace=self.workspace, timeout_seconds=self.timeout_seconds
                )
            )
        return self.client

    def attach(
        self,
        *,
        container_id: str | None = None,
        sync_dir: str | None = None,
        hide_logs: bool = False,
    ) -> ContainerAttachment:
        """Print the container's output until it stops, syncing `sync_dir` into it meanwhile."""
        terminal = self.terminal or Terminal()
        try:
            return attach_container(
                self.control_client,
                UUID(container_id or self.container_id),
                write=None if hide_logs else terminal.write,
                sync_dir=sync_dir,
            )
        except PodOperationError:
            raise
        except (SdkError, ValueError) as exc:
            raise PodOperationError(str(exc)) from exc


def attach_container(
    client: WorkloadsClient,
    container_id: UUID,
    *,
    write: Callable[[str], None] | None,
    sync_dir: str | None = None,
) -> ContainerAttachment:
    """Follow a container's output until it stops and return how it ended.

    A pod or sandbox container's output is its command's; any other
    container's is the output of the tasks it ran.
    """
    from lazycloud.abstractions.workspace_sync import ContainerWorkspaceSyncer

    container = client.api.get_container(client.workspace, container_id)
    syncer = (
        ContainerWorkspaceSyncer(container_id=str(container_id), local_dir=sync_dir, client=client)
        if sync_dir
        else None
    )
    output: list[str] = []
    try:
        if syncer is not None:
            syncer.sync_once()
            syncer.start()
        for line in _container_lines(client, container):
            if syncer is not None:
                syncer.raise_if_failed()
            output.append(line)
            if write is not None:
                write(line)
    finally:
        if syncer is not None:
            syncer.stop()
    if syncer is not None:
        syncer.raise_if_failed()
    finished = client.api.get_container(client.workspace, container_id)
    if finished.state is not ContainerState.stopped:
        raise PodOperationError("container attach stream ended before the container completed")
    return ContainerAttachment(
        container_id=str(container_id),
        output="".join(output),
        exit_code=finished.exit_code,
        stop_reason=finished.stop_reason,
        exit_message=finished.exit_message,
    )


def _container_lines(client: WorkloadsClient, container: ApiContainer) -> Iterator[str]:
    if container.kind in {WorkloadKind.pod, WorkloadKind.sandbox}:
        entries: Iterator[Any] = follow_log_stream(
            lambda after: client.stream_output(container.id, after=after, follow=True)
        )
    else:
        entries = follow_log_stream(
            lambda after: client.api.stream_container_logs(
                client.workspace, container.id, after=after, follow=True
            )
        )
    for entry in entries:
        yield entry.data if entry.data.endswith("\n") else f"{entry.data}\n"


@dataclass(slots=True)
class Pod:
    _app_slug: str = field(repr=False)
    name: str = "pod"
    image: Image = field(default_factory=Image)
    command: list[str] = field(default_factory=list)
    ports: dict[str, int] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    cpu: CpuRequest | None = 1.0
    memory: MemoryRequest | None = "128Mi"
    disk: str | None = None
    gpu: GpuInput = None
    gpu_count: int = 0
    keep_warm: int | None = 600
    secrets: list[str] = field(default_factory=list)
    volumes: list[VolumeMount] = field(default_factory=list)
    disks: list[DiskMount] = field(default_factory=list)
    authorized: bool = False
    checkpoint_enabled: bool = False
    checkpoint_readiness_path: str | None = None
    checkpoint_readiness_port: int | None = None
    checkpoint_readiness_timeout_seconds: int = 600
    checkpoint_readiness_interval_seconds: float = 1.0
    health_check_path: str | None = None
    health_check_port: int | None = None
    tcp: bool = False
    ssh: bool | None = False
    block_network: bool = False
    allow_list: list[str] | None = None
    docker_enabled: bool = False
    preemptible: bool | None = DEFAULT_WORKLOAD_PREEMPTIBLE
    role: PodRole | None = None
    root_disk_bytes: int | None = None
    region: str | None = None
    availability_zone: str = ""
    machine: MachineInput = None
    metadata: dict[str, Any] = field(default_factory=dict)
    stub_id: str = field(default="", init=False)
    client: WorkloadsClient | None = field(default=None, init=False, repr=False)
    workspace: str | None = field(default=None, init=False)
    terminal: Terminal | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.volumes = volume_mounts(self.volumes)
        self.disks = disk_mounts(self.disks)
        self.image.ignore_python = True
        if self.checkpoint_enabled and (
            not (self.checkpoint_readiness_path or "").startswith("/")
            or not self.checkpoint_readiness_port
        ):
            raise ValueError(
                "checkpoint_enabled Pods require checkpoint_readiness_path and "
                "checkpoint_readiness_port"
            )
        if self.health_check_port and not self.health_check_path:
            raise ValueError("health_check_port needs a health_check_path to request")
        if self.health_check_path and not self.health_check_path.startswith("/"):
            raise ValueError("health_check_path must be absolute")

    @property
    def resource_name(self) -> str:
        return self.name

    def spec(self) -> DeploymentSpec:
        return DeploymentSpec(
            name=self.name,
            kind=DeploymentKind.Pod,
            role=self.role,
            root_disk_bytes=self.root_disk_bytes,
            image=self.image.spec(),
            resources=Resources(
                region=ProductRegion(self.region) if self.region is not None else None,
                availability_zone=self.availability_zone,
                cpu=self.cpu,
                memory=self.memory,
                disk=self.disk or DEFAULT_DISK,
                gpu=list(gpu_preference(self.gpu)),
                gpu_count=self.gpu_count,
                keep_warm=self.keep_warm,
                preemptible=self.preemptible,
            ),
            command=self.command,
            ports=self.ports,
            env=self.env,
            secrets=self.secrets,
            volumes=self.volumes,
            disks=self.disks,
            metadata=build_resource_metadata(
                app=self._app_slug,
                authorized=self.authorized,
                checkpoint_enabled=self.checkpoint_enabled,
                tcp=self.tcp,
                ssh=self.ssh,
                block_network=self.block_network,
                allow_list=self.allow_list,
                docker_enabled=self.docker_enabled,
                machine=self.machine,
                extra={
                    **self.metadata,
                    "checkpoint_readiness_path": self.checkpoint_readiness_path or "",
                    "checkpoint_readiness_port": self.checkpoint_readiness_port or 0,
                    "checkpoint_readiness_timeout_seconds": (
                        self.checkpoint_readiness_timeout_seconds
                    ),
                    "checkpoint_readiness_interval_seconds": (
                        self.checkpoint_readiness_interval_seconds
                    ),
                    "health_check_path": self.health_check_path or "",
                    "health_check_port": self.health_check_port or 0,
                },
            ),
        )

    @property
    def is_devbox(self) -> bool:
        return self.role is PodRole.Devbox

    def handler_reference(self) -> None:
        """A pod runs its command, not a handler."""
        return None

    def unsupported_options(self) -> list[str]:
        """Declared options the platform cannot run yet, by name."""
        return container_unsupported_options(self.volumes)

    def require_supported(self) -> None:
        from lazycloud.exceptions import UnsupportedFeatureError

        unsupported = self.unsupported_options()
        if unsupported:
            label = "devbox" if self.is_devbox else "pod"
            raise UnsupportedFeatureError(f"{label} {self.name}", unsupported)

    def workload_spec(
        self, *, handler: object = None, source_sha256: str, image: ImageBuildResult
    ) -> WorkloadSpec:
        """The API definition of this pod for an uploaded source and a ready image."""
        del handler
        self.require_supported()
        # Only the options set are sent, so the server's defaults apply.
        pod = PodSpec(kind=PodKind.devbox if self.is_devbox else PodKind.pod)
        if self.command:
            pod.command = [CommandItem(item) for item in self.command]
        if self.ports:
            pod.ports = dict(self.ports)
        if self.tcp:
            pod.tcp = True
        if self.ssh:
            pod.ssh = True
        if self.health_check_path:
            pod.health_check = HealthCheck(path=self.health_check_path)
            if self.health_check_port:
                pod.health_check.port = self.health_check_port
        disks = [
            DiskMountSpec(name=disk.name, size_bytes=disk.size_bytes, mount_path=disk.mount_path)
            for disk in self.disks
        ]
        if self.is_devbox and self.root_disk_bytes is not None:
            disks.insert(
                0,
                DiskMountSpec(
                    name=self.name, size_bytes=self.root_disk_bytes, mount_path=DISK_ROOT_MOUNT_PATH
                ),
            )
        keep_warm = self.keep_warm
        if keep_warm is None and self.is_devbox:
            keep_warm = DEVBOX_KEEP_WARM_SECONDS
        preemptible = self.preemptible
        if preemptible is None:
            preemptible = not self.is_devbox
        checkpoint = (
            CheckpointSpec(
                readiness_path=self.checkpoint_readiness_path,
                readiness_port=self.checkpoint_readiness_port,
                readiness_timeout_seconds=self.checkpoint_readiness_timeout_seconds,
                readiness_interval_seconds=self.checkpoint_readiness_interval_seconds,
            )
            if self.checkpoint_enabled
            else None
        )
        return container_workload_spec(
            self,
            label="devbox" if self.is_devbox else "pod",
            pod=pod,
            source_sha256=source_sha256,
            image=image,
            keep_warm=keep_warm,
            preemptible=preemptible,
            block_network=self.block_network,
            allow_list=self.allow_list,
            disks=disks,
            checkpoint=checkpoint,
        )

    def prepare(
        self, *, workspace: str | None = None, source_root: str | Path | None = None
    ) -> str:
        """Upload the working tree and return the id of the release that runs this pod."""
        from lazycloud.session.deployment import prepare_release

        client = self._client(workspace)
        try:
            release = prepare_release(
                self,
                client=client.api,
                workspace=client.workspace,
                source_root=source_root,
                terminal=self.terminal,
            )
        except SdkError as exc:
            raise PodOperationError(str(exc)) from exc
        self.stub_id = str(release.id)
        return self.stub_id

    def create(
        self,
        command: list[str] | None = None,
        *,
        stub_id: str | None = None,
        workspace: str | None = None,
        timeout_seconds: int | None = None,
    ) -> PodInstance:
        """Start a container of this pod outside its deployment's count."""
        selected_stub_id = stub_id or self.stub_id or self.prepare(workspace=workspace)
        client = self._client(workspace)
        fields: dict[str, Any] = {"release_id": selected_stub_id}
        if command:
            fields["command"] = list(command)
        lifetime = self.keep_warm if timeout_seconds is None else timeout_seconds
        if lifetime is not None:
            fields["timeout_seconds"] = lifetime
        try:
            instance = client.create_instance(CreateInstanceRequest.model_validate(fields))
        except (SdkError, ValidationError) as exc:
            raise PodOperationError(str(exc)) from exc
        return PodInstance(
            container_id=str(instance.id),
            stub_id=str(instance.release_id),
            url=instance.url or "",
            timeout_seconds=instance.timeout_seconds or 0,
            expires_at=instance.expires_at,
            _client=client,
        )

    def run(
        self,
        *command: str,
        workspace: str | None = None,
        timeout_seconds: int | None = None,
    ) -> PodInstance:
        return self.create(
            list(command) or None,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
        )

    def shell(
        self,
        *,
        workspace: str | None = None,
        container_id: str | None = None,
        sync_dir: str | None = None,
    ) -> ShellSession:
        from lazycloud.abstractions.shell import Shell

        shell = Shell(client=self._client(workspace))
        if container_id:
            return shell.create_existing(container_id, sync_dir=sync_dir)
        return shell.create_standalone(
            self.stub_id or self.prepare(workspace=workspace), sync_dir=sync_dir
        )

    def deploy(
        self,
        *,
        workspace: str | None = None,
        source_root: str | Path | None = None,
    ) -> Deployment:
        """Deploy this pod into its app without touching the app's other workloads."""
        from lazycloud.session.deployment import AppFunctions, deploy_functions

        client = self._client(workspace)
        try:
            deployment = deploy_functions(
                [AppFunctions(app=self._app_slug, functions=(self,))],
                client=client.api,
                workspace=client.workspace,
                source_root=source_root,
                terminal=self.terminal,
            )[0]
        except SdkError as exc:
            raise PodOperationError(str(exc)) from exc
        release = next(item for item in deployment.releases if item.name == self.name)
        self.stub_id = str(release.id)
        return deployment

    def pause(self, *, version: int | None = None, workspace: str | None = None) -> Workload:
        """Stop the deployed pod; `version` must be its active version."""
        return self._deployments("stop", version, workspace)

    def resume(self, *, version: int | None = None, workspace: str | None = None) -> Workload:
        """Start the deployed pod again, making `version` active first when given."""
        return self._deployments("start", version, workspace)

    def scale(
        self, containers: int, *, version: int | None = None, workspace: str | None = None
    ) -> Workload:
        """Hold the deployed pod at `containers` containers until the next scale."""
        return self._deployments("scale", version, workspace, containers)

    def delete(self, *, version: int | None = None, workspace: str | None = None) -> None:
        """Delete the deployed pod and every version of it."""
        self._deployments("delete", version, workspace)

    def _deployments(
        self, action: str, version: int | None, workspace: str | None, *args: int
    ) -> Workload:
        from lazycloud.session.deployment import DeploymentClient

        client = self._client(workspace)
        deployments = DeploymentClient(workspace=client.workspace, client=client.api)
        reference = f"{self.name}-v{version}" if version is not None else self.name
        operation: Callable[..., Workload] = getattr(deployments, action)
        try:
            return operation(reference, *args, app=self._app_slug)
        except SdkError as exc:
            raise PodOperationError(str(exc)) from exc

    def _client(self, workspace: str | None) -> WorkloadsClient:
        selected = workspace or self.workspace
        if self.client is None or (selected is not None and selected != self.client.workspace):
            self.client = workloads_client(resolve_control_client_config(workspace=selected))
        return self.client


def container_unsupported_options(volumes: list[VolumeMount]) -> list[str]:
    """Declared pod or sandbox options the platform cannot run yet, by name."""
    declared = {
        # Hosts have no credentials of their own for a user's bucket.
        "cloud bucket without key secrets": any(
            volume.config is not None and volume.config.get("auth_mode") != "secret_references"
            for volume in volumes
        ),
    }
    return [name for name, present in declared.items() if present]


def container_workload_spec(
    owner: Any,
    *,
    label: str,
    pod: PodSpec,
    source_sha256: str,
    image: ImageBuildResult,
    keep_warm: int | None,
    preemptible: bool,
    block_network: bool,
    allow_list: list[str] | None,
    disks: list[DiskMountSpec] | None = None,
    checkpoint: CheckpointSpec | None = None,
) -> WorkloadSpec:
    """The API definition of a pod, devbox or sandbox: a command, not a handler."""
    from lazycloud.abstractions.function import _resources, _volume_spec

    if block_network:
        pod.block_network = True
    if allow_list is not None:
        pod.allow_list = [AllowListItem(item) for item in allow_list]
    spec: dict[str, Any] = {
        "kind": WorkloadKind.sandbox if pod.kind is PodKind.sandbox else WorkloadKind.pod,
        "name": owner.resource_name,
        "source": {"sha256": source_sha256},
        "image": {
            "python_version": python_minor_version(image.python_version),
            "image_id": image.image_id,
        },
        "resources": _resources(owner.cpu, owner.memory, owner.disk),
        "authorized": bool(owner.authorized),
        "pod": pod,
    }
    if keep_warm is not None:
        spec["keep_warm_seconds"] = keep_warm
    if owner.env:
        spec["environment"] = dict(owner.env)
    if owner.metadata:
        spec["metadata"] = dict(owner.metadata)
    if owner.secrets:
        spec["secrets"] = list(dict.fromkeys(owner.secrets))
    if owner.volumes:
        spec["volumes"] = [_volume_spec(volume) for volume in owner.volumes]
    if disks:
        spec["disks"] = disks
    if owner.docker_enabled:
        spec["docker_enabled"] = True
    if checkpoint is not None:
        spec["checkpoint"] = checkpoint
    try:
        gpu = gpu_preference(owner.gpu)
    except (TypeError, ValueError) as exc:
        raise PodOperationError(
            f"{label} {owner.resource_name} has invalid options: {exc}"
        ) from exc
    if gpu:
        spec["resources"]["gpu"] = list(gpu)
    if owner.gpu_count:
        spec["resources"]["gpu_count"] = owner.gpu_count
    placement: dict[str, Any] = {}
    if owner.machine:
        placement["machine"] = owner.machine
    if owner.region is not None:
        placement["region"] = owner.region
    if owner.availability_zone:
        placement["availability_zone"] = owner.availability_zone
    if not preemptible:
        placement["preemptible"] = False
    if placement:
        spec["placement"] = placement
    try:
        return WorkloadSpec.model_validate(spec)
    except ValidationError as exc:
        raise PodOperationError(
            f"{label} {owner.resource_name} has invalid options: {exc}"
        ) from exc


__all__ = [
    "Container",
    "ContainerAttachment",
    "Pod",
    "PodInstance",
    "PodOperationError",
    "PodOptions",
    "attach_container",
]
