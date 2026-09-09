from __future__ import annotations

import asyncio
import secrets
import socket
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode
from uuid import uuid4

from control.service import ControlPlaneService, StubKind
from coordination.redis_client import RedisClient
from database.records.apps import StubRecord
from database.repositories.execution import PodExecutionRepository
from database.repositories.images import CheckpointRepository
from database.repositories.orchestration import ContainerRepository
from database.types import DatabaseSession
from shared.app_identity import POD_IMAGE
from shared.autoscaling import PodStubType
from shared.checkpoints import CheckpointRecord, CheckpointStatus
from shared.container_requests import (
    WORKER_USER_CODE_VOLUME,
    RuntimeContainerStatus,
    StopContainerReason,
    WorkerStartupKind,
)
from shared.containers import (
    LIVE_CONTAINER_STATUSES,
    TERMINAL_CONTAINER_STATUSES,
    ContainerRecord,
    ContainerStatus,
)
from shared.errors import ConflictError, InvalidInputError, NotFoundError, UpstreamUnavailableError
from shared.events import EventLevel
from shared.http.pods import (
    CreatePodRequest,
    CreatePodResponse,
    PodFileSearchMatch,
    PodSandboxConnectResponse,
    PodSandboxCreateDirectoryRequest,
    PodSandboxCreateDirectoryResponse,
    PodSandboxCreateImageFromFilesystemRequest,
    PodSandboxCreateImageFromFilesystemResponse,
    PodSandboxDeleteDirectoryResponse,
    PodSandboxDeleteFileResponse,
    PodSandboxDownloadFileResponse,
    PodSandboxExecRequest,
    PodSandboxExecResponse,
    PodSandboxExposePortRequest,
    PodSandboxExposePortResponse,
    PodSandboxFileInfo,
    PodSandboxFindInFilesRequest,
    PodSandboxFindInFilesResponse,
    PodSandboxKillRequest,
    PodSandboxKillResponse,
    PodSandboxListFilesResponse,
    PodSandboxListProcessesResponse,
    PodSandboxListUrlsResponse,
    PodSandboxProcessInfo,
    PodSandboxReplaceInFilesRequest,
    PodSandboxReplaceInFilesResponse,
    PodSandboxSnapshotMemoryRequest,
    PodSandboxSnapshotMemoryResponse,
    PodSandboxStatFileResponse,
    PodSandboxStatusResponse,
    PodSandboxStderrResponse,
    PodSandboxStdoutResponse,
    PodSandboxUpdateNetworkPermissionsRequest,
    PodSandboxUpdateNetworkPermissionsResponse,
    PodSandboxUpdateTTLRequest,
    PodSandboxUpdateTTLResponse,
    PodSandboxUploadFileBody,
    PodSandboxUploadFileResponse,
)
from shared.http.workspace_changes import WorkspaceChangeType
from shared.paths import DEFAULT_SANDBOX_WORKDIR
from shared.routing import AgentBackendRoute, BackendRouteState, parse_backend_route_address
from shared.scheduling import (
    ContainerSchedulingDirectory,
    SchedulerContainerAddressMap,
    SchedulerContainerState,
    SchedulerContainerStatus,
    WorkerContainerState,
    gpu_count_for_capacity,
)
from shared.tasks import TaskStatus
from shared.timestamps import utc_now
from shared.urls import pod_proxy_url
from shared.workload_keys import pod_keep_warm_lock_key

from database import AsyncDatabaseClient
from execution.checkpoints import latest_available_checkpoint
from execution.config import env_sequence_mapping
from execution.container_clients import (
    ContainerClientHandle,
    ContainerOperationResponse,
    ContainerSandboxFileInfo,
    PodContainerControlClient,
    SchedulerContainerClientFactory,
)
from execution.containers.planning import ContainerSchedulingOptions
from execution.containers.readiness import AsyncContainerReadiness
from execution.mounts import (
    container_resource_mounts,
    container_resource_mounts_require_workspace_storage,
)
from execution.pods.config import PodStubConfig
from execution.pods.planning import (
    DEFAULT_POD_CONNECTION_TIMEOUT_SECONDS,
    POD_CONTAINER_DISCOVERY_INTERVAL_MS,
    PodBackendContainer,
    PodContainerStartRequest,
    PodProxyFailureReason,
    PodProxyProtocol,
    PodProxyRequest,
    plan_pod_container_start,
    plan_pod_proxy,
)
from execution.pods.proxy import (
    DEFAULT_POD_PROXY_TIMEOUT_SECONDS,
    PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS,
    AsyncPodProxyForwardClient,
    PodProxyConnectionRepository,
    PodProxyHttpRequest,
    PodProxyPortUnavailable,
    PodProxyResponseStream,
    PodProxySession,
    PodProxySocketClient,
    PodProxyTarget,
    PodProxyUnavailable,
)
from execution.services import ExecutionServices


class AsyncPodSchedulerContainerDirectory(Protocol):
    async def get_container_state(self, container_id: str) -> SchedulerContainerState | None: ...

    async def list_by_stub(self, stub_id: str) -> list[SchedulerContainerState]: ...

    async def get_container_address_map(
        self,
        container_id: str,
    ) -> SchedulerContainerAddressMap: ...

    async def get_container_address_maps(
        self,
        container_ids: Sequence[str],
    ) -> dict[str, SchedulerContainerAddressMap]: ...


