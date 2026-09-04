from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TypedDict

from shared.deployment_records import (
    DEFAULT_DISK,
    CpuRequest,
    DeploymentSpec,
    MemoryRequest,
    Resources,
    VolumeMount,
)
from shared.deployments import DeploymentKind
from shared.gpu import GpuInput, gpu_preference
from shared.http.compute import ContainerResponse
from shared.http.deployments import DeploymentResponse
from shared.http.gateway import (
    AttachToContainerResponse,
    DeployStubResponse,
    SyncContainerWorkspaceBody,
    SyncContainerWorkspaceResponse,
)
from shared.http.pods import CreatePodRequest, CreatePodResponse
from shared.placement import ProductRegion
from typing_extensions import Self

from lazycloud.abstractions.image import Image
from lazycloud.abstractions.metadata import PoolInput, build_resource_metadata
from lazycloud.abstractions.serve import sync_local_workspace
from lazycloud.abstractions.shell import Shell, ShellSession
from lazycloud.abstractions.volume import volume_mounts
from lazycloud.control import ControlClientConfigMixin, resolve_control_client_config
from lazycloud.control_clients import (
    gateway_control_client,
    pod_control_client,
    resource_control_client,
)
from lazycloud.session.deployment import (
    DeploymentClient,
    DeploymentControlClient,
    DeploymentResourceClient,
)
from lazycloud.terminal import Terminal


class PodClient(Protocol):
    def create_pod(self, request: CreatePodRequest) -> CreatePodResponse: ...


class PodContainerClient(Protocol):
    def stop_container(self, container_id: str) -> ContainerResponse: ...


