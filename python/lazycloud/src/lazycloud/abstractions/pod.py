from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, TypedDict

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
from shared.disks import DiskMount
from shared.gpu import GpuInput, gpu_preference
from shared.http.gateway import (
    AttachToContainerResponse,
)
from shared.placement import ProductRegion

from lazycloud.abstractions.disk import disk_mounts
from lazycloud.abstractions.image import Image
from lazycloud.abstractions.metadata import MachineInput, build_resource_metadata
from lazycloud.abstractions.volume import volume_mounts
from lazycloud.control import resolve_control_client_config
from lazycloud.exceptions import UnsupportedFeatureError
from lazycloud.terminal import Terminal

if TYPE_CHECKING:
    from shared.http.workspace_sync import WorkspaceSyncBatch, WorkspaceSyncResponse


class ContainerAttachClient(Protocol):
    def attach_to_container_events(
        self,
        container_id: str,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> Iterator[AttachToContainerResponse]: ...

    def sync_container_workspace(
        self,
        body: WorkspaceSyncBatch,
    ) -> WorkspaceSyncResponse: ...


class PodOperationError(RuntimeError):
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
class Container:
    container_id: str
    client: ContainerAttachClient | None = None
    endpoint: str | None = None
    token: str | None = None
    timeout_seconds: float = 10.0
    terminal: Terminal | None = None

    @property
    def control_client(self) -> ContainerAttachClient:
        from lazycloud.control_clients import gateway_control_client

        if self.client is None:
            config = resolve_control_client_config(
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            self.client = gateway_control_client(config)
        return self.client

    def attach(
        self,
        *,
        container_id: str | None = None,
        sync_dir: str | None = None,
        hide_logs: bool = False,
    ) -> AttachToContainerResponse:
        from lazycloud.abstractions.workspace_sync import ContainerWorkspaceSyncer

        selected_container_id = container_id or self.container_id
        syncer = (
            ContainerWorkspaceSyncer(
                container_id=selected_container_id,
                local_dir=sync_dir,
                gateway_client=self.control_client,
            )
            if sync_dir
            else None
        )
        output: list[str] = []
        terminal: AttachToContainerResponse | None = None
        try:
            try:
                if syncer is not None:
                    syncer.sync_once()
                    syncer.start()
                for response in self.control_client.attach_to_container_events(
                    selected_container_id
                ):
                    if syncer is not None:
                        syncer.raise_if_failed()
                    if response.error_msg:
                        raise PodOperationError(response.error_msg)
                    if response.output:
                        output.append(response.output)
                        if not hide_logs:
                            (self.terminal or Terminal()).write(response.output)
                    if response.done:
                        terminal = response
                        break
            finally:
                if syncer is not None:
                    syncer.stop()
            if syncer is not None:
                syncer.raise_if_failed()
        except RuntimeError as exc:
            if isinstance(exc, PodOperationError):
                raise
            raise PodOperationError(str(exc)) from exc
        if terminal is None:
            raise PodOperationError("container attach stream ended before the container completed")
        return terminal.model_copy(update={"output": "".join(output)})


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

    def deploy(self, **_: object) -> NoReturn:
        raise self._unsupported()

    def create(self, *_: object, **__: object) -> NoReturn:
        raise self._unsupported()

    def run(self, *_: object, **__: object) -> NoReturn:
        raise self._unsupported()

    def shell(self, **_: object) -> NoReturn:
        raise self._unsupported()

    def _unsupported(self) -> UnsupportedFeatureError:
        if self.role is PodRole.Devbox:
            return UnsupportedFeatureError(f"devbox {self.name}", ["devboxes"])
        return UnsupportedFeatureError(f"pod {self.name}", ["pods"])


__all__ = [
    "Container",
    "Pod",
    "PodOperationError",
    "PodOptions",
]