@dataclass(slots=True)
class PodControlService:
    services: ExecutionServices
    redis: RedisClient
    gateway_http_url: str = "http://127.0.0.1:9000"
    scheduler_containers: ContainerSchedulingDirectory | None = None
    container_clients: SchedulerContainerClientFactory[PodContainerControlClient] | None = None
    async_database: AsyncDatabaseClient | None = None
    async_scheduler_containers: AsyncPodSchedulerContainerDirectory | None = None
    async_pod_proxy_http_client: AsyncPodProxyForwardClient | None = None
    pod_proxy_socket_client: PodProxySocketClient | None = None
    pod_proxy_connections: PodProxyConnectionRepository | None = None
    container_readiness_probe: AsyncContainerReadiness | None = None
    container_connect_timeout_seconds: float = DEFAULT_POD_CONNECTION_TIMEOUT_SECONDS
    pod_proxy_start_timeout_seconds: float = DEFAULT_POD_PROXY_TIMEOUT_SECONDS
    poll_interval_seconds: float = POD_CONTAINER_DISCOVERY_INTERVAL_MS / 1000
    control_plane: ControlPlaneService = field(init=False)

    def __post_init__(self) -> None:
        self.control_plane = ControlPlaneService(self.services.context)
        if self.scheduler_containers is None:
            candidate = getattr(self.services.containers, "scheduler_containers", None)
            if isinstance(candidate, ContainerSchedulingDirectory):
                self.scheduler_containers = candidate

    def create_pod(
        self,
        request: CreatePodRequest,
        *,
        authorized_workspace_id: str | None = None,
    ) -> CreatePodResponse:
        requested_checkpoint = self._checkpoint_for_create(
            request.checkpoint_id,
            authorized_workspace_id=authorized_workspace_id,
        )
        stub_id = request.stub_id or (
            requested_checkpoint.stub_id if requested_checkpoint is not None else ""
        )
        if not stub_id:
            raise InvalidInputError("stub_id is required when checkpoint_id is not set")
        if (
            requested_checkpoint is not None
            and request.stub_id
            and request.stub_id != requested_checkpoint.stub_id
        ):
            raise InvalidInputError("checkpoint does not belong to the requested sandbox stub")
        stub = self.control_plane.get_stub(stub_id)
        if stub.kind not in {StubKind.Pod, StubKind.Sandbox}:
            raise InvalidInputError(f"stub is not runnable as a pod: {stub.id}")
        if authorized_workspace_id is not None and stub.workspace_id != authorized_workspace_id:
            raise NotFoundError(f"stub not found: {stub.id}")
        if requested_checkpoint is not None and stub.kind is not StubKind.Sandbox:
            raise InvalidInputError("memory checkpoints can only restore Sandbox workloads")
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        config = PodStubConfig.model_validate(stub.config, from_attributes=True)
        checkpoint = requested_checkpoint
        if checkpoint is None and stub.kind is StubKind.Pod and config.runtime.checkpoint_enabled:
            checkpoint = latest_available_checkpoint(
                self.services.context,
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
            )
        stub_type = (
            PodStubType.Sandbox if stub.kind is StubKind.Sandbox else PodStubType.PodDeployment
        )
        timeout_seconds = (
            request.timeout_seconds
            if request.timeout_seconds is not None
            else config.runtime.keep_warm
        )
        created_at = utc_now()
        ports = config.exposed_ports
        plan = plan_pod_container_start(
            PodContainerStartRequest(
                workspace_name=workspace.name,
                workspace_id=stub.workspace_id,
                app_id=stub.app_id or "",
                stub_id=stub.id,
                stub_type=stub_type,
                gateway_token=secrets.token_urlsafe(24),
                keep_warm_seconds=timeout_seconds,
                container_id=str(uuid4()),
                entrypoint=(
                    list(request.command)
                    if request.command is not None
                    else config.effective_entrypoint
                ),
                cpu_millicores=config.runtime.requested_cpu_millicores,
                cpu_limit_millicores=config.runtime.limit_cpu_millicores,
                memory_mib=config.runtime.requested_memory_mib,
                memory_limit_mib=config.runtime.limit_memory_mib,
                disk_mib=config.runtime.requested_disk_mib,
                requires_gpu=config.runtime.gpu_required,
                gpu_count=config.runtime.gpu_count,
                gpu=list(config.runtime.gpu),
                image_id=request.image_id or config.effective_image_id,
                env=config.env_list,
                secret_env=[],
                ports=ports,
                checkpoint_enabled=bool(request.checkpoint_id or config.runtime.checkpoint_enabled),
            )
        )
        env = env_sequence_mapping(plan.env)
        if request.checkpoint_id:
            env["CHECKPOINT_ID"] = request.checkpoint_id
        with self.services.context.database.session() as session:
            gpu = self.services.containers.admit_container_start(
                session,
                workspace_id=stub.workspace_id,
                gpu=plan.gpu,
                gpu_count=plan.gpu_count,
                region=config.runtime.region,
                availability_zone=config.runtime.availability_zone,
                stub_id=stub.id,
            )
            container = ContainerRecord(
                id=plan.container_id,
                name=f"{'sandbox' if stub.kind is StubKind.Sandbox else 'pod'}-{stub.name}",
                image=plan.image_id or POD_IMAGE,
                command=list(plan.entrypoint),
                workspace_id=stub.workspace_id,
                stub_id=stub.id,
                app_id=stub.app_id,
                status=ContainerStatus.Pending,
                env=env,
                ports={str(port): port for port in plan.ports},
                network_blocked=config.runtime.block_network,
                network_allow_list=list(config.runtime.allow_list),
                gpu=gpu,
                gpu_count=gpu_count_for_capacity(gpu, plan.gpu_count),
                timeout_seconds=timeout_seconds,
                expires_at=(
                    created_at + timedelta(seconds=timeout_seconds) if timeout_seconds > 0 else None
                ),
                created_at=created_at,
            )
            ContainerRepository(session).records.upsert(
                container,
                key=container.id,
                workspace_id=container.workspace_id,
                name=container.name,
                status=container.status.value,
            )
        self.services.containers.publish_lifecycle_change(
            container,
            WorkspaceChangeType.Created,
        )
        resource_mounts = container_resource_mounts(
            context=self.services.context,
            object_storage=self.services.object_storage,
            workspace_id=stub.workspace_id,
            workspace_name=workspace.name,
            object_id=config.object_id,
            stub_id=stub.id,
            container_id=container.id,
            volumes=config.volume_inputs,
        )
        if plan.keep_warm_lock_key is not None:
            self._set_keep_warm_lock(
                plan.keep_warm_lock_key,
                ttl_seconds=plan.keep_warm_lock_ttl_seconds,
            )
        try:
            submitted = self.services.containers.submit_scheduler_request(
                container,
                ContainerSchedulingOptions(
                    workspace_name=workspace.name,
                    stub_type=stub_type.value,
                    startup_kind=(
                        WorkerStartupKind.Sandbox
                        if stub.kind is StubKind.Sandbox
                        else WorkerStartupKind.Pod
                    ),
                    entrypoint=plan.entrypoint,
                    cwd=(
                        DEFAULT_SANDBOX_WORKDIR
                        if stub.kind is StubKind.Sandbox
                        else WORKER_USER_CODE_VOLUME
                    ),
                    env=env,
                    env_list=plan.env,
                    image_id=plan.image_id or container.image,
                    app_id=stub.app_id or "",
                    deployment_id=stub.deployment_id or "",
                    ports=plan.ports,
                    requested_ports=plan.ports,
                    checkpoint_exposed_ports=(
                        checkpoint.exposed_ports if checkpoint is not None else []
                    ),
                    checkpoint_id=checkpoint.checkpoint_id if checkpoint is not None else "",
                    checkpoint_enabled=(
                        plan.checkpoint_enabled and stub.kind is not StubKind.Sandbox
                    ),
                    checkpoint_readiness_path=config.runtime.checkpoint_readiness_path,
                    checkpoint_readiness_port=config.runtime.checkpoint_readiness_port,
                    checkpoint_readiness_timeout_seconds=(
                        config.runtime.checkpoint_readiness_timeout_seconds
                    ),
                    checkpoint_readiness_interval_seconds=(
                        config.runtime.checkpoint_readiness_interval_seconds
                    ),
                    cpu_millicores=plan.cpu_millicores,
                    cpu_limit_millicores=plan.cpu_limit_millicores,
                    memory_mib=plan.memory_mib,
                    memory_limit_mib=plan.memory_limit_mib,
                    disk_mib=plan.disk_mib,
                    gpu=list(container.gpu),
                    gpu_count=container.gpu_count,
                    pool_selector=config.runtime.pool_selector or "",
                    region=config.runtime.region,
                    availability_zone=config.runtime.availability_zone,
                    runtime=config.runtime.runtime,
                    runtime_class=config.runtime.runtime_class or "",
                    docker_enabled=config.runtime.docker_enabled,
                    block_network=config.runtime.block_network,
                    allow_list=config.runtime.allow_list,
                    preemptible=config.runtime.preemptible,
                    workspace_gpu_quota=config.runtime.workspace_gpu_quota,
                    workspace_cpu_quota_millicores=config.runtime.workspace_cpu_quota_millicores,
                    secret_names=config.secrets,
                    gateway_token_required=True,
                    workspace_storage_required=(
                        container_resource_mounts_require_workspace_storage(
                            context=self.services.context,
                            workspace_id=stub.workspace_id,
                            mounts=resource_mounts,
                        )
                    ),
                    mounts=resource_mounts,
                ),
            )
        except Exception:
            if plan.keep_warm_lock_key is not None:
                self._delete_keep_warm_lock(plan.keep_warm_lock_key)
            raise
        if not submitted.accepted:
            if plan.keep_warm_lock_key is not None:
                self._delete_keep_warm_lock(plan.keep_warm_lock_key)
            reason = submitted.reason or f"failed to schedule pod for {stub.name}"
            container.status = ContainerStatus.Failed
            container.exit_code = 1
            container.finished_at = utc_now()
            with self.services.context.database.session() as session:
                ContainerRepository(session).records.upsert(
                    container,
                    key=container.id,
                    workspace_id=container.workspace_id,
                    name=container.name,
                    status=container.status.value,
                )
            self.services.containers.publish_lifecycle_change(
                container,
                WorkspaceChangeType.Updated,
            )
            self.services.events.emit(
                "pod.schedule.failed",
                level=EventLevel.Error,
                resource_type="container",
                resource_id=container.id,
                message=reason,
                data={"stub_id": stub.id, "stub_type": stub_type.value},
                workspace_id=container.workspace_id,
            )
            raise UpstreamUnavailableError(reason)

        self.services.events.emit(
            "pod.scheduled",
            level=EventLevel.Info,
            resource_type="container",
            resource_id=container.id,
            message=f"scheduled pod container for {stub.name}",
            data={
                "stub_id": stub.id,
                "stub_type": stub_type.value,
                "keep_warm_lock_key": plan.keep_warm_lock_key,
                "scheduler_status": submitted.status.value,
            },
            workspace_id=container.workspace_id,
        )
        url = ""
        if request.external_url and ports and stub.kind is not StubKind.Sandbox:
            url = self.control_plane.stub_url(
                stub.id,
                apps=self.services.apps,
                workspace=stub.workspace_id,
                external_url=request.external_url,
                port=ports[0],
            ).url
        return CreatePodResponse(
            container_id=container.id,
            stub_id=stub.id,
            url=url,
            timeout_seconds=timeout_seconds,
            expires_at=container.expires_at,
        )

    def _checkpoint_for_create(
        self,
        checkpoint_id: str | None,
        *,
        authorized_workspace_id: str | None,
    ) -> CheckpointRecord | None:
        if not checkpoint_id:
            return None
        with self.services.context.database.session() as session:
            repository = CheckpointRepository(session)
            checkpoint = (
                repository.get(checkpoint_id, workspace_id=authorized_workspace_id)
                if authorized_workspace_id is not None
                else repository.get_across_workspaces(checkpoint_id)
            )
        if checkpoint is None:
            raise NotFoundError(f"checkpoint not found: {checkpoint_id}")
        if checkpoint.status is not CheckpointStatus.Available:
            raise ConflictError(
                f"checkpoint is not available for restore: {checkpoint.status.value}"
            )
        if not checkpoint.stub_id:
            raise ConflictError("checkpoint does not identify its source sandbox stub")
        return checkpoint

    def expire_pods(self, *, now: datetime | None = None) -> list[ContainerRecord]:
        current_time = now or utc_now()
        with self.services.context.database.session() as session:
            expired = ContainerRepository(session).expired_containers_across_workspaces(
                now=current_time,
                stub_types=(StubKind.Pod.value,),
            )
        stopped: list[ContainerRecord] = []
        for container in expired:
            if not container.stub_id:
                continue
            stub = self.control_plane.get_stub(container.stub_id)
            # Named, because the default is `User` and settles the invocations this
            # container held as cancellations: the caller is told they stopped work
            # they did not stop, and charged the attempt. A TTL is the platform's
            # own deadline, so the work is released and runs somewhere else.
            record = self.services.containers.stop(container.id, reason=StopContainerReason.Ttl)
            self._delete_keep_warm_lock(
                pod_keep_warm_lock_key(container.workspace_id, stub.id, container.id)
            )
            stopped.append(record)
            self.services.events.emit(
                "pod.expired",
                level=EventLevel.Info,
                resource_type="container",
                resource_id=record.id,
                message=f"expired pod container {record.name}",
                data={
                    "stub_id": record.stub_id or "",
                    "timeout_seconds": record.timeout_seconds,
                    "expires_at": (
                        record.expires_at.isoformat() if record.expires_at is not None else None
                    ),
                },
                workspace_id=record.workspace_id,
            )
        return stopped

    def sandbox_exec(
        self, container_id: str, request: PodSandboxExecRequest
    ) -> PodSandboxExecResponse:
        response = self._client(container_id).sandbox_exec(
            container_id,
            request.command,
            env=request.env,
            cwd=request.cwd or ".",
        )
        _raise_container_response_error(response)
        self.services.events.emit(
            "pod.exec",
            level=EventLevel.Info,
            resource_type="container",
            resource_id=container_id,
            message=f"requested pod command {request.command}",
            data={"pid": response.pid},
            workspace_id=self._container(container_id).workspace_id,
        )
        return PodSandboxExecResponse(pid=response.pid)

    def sandbox_status(self, container_id: str, pid: int) -> PodSandboxStatusResponse:
        if pid == 0:
            return self._container_status_response(container_id)
        response = self._client(container_id).sandbox_status(container_id, pid)
        _raise_container_response_error(response)
        return PodSandboxStatusResponse(
            status=response.status,
            exit_code=response.exit_code,
        )

    def sandbox_stdout(self, container_id: str, pid: int) -> PodSandboxStdoutResponse:
        response = self._client(container_id).sandbox_stdout(container_id, pid)
        _raise_container_response_error(response)
        return PodSandboxStdoutResponse(stdout=response.stdout)

    def sandbox_stderr(self, container_id: str, pid: int) -> PodSandboxStderrResponse:
        response = self._client(container_id).sandbox_stderr(container_id, pid)
        _raise_container_response_error(response)
        return PodSandboxStderrResponse(stderr=response.stderr)

    def sandbox_kill(
        self, container_id: str, request: PodSandboxKillRequest
    ) -> PodSandboxKillResponse:
        response = self._client(container_id).sandbox_kill(
            container_id,
            request.pid,
        )
        _raise_container_response_error(response)
        return PodSandboxKillResponse()

    def sandbox_upload_file(
        self,
        container_id: str,
        request: PodSandboxUploadFileBody,
    ) -> PodSandboxUploadFileResponse:
        response = self._client(container_id).sandbox_upload_file(
            container_id,
            request.container_path,
            request.data,
            mode=request.mode,
        )
        _raise_container_response_error(response)
        return PodSandboxUploadFileResponse()

    def sandbox_download_file(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxDownloadFileResponse:
        response = self._client(container_id).sandbox_download_file(
            container_id,
            container_path,
        )
        _raise_container_response_error(response)
        return PodSandboxDownloadFileResponse.from_bytes(response.data)

    def sandbox_stat_file(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxStatFileResponse:
        response = self._client(container_id).sandbox_stat_file(
            container_id,
            container_path,
        )
        _raise_container_response_error(response)
        return PodSandboxStatFileResponse(file_info=_file_info(response.file_info))

    def sandbox_list_files(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxListFilesResponse:
        response = self._client(container_id).sandbox_list_files(
            container_id,
            container_path,
        )
        _raise_container_response_error(response)
        return PodSandboxListFilesResponse(
            files=[_file_info(item) for item in response.files],
        )

    def sandbox_delete_file(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxDeleteFileResponse:
        response = self._client(container_id).sandbox_delete_file(
            container_id,
            container_path,
        )
        _raise_container_response_error(response)
        return PodSandboxDeleteFileResponse()

    def sandbox_create_directory(
        self,
        container_id: str,
        request: PodSandboxCreateDirectoryRequest,
    ) -> PodSandboxCreateDirectoryResponse:
        response = self._client(container_id).sandbox_create_directory(
            container_id,
            request.container_path,
            mode=request.mode,
        )
        _raise_container_response_error(response)
        return PodSandboxCreateDirectoryResponse()

    def sandbox_delete_directory(
        self,
        container_id: str,
        container_path: str,
    ) -> PodSandboxDeleteDirectoryResponse:
        response = self._client(container_id).sandbox_delete_directory(
            container_id,
            container_path,
        )
        _raise_container_response_error(response)
        return PodSandboxDeleteDirectoryResponse()

    def sandbox_replace_in_files(
        self,
        container_id: str,
        request: PodSandboxReplaceInFilesRequest,
    ) -> PodSandboxReplaceInFilesResponse:
        response = self._client(container_id).sandbox_replace_in_files(
            container_id,
            request.container_path,
            request.pattern,
            request.new_string,
        )
        _raise_container_response_error(response)
        return PodSandboxReplaceInFilesResponse()

    def sandbox_find_in_files(
        self,
        container_id: str,
        request: PodSandboxFindInFilesRequest,
    ) -> PodSandboxFindInFilesResponse:
        response = self._client(container_id).sandbox_find_in_files(
            container_id,
            request.container_path,
            request.pattern,
        )
        _raise_container_response_error(response)
        return PodSandboxFindInFilesResponse(
            results=[
                PodFileSearchMatch(
                    path=item.path,
                    text=item.text,
                    line=item.line,
                    column=item.column,
                )
                for item in response.results
            ],
        )

    def sandbox_expose_port(
        self,
        container_id: str,
        request: PodSandboxExposePortRequest,
    ) -> PodSandboxExposePortResponse:
        validated_container, stub = self._sandbox_container(container_id)
        if not validated_container.stub_id:
            raise InvalidInputError("sandbox container has no owning stub")
        response = self._client(container_id).sandbox_expose_port(
            container_id,
            request.port,
        )
        _raise_container_response_error(response)
        container, current_stub = self._sandbox_container(container_id)
        if current_stub.id != stub.id or current_stub.workspace_id != stub.workspace_id:
            raise ConflictError("sandbox container ownership changed during port exposure")
        url = pod_proxy_url(
            self.gateway_http_url,
            resource=stub.kind,
            stub_id=stub.id,
            port=request.port,
            container_id=container.id,
        )
        self._store_url(container_id, request.port, url)
        container.ports[str(request.port)] = request.port
        with self.services.context.database.session() as session:
            ContainerRepository(session).upsert(container)
        self.services.containers.publish_lifecycle_change(
            container,
            WorkspaceChangeType.Updated,
        )
        return PodSandboxExposePortResponse(url=url)

    def sandbox_update_network_permissions(
        self,
        container_id: str,
        request: PodSandboxUpdateNetworkPermissionsRequest,
    ) -> PodSandboxUpdateNetworkPermissionsResponse:
        response = self._client(container_id).sandbox_update_network_permissions(
            container_id,
            block_network=request.block_network,
            allow_list=request.allow_list,
        )
        _raise_container_response_error(response)
        container = self._container(container_id)
        container.network_blocked = request.block_network
        container.network_allow_list = list(request.allow_list)
        with self.services.context.database.session() as session:
            ContainerRepository(session).upsert(container)
        self.services.containers.publish_lifecycle_change(
            container,
            WorkspaceChangeType.Updated,
        )
        return PodSandboxUpdateNetworkPermissionsResponse(
            block_network=container.network_blocked,
            allow_list=container.network_allow_list,
        )

    def sandbox_network_permissions(
        self,
        container_id: str,
    ) -> PodSandboxUpdateNetworkPermissionsResponse:
        container = self._container(container_id)
        return PodSandboxUpdateNetworkPermissionsResponse(
            block_network=container.network_blocked,
            allow_list=container.network_allow_list,
        )

    def sandbox_connect(self, container_id: str) -> PodSandboxConnectResponse:
        container = self._container(container_id)
        try:
            ready_client = self._wait_for_container_client(container, timeout_seconds=0)
        except RuntimeError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        client = (
            ready_client.client
            if ready_client is not None
            else self._container_client_factory().client_for(container).client
        )
        supervisor = client.sandbox_status(container.id, 0)
        _raise_container_response_error(supervisor)
        return PodSandboxConnectResponse(stub_id=container.stub_id or "")

    def sandbox_update_ttl(
        self,
        container_id: str,
        request: PodSandboxUpdateTTLRequest,
    ) -> PodSandboxUpdateTTLResponse:
        container, _stub = self._sandbox_container(container_id)
        container.timeout_seconds = request.ttl
        container.expires_at = (
            utc_now() + timedelta(seconds=request.ttl) if request.ttl > 0 else None
        )
        with self.services.context.database.session() as session:
            ContainerRepository(session).upsert(container)
        self.services.containers.publish_lifecycle_change(
            container,
            WorkspaceChangeType.Updated,
        )
        self._set_keep_warm_lock(
            pod_keep_warm_lock_key(container.workspace_id, container.stub_id or "", container.id),
            ttl_seconds=request.ttl if request.ttl > 0 else None,
        )
        return PodSandboxUpdateTTLResponse(
            ttl=container.timeout_seconds,
            expires_at=container.expires_at,
        )

    def sandbox_terminate(self, container_id: str) -> None:
        container, _stub = self._sandbox_container(container_id)
        stopped = self.services.containers.stop(
            container.id,
            reason=StopContainerReason.User,
        )
        self._delete_keep_warm_lock(
            pod_keep_warm_lock_key(container.workspace_id, container.stub_id or "", container.id)
        )
        self.services.events.emit(
            "sandbox.terminated",
            level=EventLevel.Info,
            resource_type="container",
            resource_id=stopped.id,
            message=f"terminated sandbox {stopped.name}",
            data={"stub_id": stopped.stub_id or ""},
            workspace_id=stopped.workspace_id,
        )

    def sandbox_create_image_from_filesystem(
        self,
        container_id: str,
        request: PodSandboxCreateImageFromFilesystemRequest,
    ) -> PodSandboxCreateImageFromFilesystemResponse:
        _ = request
        container = self._container(container_id)
        if not container.stub_id:
            raise InvalidInputError(f"container has no owning stub: {container_id}")
        image_id = f"image-{container.stub_id}-{uuid4().hex[:8]}"
        self._client(container_id).archive(
            container_id,
            image_id,
            lambda _: None,
        )
        return PodSandboxCreateImageFromFilesystemResponse(image_id=image_id)

    def sandbox_snapshot_memory(
        self,
        container_id: str,
        request: PodSandboxSnapshotMemoryRequest,
    ) -> PodSandboxSnapshotMemoryResponse:
        response = self._client(container_id).checkpoint(container_id)
        _raise_container_response_error(response)
        return PodSandboxSnapshotMemoryResponse(checkpoint_id=response.checkpoint_id)

    def sandbox_list_processes(
        self,
        container_id: str,
    ) -> PodSandboxListProcessesResponse:
        response = self._client(container_id).sandbox_list_processes(container_id)
        _raise_container_response_error(response)
        return PodSandboxListProcessesResponse(
            processes=[
                PodSandboxProcessInfo(pid=item.pid, command=item.command)
                for item in response.processes
            ],
        )

    def sandbox_list_urls(self, container_id: str) -> PodSandboxListUrlsResponse:
        self._sandbox_container(container_id)
        response = self._client(container_id).sandbox_list_exposed_ports(container_id)
        _raise_container_response_error(response)
        exposed_ports = set(response.ports)
        urls = {
            port: url
            for port, url in self._stored_urls(container_id).items()
            if port in exposed_ports
        }
        return PodSandboxListUrlsResponse(urls=urls)

    async def prepare_pod_proxy(
        self,
        *,
        stub_id: str,
        container_id: str | None = None,
        port: int,
        path: str,
        query_params: dict[str, list[str]],
        protocol: PodProxyProtocol,
    ) -> PodProxySession:
        stub = await self._async_database().run_transaction(
            lambda session: self.control_plane.get_stub_in_session(session, stub_id)
        )
        workspace_id = stub.workspace_id
        config = PodStubConfig.model_validate(stub.config, from_attributes=True)
        connections = self._pod_proxy_connections()
        request = PodProxyRequest(
            port=port,
            container_id=container_id,
            sub_path=path,
            query_string=_query_string(query_params),
            protocol=protocol,
        )
        demand_recorded = False
        try:
            if container_id is not None:
                target = await self._pinned_sandbox_proxy_target(stub, request)
            else:
                await connections.increment_total_connections(workspace_id, stub_id)
                demand_recorded = True
                target = await self._wait_for_pod_proxy_target(
                    stub,
                    request,
                    health_path=config.runtime.health_check_path,
                    health_port=config.runtime.health_check_port,
                )
            if not demand_recorded:
                await connections.increment_total_connections(workspace_id, stub_id)
                demand_recorded = True
            await connections.increment_container_connections(
                workspace_id,
                stub_id,
                target.container_id,
                keep_warm_seconds=(config.runtime.keep_warm if stub.kind is StubKind.Pod else None),
            )
        except Exception:
            if demand_recorded:
                await connections.decrement_total_connections(workspace_id, stub_id)
            raise
        return PodProxySession(
            workspace_id=workspace_id,
            stub_id=stub_id,
            target=target,
            keep_warm_seconds=(config.runtime.keep_warm if stub.kind is StubKind.Pod else None),
            pinned=container_id is not None,
        )

    async def open_pod_proxy_http_stream(
        self,
        session: PodProxySession,
        request: PodProxyHttpRequest,
    ) -> PodProxyResponseStream:
        return await self._async_pod_proxy_http_client().open_stream(
            session.target,
            request,
            timeout_seconds=self.pod_proxy_start_timeout_seconds,
            connect_timeout_seconds=(
                PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS if session.pinned else None
            ),
        )

    async def open_pod_proxy_socket(self, session: PodProxySession) -> socket.socket:
        try:
            return await asyncio.to_thread(
                self._pod_proxy_socket_client().open_socket,
                session.target,
                timeout_seconds=(
                    PINNED_SANDBOX_CONNECT_TIMEOUT_SECONDS
                    if session.pinned
                    else self.pod_proxy_start_timeout_seconds
                ),
            )
        except (OSError, RuntimeError) as exc:
            if session.pinned:
                raise PodProxyUnavailable("sandbox backend is unavailable") from exc
            raise

    async def finish_pod_proxy(self, session: PodProxySession) -> None:
        async with session.finalization() as should_finalize:
            if not should_finalize:
                return
            connections = self._pod_proxy_connections()
            await asyncio.gather(
                connections.decrement_container_connections(
                    session.workspace_id,
                    session.stub_id,
                    session.target.container_id,
                    keep_warm_seconds=session.keep_warm_seconds,
                ),
                connections.decrement_total_connections(
                    session.workspace_id,
                    session.stub_id,
                ),
            )

    async def _wait_for_pod_proxy_target(
        self,
        stub: StubRecord,
        request: PodProxyRequest,
        *,
        health_path: str,
        health_port: int,
    ) -> PodProxyTarget:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self.pod_proxy_start_timeout_seconds, 0.0)
        while True:
            try:
                return await self._pod_proxy_target(
                    stub,
                    request,
                    health_path=health_path,
                    health_port=health_port,
                )
            except PodProxyPortUnavailable:
                raise
            except PodProxyUnavailable:
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(max(self.poll_interval_seconds, 0.0))

    def _container(self, container_id: str) -> ContainerRecord:
        try:
            return self.services.containers.get(container_id)
        except NotFoundError as exc:
            raise NotFoundError("container not found") from exc

    def _sandbox_container(
        self,
        container_id: str,
    ) -> tuple[ContainerRecord, StubRecord]:
        container = self._container(container_id)
        if not container.stub_id:
            raise InvalidInputError(f"container is not a sandbox: {container_id}")
        stub = self.control_plane.get_stub(container.stub_id, workspace=container.workspace_id)
        if stub.workspace_id != container.workspace_id or stub.kind is not StubKind.Sandbox:
            raise InvalidInputError(f"container is not a sandbox: {container_id}")
        return container, stub

    def _set_keep_warm_lock(self, key: str, *, ttl_seconds: int | None) -> None:
        self.redis.set(
            self.redis.key(key),
            "1",
            ex=ttl_seconds if ttl_seconds is not None and ttl_seconds > 0 else None,
        )

    def _delete_keep_warm_lock(self, key: str) -> None:
        self.redis.delete(self.redis.key(key))

    def _client(self, container_id: str) -> PodContainerControlClient:
        try:
            container = self._container(container_id)
        except NotFoundError:
            raise
        try:
            ready_client = self._wait_for_container_client(container)
        except RuntimeError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        if ready_client is not None:
            return ready_client.client
        return self._container_client_factory().client_for(container).client

    def _wait_for_container_client(
        self,
        container: ContainerRecord,
        *,
        timeout_seconds: float | None = None,
    ) -> ContainerClientHandle[PodContainerControlClient] | None:
        if self.scheduler_containers is None:
            return None
        wait_seconds = (
            self.container_connect_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        deadline = time.monotonic() + max(wait_seconds, 0.0)
        last_reason = "container is not ready"
        while True:
            container = self._container(container.id)
            if container.status in TERMINAL_CONTAINER_STATUSES:
                raise ConflictError(f"container {container.id} is {container.status.value}")
            state = self.scheduler_containers.get_container_state(container.id)
            if state is None:
                last_reason = f"scheduler state not found for container {container.id}"
            elif state.workspace_id != container.workspace_id:
                msg = "container scheduler state workspace mismatch"
                raise RuntimeError(msg)
            elif state.status in {
                SchedulerContainerStatus.Complete,
                SchedulerContainerStatus.Failed,
                SchedulerContainerStatus.Stopping,
            }:
                msg = f"container {container.id} is {state.status.value}"
                raise ConflictError(msg)
            elif state.status is SchedulerContainerStatus.Running:
                self._mark_container_running(container, state)
                worker_address = self.scheduler_containers.get_worker_address(container.id)
                if worker_address is not None and worker_address.address:
                    try:
                        ready_client = self._container_client_factory().client_for(container)
                        status = ready_client.client.status(container.id)
                    except Exception as exc:
                        last_reason = f"container service not ready: {type(exc).__name__}: {exc}"
                        if time.monotonic() >= deadline:
                            msg = last_reason
                            raise RuntimeError(msg) from exc
                        time.sleep(max(self.poll_interval_seconds, 0.0))
                        continue
                    if status.ok and status.status == RuntimeContainerStatus.Running.value:
                        return ready_client
                    if status.status in {
                        RuntimeContainerStatus.Stopped.value,
                        RuntimeContainerStatus.Unknown.value,
                    }:
                        last_reason = (
                            status.error_msg
                            or f"container {container.id} runtime is {status.status}"
                        )
                    else:
                        last_reason = f"container {container.id} runtime is {status.status}"
                    if not status.ok and status.error_msg:
                        last_reason = status.error_msg
                    if time.monotonic() >= deadline:
                        msg = last_reason
                        raise RuntimeError(msg)
                    time.sleep(max(self.poll_interval_seconds, 0.0))
                    continue
                last_reason = f"worker address not published for container {container.id}"
            else:
                last_reason = f"container {container.id} is {state.status.value}"

            if time.monotonic() >= deadline:
                msg = last_reason
                raise RuntimeError(msg)
            time.sleep(max(self.poll_interval_seconds, 0.0))

    def _mark_container_running(
        self,
        container: ContainerRecord,
        state: WorkerContainerState,
    ) -> None:
        assigned_worker_id = state.worker_id or container.runtime_worker_id
        assigned_machine_id = container.runtime_machine_id
        if (
            container.status is ContainerStatus.Running
            and container.runtime_worker_id == assigned_worker_id
            and container.runtime_machine_id == assigned_machine_id
        ):
            return
        container.status = ContainerStatus.Running
        container.runtime_worker_id = assigned_worker_id
        container.runtime_machine_id = assigned_machine_id
        container.started_at = container.started_at or state.started_at or utc_now()
        if container.timeout_seconds > 0:
            container.expires_at = container.started_at + timedelta(
                seconds=container.timeout_seconds
            )
        with self.services.context.database.session() as session:
            ContainerRepository(session).records.upsert(
                container,
                key=container.id,
                workspace_id=container.workspace_id,
                name=container.name,
                status=container.status.value,
            )
        self.services.containers.publish_lifecycle_change(
            container,
            WorkspaceChangeType.Updated,
        )

    async def _pod_proxy_target(
        self,
        stub: StubRecord,
        request: PodProxyRequest,
        *,
        health_path: str,
        health_port: int,
    ) -> PodProxyTarget:
        containers, targets = await self._pod_backend_containers(
            workspace_id=stub.workspace_id,
            stub_id=stub.id,
            port=request.port,
            health_path=health_path,
            health_port=health_port,
        )
        plan = plan_pod_proxy(request, containers)
        if plan.failure_reason is PodProxyFailureReason.NoAvailableContainers:
            msg = f"no running pod containers for stub {stub.id}"
            raise PodProxyUnavailable(msg)
        if plan.failure_reason is PodProxyFailureReason.PortUnavailable:
            msg = f"port {request.port} is not available for stub {stub.id}"
            raise PodProxyPortUnavailable(msg)
        if not plan.container_id:
            msg = f"no pod proxy target selected for stub {stub.id}"
            raise PodProxyUnavailable(msg)
        target = targets.get(plan.container_id)
        if target is None:
            msg = f"pod proxy target disappeared for container {plan.container_id}"
            raise PodProxyUnavailable(msg)
        return target

    async def _pinned_sandbox_proxy_target(
        self,
        stub: StubRecord,
        request: PodProxyRequest,
    ) -> PodProxyTarget:
        container_id = request.container_id
        if stub.kind is not StubKind.Sandbox or container_id is None:
            raise PodProxyUnavailable("sandbox container is unavailable")
        container, port_is_exposed = await self._async_database().run_transaction(
            lambda session: self._pinned_sandbox_in_session(
                session,
                container_id=container_id,
                workspace_id=stub.workspace_id,
                port=request.port,
            )
        )
        if (
            container is None
            or container.workspace_id != stub.workspace_id
            or container.stub_id != stub.id
            or container.status is not ContainerStatus.Running
        ):
            raise PodProxyUnavailable("sandbox container is unavailable")
        if not port_is_exposed:
            raise PodProxyPortUnavailable("sandbox port is not exposed")
        scheduler = self._async_scheduler_container_directory()
        state, address_map = await asyncio.gather(
            scheduler.get_container_state(container.id),
            scheduler.get_container_address_map(container.id),
        )
        if (
            state is None
            or state.workspace_id != container.workspace_id
            or state.stub_id != stub.id
            or state.status is not SchedulerContainerStatus.Running
        ):
            raise PodProxyUnavailable("sandbox container is unavailable")
        if address_map.container_id != container.id:
            raise PodProxyUnavailable("sandbox address ownership is invalid")
        address = address_map.address_map.get(request.port, "")
        if not address:
            raise PodProxyPortUnavailable("sandbox port is not exposed")
        route_id = _owned_route_id_for_port(
            address_map.routes,
            address=address,
            port=request.port,
            container_id=container.id,
            workspace_id=container.workspace_id,
        )
        return PodProxyTarget(
            container_id=container.id,
            address=address,
            route_id=route_id,
        )

    async def _pod_backend_containers(
        self,
        *,
        workspace_id: str,
        stub_id: str,
        port: int,
        health_path: str,
        health_port: int,
    ) -> tuple[list[PodBackendContainer], dict[str, PodProxyTarget]]:
        scheduler = self._async_scheduler_container_directory()
        records, scheduler_states = await asyncio.gather(
            self._async_database().run_transaction(
                lambda session: ContainerRepository(session).list(
                    workspace_id=workspace_id,
                    statuses=tuple(status.value for status in LIVE_CONTAINER_STATUSES),
                    stub_ids=(stub_id,),
                )
            ),
            scheduler.list_by_stub(stub_id),
        )
        states = {state.container_id: state for state in scheduler_states}
        address_maps = await scheduler.get_container_address_maps(
            [container.id for container in records]
        )
        targets: dict[str, PodProxyTarget] = {}
        probe_port = health_port or port
        candidates: list[tuple[str, dict[int, str], PodProxyTarget]] = []
        for container in records:
            state = states.get(container.id)
            if state is None:
                continue
            if (
                state.workspace_id != container.workspace_id
                or state.stub_id != stub_id
                or state.status is not SchedulerContainerStatus.Running
            ):
                continue
            address_map_record = address_maps[container.id]
            address_map = dict(address_map_record.address_map)
            if not address_map:
                continue
            routes = address_map_record.routes
            probe_target = PodProxyTarget(
                container_id=container.id,
                address=address_map.get(probe_port, ""),
                route_id=_route_id_for_port(routes, probe_port),
            )
            candidates.append((container.id, address_map, probe_target))
            if probe_port == port:
                target = probe_target
            else:
                target = PodProxyTarget(
                    container_id=container.id,
                    address=address_map.get(port, ""),
                    route_id=_route_id_for_port(routes, port),
                )
            if target.address:
                targets[container.id] = target

        # Probing in series would make a stub's slowest unreachable backend set the
        # latency of every request that had a healthy one to go to.
        #
        # A container with no address on the probe port cannot be asked. What that
        # means depends on which port went missing. When the probe port is the
        # requested one, the container is already excluded by the port check that
        # follows, and calling it unready as well would turn "this port is not
        # exposed", answerable at once, into "nothing is serving yet", which the
        # caller waits out the whole start timeout before hearing. When a declared
        # health port is the one missing, nothing else excludes it, so an
        # unaskable container is unready rather than silently routed to unprobed.
        unprobed_verdict = probe_port == port
        readiness = await self._probe_candidates(
            [target for _id, _map, target in candidates if target.address],
            stub_id=stub_id,
            port=probe_port,
            health_path=health_path,
        )
        # Only a ready container is ever balanced across, so a count for one that
        # is not costs a Redis round trip nobody reads, and during a cold start
        # that is every container, every 250ms poll.
        ready_container_ids = [
            container_id
            for container_id, _address_map, _target in candidates
            if readiness.get(container_id, unprobed_verdict)
        ]
        active_counts = await asyncio.gather(
            *(
                self._pod_proxy_connections().container_connections(
                    workspace_id,
                    stub_id,
                    container_id,
                )
                for container_id in ready_container_ids
            )
        )
        counts_by_container = dict(zip(ready_container_ids, active_counts, strict=True))
        backend_containers: list[PodBackendContainer] = []
        for container_id, address_map, _target in candidates:
            ready = readiness.get(container_id, unprobed_verdict)
            backend_containers.append(
                PodBackendContainer(
                    container_id=container_id,
                    address_map=address_map,
                    active_connections=counts_by_container.get(container_id, 0),
                    ready=ready,
                )
            )
        return backend_containers, targets

    async def _probe_candidates(
        self,
        targets: list[PodProxyTarget],
        *,
        stub_id: str,
        port: int,
        health_path: str,
    ) -> dict[str, bool]:
        async def probe(target: PodProxyTarget) -> tuple[str, bool]:
            return (
                target.container_id,
                await self._container_readiness().is_ready(
                    container_id=target.container_id,
                    stub_id=stub_id,
                    address=target.address,
                    route_id=target.route_id,
                    port=port,
                    health_path=health_path,
                ),
            )

        if not targets:
            return {}
        return dict(await asyncio.gather(*(probe(target) for target in targets)))

    def _container_readiness(self) -> AsyncContainerReadiness:
        if self.container_readiness_probe is None:
            msg = "container readiness probe is not configured"
            raise RuntimeError(msg)
        return self.container_readiness_probe

    def _async_database(self) -> AsyncDatabaseClient:
        if self.async_database is None:
            msg = "pod proxy asynchronous database is not configured"
            raise RuntimeError(msg)
        return self.async_database

    def _async_scheduler_container_directory(self) -> AsyncPodSchedulerContainerDirectory:
        if self.async_scheduler_containers is None:
            msg = "pod proxy asynchronous scheduler state is not configured"
            raise RuntimeError(msg)
        return self.async_scheduler_containers

    def _async_pod_proxy_http_client(self) -> AsyncPodProxyForwardClient:
        if self.async_pod_proxy_http_client is None:
            msg = "pod proxy asynchronous HTTP client is not configured"
            raise RuntimeError(msg)
        return self.async_pod_proxy_http_client

    @staticmethod
    def _pinned_sandbox_in_session(
        session: DatabaseSession,
        *,
        container_id: str,
        workspace_id: str,
        port: int,
    ) -> tuple[ContainerRecord | None, bool]:
        container = ContainerRepository(session).get(container_id, workspace_id=workspace_id)
        exposure = PodExecutionRepository(session).urls.get(
            container_id=container_id,
            port=port,
        )
        return container, exposure is not None

    def _pod_proxy_socket_client(self) -> PodProxySocketClient:
        if self.pod_proxy_socket_client is None:
            msg = "pod proxy socket client is not configured"
            raise RuntimeError(msg)
        return self.pod_proxy_socket_client

    def _pod_proxy_connections(self) -> PodProxyConnectionRepository:
        if self.pod_proxy_connections is None:
            msg = "pod proxy connection repository is not configured"
            raise RuntimeError(msg)
        return self.pod_proxy_connections

    def _container_client_factory(
        self,
    ) -> SchedulerContainerClientFactory[PodContainerControlClient]:
        if self.container_clients is None:
            msg = "container client factory is not configured"
            raise RuntimeError(msg)
        return self.container_clients

    def _container_status_response(self, container_id: str) -> PodSandboxStatusResponse:
        container = self._container(container_id)
        state = self._container_client_factory().state_for(container)
        if state is None:
            return PodSandboxStatusResponse(
                status=_task_status_for_container(container.status),
                exit_code=container.exit_code or 0,
            )
        return PodSandboxStatusResponse(
            status=_task_status_for_scheduler(state.status),
            exit_code=container.exit_code or 0,
        )

    def _store_url(self, container_id: str, port: int, url: str) -> None:
        if not url:
            return
        with self.services.context.database.session() as session:
            PodExecutionRepository(session).urls.upsert(
                container_id=container_id,
                port=port,
                url=url,
            )

    def _stored_urls(self, container_id: str) -> dict[int, str]:
        with self.services.context.database.session() as session:
            records = PodExecutionRepository(session).urls.list_for_container(container_id)
        return {item.port: item.url for item in records}


def _file_info(value: ContainerSandboxFileInfo) -> PodSandboxFileInfo:
    mod_time_epoch = value.mod_time
    return PodSandboxFileInfo(
        mode=value.mode,
        size=value.size,
        mod_time=datetime.fromtimestamp(mod_time_epoch, UTC) if mod_time_epoch else None,
        owner=value.owner,
        group=value.group,
        is_dir=value.is_dir,
        name=value.name,
        permissions=value.permissions,
    )


def _raise_container_response_error(response: ContainerOperationResponse) -> None:
    if response.ok:
        return
    message = response.error_msg or "container operation failed"
    if "not found" in message.lower():
        raise NotFoundError(message)
    raise UpstreamUnavailableError(message)


def _task_status_for_scheduler(status: SchedulerContainerStatus) -> str:
    match status:
        case SchedulerContainerStatus.Pending:
            return TaskStatus.Pending.value
        case SchedulerContainerStatus.Running:
            return TaskStatus.Running.value
        case SchedulerContainerStatus.Stopping:
            return TaskStatus.Cancelled.value
        case SchedulerContainerStatus.Complete:
            return TaskStatus.Complete.value
        case SchedulerContainerStatus.Failed:
            return TaskStatus.Failed.value


def _task_status_for_container(status: ContainerStatus) -> str:
    match status:
        case ContainerStatus.Pending:
            return TaskStatus.Pending.value
        case ContainerStatus.Running:
            return TaskStatus.Running.value
        case ContainerStatus.Exited | ContainerStatus.Stopped:
            return TaskStatus.Complete.value
        case ContainerStatus.Failed:
            return TaskStatus.Failed.value


def _route_id_for_port(routes: Iterable[AgentBackendRoute], port: int) -> str:
    for route in routes:
        if route.port == port:
            return route.route_id
    return ""


def _owned_route_id_for_port(
    routes: Iterable[AgentBackendRoute],
    *,
    address: str,
    port: int,
    container_id: str,
    workspace_id: str,
) -> str:
    address_route_id, address_is_route = parse_backend_route_address(address)
    matching = [route for route in routes if route.port == port]
    if not matching:
        if address_is_route:
            raise PodProxyUnavailable("sandbox address ownership is invalid")
        return ""
    if len(matching) != 1:
        raise PodProxyUnavailable("sandbox address ownership is ambiguous")
    route = matching[0]
    if route.container_id != container_id or route.workspace_id != workspace_id:
        raise PodProxyUnavailable("sandbox address ownership is invalid")
    if not route.route_id or route.state != BackendRouteState.Ready.value or bool(route.error):
        raise PodProxyUnavailable("sandbox backend route is unavailable")
    if address_is_route and route.route_id != address_route_id:
        raise PodProxyUnavailable("sandbox address ownership is invalid")
    return route.route_id


def _query_string(query_params: dict[str, list[str]]) -> str:
    return urlencode(
        [(key, value) for key, values in query_params.items() for value in values],
    )


__all__ = ["PodControlService"]