class ContainerAttachClient(Protocol):
    def attach_to_container_events(
        self,
        container_id: str,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> Iterator[AttachToContainerResponse]: ...

    def sync_container_workspace(
        self,
        body: SyncContainerWorkspaceBody,
    ) -> SyncContainerWorkspaceResponse: ...


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
    keep_warm: int
    secrets: list[str]
    volumes: list[VolumeMount]
    authorized: bool
    checkpoint_enabled: bool
    checkpoint_readiness_path: str | None
    checkpoint_readiness_port: int | None
    checkpoint_readiness_timeout_seconds: int
    checkpoint_readiness_interval_seconds: float
    health_check_path: str | None
    health_check_port: int | None
    tcp: bool
    block_network: bool
    allow_list: list[str] | None
    docker_enabled: bool
    preemptible: bool
    region: str | None
    pool: PoolInput
    provider: str | None
    metadata: dict[str, Any]


@dataclass(slots=True)
class PodInstance:
    container_id: str
    stub_id: str = ""
    url: str = ""
    timeout_seconds: int = 0
    expires_at: datetime | None = None
    _container_client: PodContainerClient | None = field(default=None, repr=False)

    def terminate(self) -> bool:
        if self._container_client is None:
            msg = "container lifecycle client is required to terminate a pod instance"
            raise PodOperationError(msg)
        try:
            self._container_client.stop_container(self.container_id)
        except RuntimeError as exc:
            raise PodOperationError(str(exc)) from exc
        return True


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
        selected_container_id = container_id or self.container_id
        if sync_dir:
            sync_local_workspace(
                container_id=selected_container_id,
                local_dir=sync_dir,
                gateway_client=self.control_client,
            )
        output: list[str] = []
        terminal: AttachToContainerResponse | None = None
        try:
            for response in self.control_client.attach_to_container_events(selected_container_id):
                if response.error_msg:
                    raise PodOperationError(response.error_msg)
                if response.output:
                    output.append(response.output)
                    if not hide_logs:
                        (self.terminal or Terminal()).write(response.output)
                if response.done:
                    terminal = response
                    break
        except RuntimeError as exc:
            if isinstance(exc, PodOperationError):
                raise
            raise PodOperationError(str(exc)) from exc
        if terminal is None:
            raise PodOperationError("container attach stream ended before the container completed")
        return terminal.model_copy(update={"output": "".join(output)})


@dataclass(slots=True)
class Pod(ControlClientConfigMixin):
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
    keep_warm: int = 600
    secrets: list[str] = field(default_factory=list)
    volumes: list[VolumeMount] = field(default_factory=list)
    authorized: bool = False
    checkpoint_enabled: bool = False
    checkpoint_readiness_path: str | None = None
    checkpoint_readiness_port: int | None = None
    checkpoint_readiness_timeout_seconds: int = 600
    checkpoint_readiness_interval_seconds: float = 1.0
    health_check_path: str | None = None
    health_check_port: int | None = None
    tcp: bool = False
    block_network: bool = False
    allow_list: list[str] | None = None
    docker_enabled: bool = False
    preemptible: bool = False
    region: str | None = None
    pool: PoolInput = None
    provider: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    stub_id: str = field(default="", init=False)
    deployment_id: str = field(default="", init=False)
    image_id: str | None = field(default=None, init=False)
    checkpoint_id: str | None = field(default=None, init=False)
    client: PodClient | None = field(default=None, init=False, repr=False)
    container_client: PodContainerClient | None = field(default=None, init=False, repr=False)
    deployment_client: DeploymentControlClient | None = field(
        default=None,
        init=False,
        repr=False,
    )
    deployment_resource_client: DeploymentResourceClient | None = field(
        default=None,
        init=False,
        repr=False,
    )
    workspace: str | None = field(default=None, init=False)
    endpoint: str | None = field(default=None, init=False)
    token: str | None = field(default=None, init=False, repr=False)
    timeout_seconds: float = field(default=10.0, init=False)
    terminal: Terminal | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.volumes = volume_mounts(self.volumes)
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
    def control_client(self) -> PodClient:
        if self.client is None:
            config = resolve_control_client_config(
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
            )
            self.client = pod_control_client(config)
        return self.client

    @property
    def lifecycle_client(self) -> PodContainerClient:
        if self.container_client is None:
            self.container_client = resource_control_client(self._config())
        return self.container_client

    def spec(self) -> DeploymentSpec:
        return DeploymentSpec(
            name=self.name,
            kind=DeploymentKind.Pod,
            image=self.image.spec(),
            resources=Resources(
                region=ProductRegion(self.region) if self.region is not None else None,
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
            metadata=build_resource_metadata(
                app=self._app_slug,
                authorized=self.authorized,
                checkpoint_enabled=self.checkpoint_enabled,
                tcp=self.tcp,
                block_network=self.block_network,
                allow_list=self.allow_list,
                docker_enabled=self.docker_enabled,
                pool=self.pool,
                provider=self.provider,
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

    def configure(
        self,
        *,
        image: Image | None = None,
        command: list[str] | None = None,
        ports: dict[str, int] | None = None,
        env: dict[str, str] | None = None,
        cpu: CpuRequest | None = None,
        memory: MemoryRequest | None = None,
        disk: str | None = None,
        gpu: GpuInput = None,
        gpu_count: int | None = None,
        keep_warm: int | None = None,
        secrets: list[str] | None = None,
        tcp: bool | None = None,
        region: str | None = None,
        pool: PoolInput = None,
        preemptible: bool | None = None,
    ) -> Self:
        if image is not None:
            image.ignore_python = True
            self.image = image
        if command:
            self.command = list(command)
        if ports:
            self.ports.update(ports)
        if env:
            self.env.update(env)
        if cpu is not None:
            self.cpu = cpu
        if memory is not None:
            self.memory = memory
        if disk is not None:
            self.disk = disk
        if gpu is not None:
            self.gpu = gpu
        if gpu_count is not None:
            self.gpu_count = gpu_count
        if keep_warm is not None:
            self.keep_warm = keep_warm
        if secrets:
            self.secrets.extend(secret for secret in secrets if secret not in self.secrets)
        if tcp is not None:
            self.tcp = tcp
        if pool is not None:
            self.pool = pool
        if region is not None:
            self.region = region
        if preemptible is not None:
            self.preemptible = preemptible
        return self

    def prepare(self, *, workspace: str | None = None) -> str:
        try:
            response = DeploymentClient(
                client=self.deployment_client,
                resource_client=self.deployment_resource_client,
                workspace=workspace or self.workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
                sync_source=True,
                terminal=self.terminal,
            ).prepare(self.spec(), workspace=workspace or self.workspace, image=self.image)
        except RuntimeError as exc:
            raise PodOperationError(str(exc)) from exc
        if not response.stub_id:
            msg = "deployment prepare did not return a pod stub_id"
            raise PodOperationError(msg)
        self.stub_id = response.stub_id
        return self.stub_id

    def create(
        self,
        command: list[str] | None = None,
        *,
        stub_id: str | None = None,
        workspace: str | None = None,
        timeout_seconds: int | None = None,
    ) -> PodInstance:
        selected_stub_id = stub_id or self.stub_id or self.prepare(workspace=workspace)
        return _create_pod_instance(
            self.control_client,
            stub_id=selected_stub_id,
            image_id=self.image_id,
            checkpoint_id=self.checkpoint_id,
            command=command,
            timeout_seconds=(self.keep_warm if timeout_seconds is None else timeout_seconds),
            external_url=self._config().endpoint,
            container_client=self.lifecycle_client,
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
        shell = Shell(
            workspace=workspace or self.workspace,
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.timeout_seconds,
        )
        if container_id:
            session = shell.create_existing(container_id)
        else:
            session = shell.create_standalone(self.stub_id or self.prepare(workspace=workspace))
        if sync_dir:
            sync_local_workspace(
                container_id=session.container_id,
                local_dir=sync_dir,
                gateway_client=gateway_control_client(self._config()),
            )
        return session

    def deploy(
        self,
        *,
        name: str | None = None,
        workspace: str | None = None,
        external_url: str | None = None,
        source_root: str | Path | None = None,
    ) -> DeployStubResponse:
        if name is not None:
            self.name = name
        try:
            response = DeploymentClient(
                client=self.deployment_client,
                workspace=workspace or self.workspace,
                endpoint=self.endpoint,
                token=self.token,
                timeout_seconds=self.timeout_seconds,
                sync_source=True,
                terminal=self.terminal,
            ).create(
                self.spec(),
                workspace=workspace or self.workspace,
                external_url=external_url,
                image=self.image,
                source_root=source_root,
            )
        except RuntimeError as exc:
            raise PodOperationError(str(exc)) from exc
        self.stub_id = response.stub_id or self.stub_id
        self.deployment_id = response.deployment_id or self.deployment_id
        return response

    def pause(
        self,
        *,
        version: int | None = None,
        workspace: str | None = None,
    ) -> DeploymentResponse:
        deployment, selected_workspace = self._deployment_target(
            version=version,
            workspace=workspace,
        )
        try:
            return deployment.stop(self.deployment_id, workspace=selected_workspace)
        except RuntimeError as exc:
            raise PodOperationError(str(exc)) from exc

    def resume(
        self,
        *,
        version: int | None = None,
        workspace: str | None = None,
    ) -> DeploymentResponse:
        deployment, selected_workspace = self._deployment_target(
            version=version,
            workspace=workspace,
        )
        try:
            return deployment.start(self.deployment_id, workspace=selected_workspace)
        except RuntimeError as exc:
            raise PodOperationError(str(exc)) from exc

    def scale(
        self,
        containers: int,
        *,
        version: int | None = None,
        workspace: str | None = None,
    ) -> DeploymentResponse:
        deployment, selected_workspace = self._deployment_target(
            version=version,
            workspace=workspace,
        )
        try:
            return deployment.scale(
                self.deployment_id,
                containers,
                workspace=selected_workspace,
            )
        except RuntimeError as exc:
            raise PodOperationError(str(exc)) from exc

    def delete(
        self,
        *,
        version: int | None = None,
        workspace: str | None = None,
    ) -> None:
        deployment, selected_workspace = self._deployment_target(
            version=version,
            workspace=workspace,
        )
        try:
            deployment.delete(self.deployment_id, workspace=selected_workspace)
        except RuntimeError as exc:
            raise PodOperationError(str(exc)) from exc

    def _deployment_target(
        self,
        *,
        version: int | None,
        workspace: str | None,
    ) -> tuple[DeploymentClient, str]:
        selected_workspace = workspace or self.workspace or self._config().workspace
        deployment = DeploymentClient(
            client=self.deployment_client,
            resource_client=self.deployment_resource_client,
            workspace=selected_workspace,
            endpoint=self.endpoint,
            token=self.token,
            timeout_seconds=self.timeout_seconds,
        )
        try:
            target = deployment.resolve_target(
                kind=DeploymentKind.Pod,
                name=self.name,
                app=self._app_slug,
                deployment_version=version,
                workspace=selected_workspace,
            )
        except RuntimeError as exc:
            raise PodOperationError(str(exc)) from exc
        if not target.deployment_id:
            msg = f"pod deployment target did not return an id: {self.name}"
            raise PodOperationError(msg)
        self.stub_id = target.stub_id or self.stub_id
        self.deployment_id = target.deployment_id
        return deployment, selected_workspace


def _create_pod_instance(
    client: PodClient,
    *,
    stub_id: str,
    image_id: str | None = None,
    checkpoint_id: str | None = None,
    command: list[str] | None = None,
    timeout_seconds: int | None = None,
    external_url: str = "",
    container_client: PodContainerClient | None = None,
) -> PodInstance:
    if not stub_id:
        msg = "stub_id is required to create a pod through the control API"
        raise PodOperationError(msg)
    try:
        response = client.create_pod(
            CreatePodRequest(
                stub_id=stub_id,
                image_id=image_id,
                checkpoint_id=checkpoint_id,
                command=command,
                timeout_seconds=timeout_seconds,
                external_url=external_url,
            )
        )
    except RuntimeError as exc:
        raise PodOperationError(str(exc)) from exc
    return PodInstance(
        container_id=response.container_id,
        stub_id=response.stub_id or stub_id,
        url=response.url,
        timeout_seconds=response.timeout_seconds,
        expires_at=response.expires_at,
        _container_client=container_client,
    )


__all__ = [
    "Container",
    "Pod",
    "PodClient",
    "PodContainerClient",
    "PodInstance",
    "PodOperationError",
    "PodOptions",
]
