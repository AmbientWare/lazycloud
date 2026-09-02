from __future__ import annotations

import hashlib
import ipaddress
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from compute.agent_control import (
    DEFAULT_PRIVATE_JOIN_TTL_SECONDS,
    AgentImageConfig,
    AgentJoinRequest,
    AgentRouteStatusRequest,
    AgentWorkerTokenPlan,
    GatewayEndpointConfig,
    JoinTokenCreationPlan,
    WorkerTokenKind,
    WorkerTokenRecord,
    agent_install_command,
    agent_machine_worker_id,
    agent_worker_image,
    build_agent_bootstrap_config,
    hash_compute_token,
    hash_machine_fingerprint,
    plan_agent_heartbeat_touch,
    plan_agent_join,
    plan_agent_stream_snapshot,
    plan_agent_worker_slot,
    plan_agent_worker_token,
    plan_route_status_update,
)
from compute.projection import PoolConfig
from compute.providers import joined_unit_identity
from compute.service import ComputeService
from compute.state import (
    AsyncRedisComputeStateRepository,
    ComputeAgentTokenState,
    ComputeAgentWorkerSlotState,
    ComputeJoinTokenState,
    RedisComputeStateRepository,
)
from compute.telemetry import (
    AGENT_HEARTBEAT_TIMEOUT_SECONDS,
    AgentDisconnectAction,
    AgentMetricUpdatePlan,
    PoolTelemetryState,
    agent_machine_last_seen,
    agent_silence_description,
    plan_agent_disconnect,
    plan_agent_metric_update,
    redact_telemetry_line,
    validate_agent_telemetry_token,
)
from compute.telemetry import (
    AgentMetricSnapshot as AgentMetricSnapshotProtocol,
)
from control.apps import AppService
from control.deployment_resources import DeploymentResourceService, client_manifest_resource
from control.deployments import DeploymentService
from control.service import ControlPlaneService, StubKind
from coordination.redis_client import AsyncRedisClient
from database.context import ServiceContext
from database.repositories.compute import (
    ComputeJoinCredentialRecord,
    ComputeJoinCredentialRepository,
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRecord,
    ComputeMachineEnrollmentRepository,
    ComputeUnitRepository,
    WireGuardGatewayRepository,
    WireGuardPeerRepository,
)
from database.repositories.execution import LogRepository
from database.repositories.identity import WorkspaceMemberRepository
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from execution.containers.service import ContainerService
from execution.functions.service import FunctionControlService
from execution.tasks import TaskService
from identity.auth import AuthService
from identity.authz import AuthzRequirement
from identity.signatures import sign_payload
from networking.async_http import AsyncBackendHttpClient, AsyncBackendHttpError
from networking.dialer import BackendConnector, SocketBackendConnector
from networking.routing import BackendRouteAuthenticator
from networking.wireguard import (
    WIREGUARD_AGENT_ROUTE_PROXY_PORT,
    WIREGUARD_INTERFACE,
    WIREGUARD_KEEPALIVE_SECONDS,
    WIREGUARD_PLATFORM_NETWORK,
    WireGuardPeerConfiguration,
    allocate_wireguard_agent_address,
    validate_wireguard_public_key,
)
from observability.events import EventService
from observability.metrics import MetricsService
from observability.stream_state import AsyncRedisEventStreamRepository, RedisEventStreamRepository
from observability.usage import UsageService
from operations.management import ManagementService
from pydantic import JsonValue
from scheduler.state import (
    RedisWorkerPoolStateRepository,
    SchedulerRepositoryError,
)
from scheduler.workers import (
    SchedulerWorkerAdminRepository,
    SchedulerWorkerAdminService,
    SchedulerWorkerContainerRepository,
)
from shared.app_identity import AGENT_NAME, CONTAINER_WORKER_IMAGE
from shared.app_slug import app_slug_or_default, validate_app_slug
from shared.compute_enrollment import (
    AgentCapacityState,
    ComputeCredentialStatus,
    ComputeMachineEnrollmentStatus,
    MachineReadinessPhase,
    PrivateNetworkEnrollmentPhase,
    WireGuardPeer,
    WireGuardPeerStatus,
)
from shared.compute_fleet import Machine, ResourceStatus, Worker
from shared.compute_policy import (
    ComputeUnitRecord,
    MachinePool,
)
from shared.container_requests import StopContainerReason
from shared.containers import ContainerRecord, ContainerStatus
from shared.deployment_records import resolve_authorized, resolve_max_pending_tasks, resolve_retries
from shared.deployments import DeploymentKind
from shared.errors import (
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from shared.events import EventLevel
from shared.http.client_manifests import (
    CLIENT_MANIFEST_DEPLOYMENT_KINDS,
    ClientManifestRequest,
    ClientManifestResponse,
)
from shared.http.compute import (
    MachineJoinCommandRequest,
    MachineJoinCommandResponse,
    MachineJoinTokenResponse,
    UnitJoinCommandResponse,
    UnitMachineListResponse,
    UnitMachineResponse,
)
from shared.http.gateway import (
    AgentCapacityInterruptionRequest,
    AgentCapacityInterruptionResponse,
    AttachToContainerRequest,
    AttachToContainerResponse,
    CheckpointContainerRequest,
    CheckpointContainerResponse,
    DeployStubRequest,
    DeployStubResponse,
    GatewayUrlKind,
    GetOrCreateStubRequest,
    GetOrCreateStubResponse,
    GetUrlRequest,
    GetUrlResponse,
    ResolveDeploymentTargetRequest,
    ResolveDeploymentTargetResponse,
    SyncContainerWorkspaceBody,
    SyncContainerWorkspaceResponse,
)
from shared.http.gateway_tasks import (
    AppendTaskLogRequest,
    AppendTaskLogResponse,
    EndTaskRequest,
    EndTaskResponse,
    StartTaskRequest,
    StartTaskResponse,
)
from shared.http.objects import (
    HeadObjectRequest,
    HeadObjectResponse,
    ObjectMetadata,
    PutObjectRequest,
    PutObjectResponse,
)
from shared.identity import AuthScope, TokenKind, TokenStatus
from shared.logs import LogEntry
from shared.objects import ObjectRecord
from shared.realtime.contracts import EventRecordType
from shared.realtime.streams import LogStreamQuery
from shared.routing import AgentBackendRoute
from shared.scheduling import (
    SchedulerContainerStatus,
    SchedulerWorkerRecord,
    SchedulerWorkerStatus,
    WorkerUnavailableReason,
)
from shared.source_cache_cleanup import (
    WorkerCacheStorageDestructionEvidence,
    WorkerCacheStorageOwnerKind,
    WorkerCacheStorageOwnerRecord,
)
from shared.tasks import Task, TaskStatus
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner, UsageMetric, UsageUnit, usage_record_id
from sqlalchemy.orm import Session
from storage.service import ObjectStorage
from worker.container_client import models
from worker.container_client.scheduler import SchedulerContainerClientFactory

from database import AsyncDatabaseClient
from gateway.http import (
    AgentMetricSnapshot as HttpAgentMetricSnapshot,
)
from gateway.http import (
    AgentRoute,
    AgentTelemetryRequest,
    AgentTelemetryResponse,
    AuthorizeRequest,
    AuthorizeResponse,
    JoinAgentRequest,
    JoinAgentResponse,
    LeaveAgentRequest,
    LeaveAgentResponse,
    ListAgentRoutesRequest,
    ListAgentRoutesResponse,
    RegisterAgentPrivateNetworkRequest,
    RegisterAgentPrivateNetworkResponse,
    SignPayloadRequest,
    SignPayloadResponse,
    StreamAgentRequest,
    StreamAgentResponse,
    UpdateAgentRouteStatusRequest,
    UpdateAgentRouteStatusResponse,
)
from gateway.payloads import (
    CONTAINER_OUTPUT_LOG_LIMIT,
    container_output,
    object_key,
    task_result_value,
)
from gateway.route_prewarm import RoutePrewarmService
from gateway.stub_config import deployment_spec_from_stub, stub_config, stub_kind
from gateway.unit_state import GatewayUnitStateCoordinator, billing_owner_for_unit
from gateway.views import (
    agent_pool_transport,
    agent_route_view,
    agent_telemetry_state,
    agent_worker_record,
    agent_worker_slot_view,
    machine_view,
    pool_config_from_unit,
    stub_for_task,
)


class GatewayServices(Protocol):
    @property
    def context(self) -> ServiceContext: ...

    @property
    def auth(self) -> AuthService: ...

    @property
    def apps(self) -> AppService: ...

    @property
    def deployments(self) -> DeploymentService: ...

    @property
    def deployment_resources(self) -> DeploymentResourceService: ...

    @property
    def compute(self) -> ComputeService: ...

    @property
    def containers(self) -> ContainerService: ...

    @property
    def events(self) -> EventService: ...

    @property
    def metrics(self) -> MetricsService: ...

    @property
    def object_storage(self) -> ObjectStorage: ...

    @property
    def tasks(self) -> TaskService: ...

    @property
    def usage(self) -> UsageService: ...


class GatewayContainerStopper(Protocol):
    def stop_container(self, container: ContainerRecord) -> None: ...


class CapacityReservationGuard(Protocol):
    def mutation_lock(self, capacity_owner_id: str) -> AbstractContextManager[None]: ...

    def has_open_reservations(self, capacity_owner_id: str) -> bool: ...


class AgentCapacityInterruptionSink(Protocol):
    def preempt_agent_capacity(self, state: ComputeAgentTokenState) -> None: ...


def _deployment_kind_to_stub_kind(kind: DeploymentKind) -> StubKind:
    try:
        return StubKind(kind.value)
    except ValueError as exc:
        msg = f"deployment kind is not invokable: {kind}"
        raise ValueError(msg) from exc


def _request_with_workload_defaults(
    request: GetOrCreateStubRequest,
    kind: StubKind,
) -> GetOrCreateStubRequest:
    fields_set = request.model_fields_set
    updates: dict[str, bool | int] = {}
    if "authorized" not in fields_set:
        updates["authorized"] = resolve_authorized(kind.value, None)
    if "max_pending_tasks" not in fields_set:
        max_pending_tasks = resolve_max_pending_tasks(kind.value, None)
        if max_pending_tasks is not None:
            updates["max_pending_tasks"] = max_pending_tasks
    if "retries" not in fields_set and request.retry_policy is None:
        updates["retries"] = resolve_retries(kind.value, None)
    return request.model_copy(update=updates) if updates else request


SELF_HOSTED_FLEET_POOL_NAME = "self-hosted"
"""Server-owned name of each workspace's single implicit self-hosted machine fleet."""

MACHINE_STATUS_FOR_READINESS = {
    MachineReadinessPhase.Ready: ResourceStatus.Running,
    MachineReadinessPhase.Blocked: ResourceStatus.Failed,
    MachineReadinessPhase.Offline: ResourceStatus.Stopped,
    MachineReadinessPhase.Joining: ResourceStatus.Created,
    MachineReadinessPhase.Revoked: ResourceStatus.Deleted,
}
"""What a readiness phase means for the machine row both writers keep in step."""

DISCONNECT_SWEEP_LIMIT = 200
"""Machines one disconnect sweep will mark.

A bound on the work a single pass does, not on how many machines can be marked:
each pass writes a disconnect that takes those rows out of the next scan, so a
larger backlog drains over consecutive passes instead of holding one lease for
the whole fleet."""

PRIVATE_NETWORK_PROBE_TIMEOUT_SECONDS = 2.0
# Matches the presigned PUT validity the object store hands out.
OBJECT_UPLOAD_TIMEOUT_SECONDS = 3600.0


def _domain_error(exc: KeyError | ValueError) -> DomainError:
    if isinstance(exc, KeyError):
        return NotFoundError(str(exc).strip("'\""))
    return InvalidInputError(str(exc))


@dataclass(frozen=True, slots=True)
class GatewayControlService:
    services: GatewayServices
    control_plane: ControlPlaneService
    management: ManagementService
    compute_state: RedisComputeStateRepository
    scheduler_workers: SchedulerWorkerAdminRepository
    scheduler_containers: SchedulerWorkerContainerRepository
    scheduler_pool_states: RedisWorkerPoolStateRepository
    capacity_reservations: CapacityReservationGuard
    object_storage: ObjectStorage
    gateway_endpoint: GatewayEndpointConfig
    agent_image: AgentImageConfig
    event_streams: RedisEventStreamRepository
    route_prewarmer: RoutePrewarmService
    container_stopper: GatewayContainerStopper
    container_client_factory: SchedulerContainerClientFactory
    # Resolved per use, not held: the control plane publishes where it is
    # actually reachable, and a value captured at construction would outlive a
    # device rename that every agent picks up on its next poll.
    runtime_origin: Callable[[], str]
    private_network_connector: BackendConnector = field(default_factory=SocketBackendConnector)
    capacity_interruption_sink: AgentCapacityInterruptionSink | None = None
    route_authenticator: BackendRouteAuthenticator | None = None
    agent_cluster_name: str = AGENT_NAME
    agent_worker_image_registry: str = ""
    agent_worker_image_name: str = CONTAINER_WORKER_IMAGE
    agent_worker_image_tag: str = "local"
    agent_artifact_version: str = ""
    agent_sha256_by_arch: Mapping[str, str] = field(default_factory=lambda: dict[str, str]())
    async_http_client: AsyncBackendHttpClient | None = None

    @property
    def objects(self) -> ObjectStorage:
        return self.object_storage

    @property
    def compute_states(self) -> RedisComputeStateRepository:
        return self.compute_state

    @property
    def unit_state_coordinator(self) -> GatewayUnitStateCoordinator:
        return GatewayUnitStateCoordinator(
            self.services.context,
            self.services.compute,
            self.compute_states,
        )

    @property
    def scheduler_worker_lookup(self) -> SchedulerWorkerAdminRepository:
        return self.scheduler_workers

    @property
    def scheduler_container_lookup(self) -> SchedulerWorkerContainerRepository:
        return self.scheduler_containers

    @property
    def scheduler_pool_state_repository(self) -> RedisWorkerPoolStateRepository:
        return self.scheduler_pool_states

    def authorize(self, request: AuthorizeRequest, authorization: str | None) -> AuthorizeResponse:
        token = self.services.auth.authorize_header(
            authorization,
            AuthzRequirement(action=request.action),
            allow_if_no_tokens=False,
        )
        return AuthorizeResponse(workspace_id=token.workspace_id if token else "default")

    def sign_payload(self, request: SignPayloadRequest) -> SignPayloadResponse:
        try:
            secret_key = self.control_plane.workspace_signing_key(request.workspace)
            timestamp = request.timestamp or int(time.time())
            signature = sign_payload(request.payload_bytes(), secret_key, timestamp=timestamp)
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return SignPayloadResponse(
            signature=signature.key,
            timestamp=signature.timestamp,
        )

    def head_object(self, request: HeadObjectRequest, *, workspace_id: str) -> HeadObjectResponse:
        try:
            record = self.objects.find_by_sha256_for_workspace(
                workspace_id=workspace_id,
                sha256=request.hash,
                bucket=request.bucket,
            )
            if record is None:
                with_key = self._object_by_key(
                    request.bucket,
                    request.hash,
                    workspace_id=workspace_id,
                )
                record = with_key
            if record is None:
                return HeadObjectResponse(exists=False)
            if not self.objects.object_is_complete(record):
                return HeadObjectResponse(exists=False)
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return HeadObjectResponse(
            exists=True,
            object_id=record.id,
            object_metadata=ObjectMetadata(name=record.key, size=record.size),
        )

    async def put_object_chunks(
        self,
        request: PutObjectRequest,
        chunks: AsyncIterator[bytes],
        *,
        workspace_id: str,
        database: AsyncDatabaseClient,
    ) -> PutObjectResponse:
        if self.async_http_client is None:
            raise UpstreamUnavailableError("async object upload transport is unavailable")
        key = object_key(request.object_metadata, request.hash)
        upload = None
        try:
            upload = await self.objects.prepare_stream_upload(
                database,
                workspace_id=workspace_id,
                bucket=request.bucket,
                key=key,
                size=request.object_metadata.size,
                sha256=request.hash,
                content_type=request.content_type,
                metadata=request.metadata,
                overwrite=request.overwrite,
            )
            target = urlsplit(upload.upload.url)
            if target.scheme not in {"http", "https"} or not target.netloc:
                raise UpstreamUnavailableError("object store returned an invalid upload target")
            response = await self.async_http_client.open_stream(
                address=urlunsplit((target.scheme, target.netloc, "", "", "")),
                route_id="",
                method="PUT",
                path=urlunsplit(("", "", target.path or "/", target.query, "")),
                headers=upload.upload.headers,
                body=self._validated_upload_chunks(request, chunks),
                content_length=request.object_metadata.size,
                timeout_seconds=OBJECT_UPLOAD_TIMEOUT_SECONDS,
                resource="object store",
            )
            await response.read()
            if 400 <= response.status_code < 500:
                raise InvalidInputError("object store rejected the upload")
            if response.status_code < 200 or response.status_code >= 300:
                raise UpstreamUnavailableError(
                    f"object store upload failed with status {response.status_code}"
                )
            record = await self.objects.complete_stream_upload(database, upload)
        except BaseException as exc:
            if upload is not None:
                try:
                    await self.objects.abort_stream_upload(database, upload)
                except BaseException as abort_error:
                    raise BaseExceptionGroup(
                        "object upload and claim cleanup failed",
                        [exc, abort_error],
                    ) from None
            if isinstance(exc, (KeyError, ValueError)):
                raise _domain_error(exc) from exc
            if isinstance(exc, (AsyncBackendHttpError, RuntimeError, OSError)):
                raise UpstreamUnavailableError(str(exc)) from exc
            raise
        return PutObjectResponse(object_id=record.id)

    @staticmethod
    async def _validated_upload_chunks(
        request: PutObjectRequest,
        chunks: AsyncIterator[bytes],
    ) -> AsyncIterator[bytes]:
        digest = hashlib.sha256()
        size = 0
        async for chunk in chunks:
            if not chunk:
                continue
            size += len(chunk)
            if size > request.object_metadata.size:
                raise InvalidInputError("object size does not match content")
            digest.update(chunk)
            yield chunk
        if size != request.object_metadata.size:
            raise InvalidInputError("object size does not match content")
        if digest.hexdigest() != request.hash:
            raise InvalidInputError("object hash does not match content")

    def object_download_url(
        self,
        *,
        workspace_id: str,
        bucket: str,
        key: str,
        expires_seconds: int = 3600,
    ) -> str:
        return self.objects.generate_presigned_get_url_for_workspace(
            workspace_id=workspace_id,
            bucket=bucket,
            key=key,
            expires_seconds=expires_seconds,
        )

    def checkpoint_container(
        self,
        request: CheckpointContainerRequest,
        *,
        workspace_id: str,
    ) -> CheckpointContainerResponse:
        try:
            container = self._container_for_workspace(request.container_id, workspace_id)
            response = self.container_client_factory.client_for(container).client.checkpoint(
                container.id,
                checkpoint_id=request.checkpoint_id or str(uuid4()),
            )
            if not response.ok:
                raise InvalidInputError(response.error_msg or "container checkpoint failed")
            if not response.checkpoint_id:
                raise UpstreamUnavailableError("container worker did not return a checkpoint id")
            self.services.events.emit(
                "container.checkpoint",
                resource_type="container",
                resource_id=container.id,
                message=f"created checkpoint for container {container.name}",
                data={"checkpoint_id": response.checkpoint_id},
                workspace_id=container.workspace_id,
            )
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        except RuntimeError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        return CheckpointContainerResponse(checkpoint_id=response.checkpoint_id)

    def _stop_container_by_id(self, container_id: str) -> None:
        container = self.services.containers.get(container_id)
        # `Scheduler`, not `Admin`: a drained container is usually also serving
        # calls nobody cancelled, and `Admin` settles their claims the way it
        # settles an operator's intent — cancelled, terminal, no retry. Saying
        # the platform stopped it releases them to run somewhere else.
        stopped = self.services.containers.stop(
            container.id,
            reason=StopContainerReason.Scheduler,
        )
        self._stop_worker_container(stopped)

    def _stop_worker_container(self, container: ContainerRecord) -> None:
        try:
            self.container_stopper.stop_container(container)
        except Exception as exc:
            self.services.events.emit(
                "container.worker_stop.failed",
                resource_type="container",
                resource_id=container.id,
                message=f"failed to stop worker container directly: {exc}",
                level=EventLevel.Warning,
                workspace_id=container.workspace_id,
            )

    async def attach_to_container(
        self,
        request: AttachToContainerRequest,
        *,
        workspace_id: str,
        database: AsyncDatabaseClient,
        redis: AsyncRedisClient,
    ) -> AttachToContainerResponse:
        try:
            container, task_logs = await database.run_transaction(
                lambda session: self._container_output_state(
                    session,
                    request.container_id,
                    workspace_id,
                )
            )
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        output = await container_output(
            container,
            task_logs=task_logs,
            logs=AsyncRedisEventStreamRepository(redis),
        )
        done = container.finished_at is not None
        return AttachToContainerResponse(
            output=output,
            done=done,
            exit_code=container.exit_code or 0,
        )

    @staticmethod
    def _container_output_state(
        session: Session,
        container_id: str,
        workspace_id: str,
    ) -> tuple[ContainerRecord, tuple[LogEntry, ...]]:
        container = ContainerRepository(session).get(container_id, workspace_id=workspace_id)
        if container is None:
            raise NotFoundError(f"container not found: {container_id}")
        if not container.task_id:
            return container, ()
        page = LogRepository(session).page(
            LogStreamQuery(workspace_id=workspace_id, task_id=container.task_id),
            workspace_id=workspace_id,
            limit=CONTAINER_OUTPUT_LOG_LIMIT,
        )
        return container, tuple(record.entry for record in page.data)

    def sync_container_workspace(
        self,
        request: SyncContainerWorkspaceBody,
        *,
        workspace_id: str,
    ) -> SyncContainerWorkspaceResponse:
        try:
            container = self._container_for_workspace(request.container_id, workspace_id)
            response = self.container_client_factory.client_for(container).client.sync_workspace(
                models.SyncContainerWorkspaceRequest(
                    container_id=request.container_id,
                    operation=models.ContainerWorkspaceSyncOperation(request.operation.value),
                    path=request.path,
                    new_path=request.new_path,
                    data=request.data,
                    mode=request.mode,
                    metadata=request.metadata,
                )
            )
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        except RuntimeError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        if not response.ok:
            raise InvalidInputError(response.error_msg or "container workspace sync failed")
        return SyncContainerWorkspaceResponse(path=response.path)

    def start_task(self, request: StartTaskRequest, *, workspace_id: str) -> StartTaskResponse:
        try:
            pending = self._task_for_workspace(request.task_id, workspace_id)
            self._record_task_lifecycle(
                pending,
                "dispatch",
                container_id=request.container_id,
            )
            task = self.services.tasks.start(
                request.task_id,
                container_id=request.container_id or None,
            )
            self._record_task_lifecycle(
                task,
                "claim",
                container_id=request.container_id,
            )
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return StartTaskResponse(task_id=request.task_id)

    def append_task_log(
        self,
        request: AppendTaskLogRequest,
        *,
        workspace_id: str,
    ) -> AppendTaskLogResponse:
        try:
            self._task_for_workspace(request.task_id, workspace_id)
            self.services.tasks.append_log(request.task_id, request.stream, request.message)
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return AppendTaskLogResponse(task_id=request.task_id)

    def end_task(self, request: EndTaskRequest, *, workspace_id: str) -> EndTaskResponse:
        try:
            pending = self._task_for_workspace(request.task_id, workspace_id)
            result = task_result_value(request)
            error = (
                None if request.task_status is TaskStatus.Complete else request.task_status.value
            )
            stub = stub_for_task(self.control_plane, pending)
            if stub is not None and stub.kind is StubKind.Function:
                task = FunctionControlService(
                    self.services,
                    gateway_http_url=self.runtime_origin,
                ).finish_function_task(
                    request.task_id,
                    request.task_status,
                    container_id=request.container_id,
                    result=result,
                    error=error,
                    exit_code=0 if request.task_status is TaskStatus.Complete else 1,
                )
            else:
                task = self.services.tasks.finish(
                    request.task_id,
                    request.task_status,
                    result=result,
                    error=error,
                )
            self._record_task_lifecycle(
                task,
                "complete",
                container_id=request.container_id,
                duration_seconds=request.task_duration,
                result_present=bool(request.result_base64),
                keep_warm_seconds=request.keep_warm_seconds,
            )
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return EndTaskResponse(
            task_status=request.task_status,
            final_status=task.status,
            retry_scheduled=task.status is TaskStatus.Retry,
            attempt_number=task.attempt_number,
            max_attempts=task.max_attempts,
        )

    def _record_task_lifecycle(
        self,
        task: Task,
        phase: str,
        *,
        container_id: str = "",
        duration_seconds: float = 0,
        result_present: bool = False,
        keep_warm_seconds: float = 0,
    ) -> None:
        labels = {
            "phase": phase,
            "status": task.status.value,
        }
        if task.deployment_id:
            labels["deployment_id"] = task.deployment_id
        self.services.metrics.increment("gateway_task_lifecycle_total", labels=labels)
        if phase == "complete" and duration_seconds > 0:
            self.services.metrics.observe_histogram(
                "gateway_task_duration_seconds",
                duration_seconds,
                labels={"status": task.status.value},
            )
        resolved_container_id = container_id or str(task.kwargs.get("container_id") or "")
        stub = stub_for_task(self.control_plane, task)
        workspace_id = stub.workspace_id if stub else ""
        app_id = stub.app_id if stub and stub.app_id else ""
        data: dict[str, JsonValue] = {
            "phase": phase,
            "status": task.status.value,
            "task_id": task.id,
            "container_id": resolved_container_id,
            "deployment_id": task.deployment_id or "",
            "workspace_id": workspace_id,
            "stub_id": stub.id if stub else "",
            "app_id": app_id,
            "result_present": result_present,
        }
        if duration_seconds > 0:
            data["duration_seconds"] = duration_seconds
        if keep_warm_seconds > 0:
            data["keep_warm_seconds"] = keep_warm_seconds
        self.services.events.emit(
            f"task.lifecycle.{phase}",
            resource_type="task",
            resource_id=task.id,
            message=f"task {task.name} lifecycle {phase}",
            level=EventLevel.Error if task.status is TaskStatus.Failed else EventLevel.Info,
            data=data,
            workspace_id=workspace_id or None,
        )
        self.event_streams.append_event(EventRecordType.TaskUpdated, data)

    def get_or_create_stub(self, request: GetOrCreateStubRequest) -> GetOrCreateStubResponse:
        try:
            kind = stub_kind(request.stub_type)
            request = _request_with_workload_defaults(request, kind)
            config = stub_config(request)
            app_name = app_slug_or_default(request.app_name, default=request.name)
            app = self.services.apps.create(
                app_name,
                workspace=request.workspace,
                public=not request.authorized,
            )
            metadata: dict[str, JsonValue] = {
                "object_id": request.object_id,
                "image_id": request.image_id,
                "force_create": request.force_create,
                "app": app.name,
                "app_id": app.id,
            }
            config_metadata = dict(config.metadata)
            config_metadata["app"] = app.name
            config_metadata["app_id"] = app.id
            config.metadata = config_metadata
            stub = self.control_plane.create_stub(
                request.name,
                workspace=request.workspace,
                kind=kind,
                handler=request.handler or None,
                app_id=app.id,
                public=not request.authorized,
                config=config,
                metadata=metadata,
            )
            self.services.apps.create(
                app.name,
                stub_id=stub.id,
                workspace=stub.workspace_id,
                public=not request.authorized,
            )
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return GetOrCreateStubResponse(stub_id=stub.id)

    def deploy_stub(self, request: DeployStubRequest) -> DeployStubResponse:
        try:
            stub = self.control_plane.get_stub(request.stub_id, workspace=request.workspace)
            workspace = request.workspace or stub.workspace_id
            deployment = self.services.deployments.deploy(
                deployment_spec_from_stub(stub, name=request.name or stub.name),
                workspace=workspace,
            )
            resource = self.services.deployment_resources.get_by_deployment_id(
                deployment.id,
                workspace=workspace,
            )
            if resource is None:
                msg = f"deployment resource not found after deploy: {deployment.id}"
                raise ValueError(msg)
            if resource.stub.kind is StubKind.Pod:
                pod_ports = list(resource.stub.config.ports.values()) or list(
                    resource.stub.config.runtime.ports.values()
                )
                invoke_url = (
                    self.control_plane.stub_url(
                        resource.stub.id,
                        apps=self.services.apps,
                        workspace=workspace,
                        external_url=request.external_url,
                        deployment_id=deployment.id,
                        port=pod_ports[0],
                    ).url
                    if pod_ports
                    else ""
                )
            else:
                invoke_url = resource.invoke_url(request.external_url)
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return DeployStubResponse(
            stub_id=resource.stub.id,
            deployment_id=deployment.id,
            app_id=resource.app.id,
            version=deployment.version,
            invoke_url=invoke_url,
        )

    def get_url(self, request: GetUrlRequest) -> GetUrlResponse:
        try:
            if request.url_type is GatewayUrlKind.Deployment and request.deployment_id:
                url = self.management.deployment_url(
                    request.deployment_id,
                    workspace=request.workspace,
                    external_url=request.external_url,
                ).url
            else:
                url = self.control_plane.stub_url(
                    request.stub_id,
                    apps=self.services.apps,
                    workspace=request.workspace,
                    deployment_id=request.deployment_id or None,
                    external_url=request.external_url,
                    port=request.port,
                ).url
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return GetUrlResponse(url=url)

    def resolve_deployment_target(
        self,
        request: ResolveDeploymentTargetRequest,
    ) -> ResolveDeploymentTargetResponse:
        try:
            stub_type = _deployment_kind_to_stub_kind(request.kind)
            app_id = None
            if request.app:
                app = self.services.apps.get(
                    validate_app_slug(request.app),
                    workspace=request.workspace,
                )
                app_id = app.id
            result = self.management.deployment_url_by_name(
                request.workspace,
                stub_type,
                request.name,
                request.deployment_version,
                app_id=app_id,
                external_url=request.external_url,
            )
            if result.stub is None:
                raise NotFoundError(f"deployment target has no stub: {request.name}")
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return ResolveDeploymentTargetResponse(
            kind=request.kind,
            stub_id=result.stub.id,
            deployment_id=result.deployment.id,
            deployment_name=result.deployment.name,
            deployment_version=result.deployment.version,
            url=result.url,
        )

    def client_manifest(self, request: ClientManifestRequest) -> ClientManifestResponse:
        try:
            app = self.services.apps.get(request.app, workspace=request.workspace)
            deployed_resources = self.services.deployment_resources.list(
                workspace=request.workspace,
                app=app.name,
                kinds=CLIENT_MANIFEST_DEPLOYMENT_KINDS,
                active=True,
                latest_per_resource=True,
            )
            resources = [
                client_manifest_resource(
                    deployed_resource,
                    external_url=request.external_url,
                )
                for deployed_resource in deployed_resources
            ]
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return ClientManifestResponse(
            app=app.name,
            workspace=request.workspace,
            resources=resources,
        )

    def delete_unit(self, unit_id: str, *, workspace_id: str) -> None:
        try:
            unit = self.unit_state_coordinator.unit_by_id(
                unit_id,
                workspace_id=workspace_id,
            )
            name = unit.name
            with self.capacity_reservations.mutation_lock(unit.capacity_owner_id):
                if self.capacity_reservations.has_open_reservations(unit.capacity_owner_id):
                    raise ConflictError(f"compute pool {name!r} has active capacity reservations")
                self._delete_pool_enrollments(
                    workspace_id,
                    unit,
                    require_host_decommission=unit.provider == "agent",
                )
                self._delete_pool_workers(unit.capacity_owner_id)
                self.services.compute.delete_unit(unit.capacity_owner_id, workspace=workspace_id)
                self.unit_state_coordinator.delete_compute_unit_state(
                    unit.capacity_owner_id,
                    workspace_id=workspace_id,
                )
                self.scheduler_pool_state_repository.delete_unit_state(unit.capacity_owner_id)
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc

    def scale_unit(
        self,
        unit_id: str,
        desired_machines: int,
        *,
        workspace_id: str,
    ) -> ComputeUnitRecord:
        try:
            owner = self.unit_state_coordinator.unit_by_id(
                unit_id,
                workspace_id=workspace_id,
            )
            name = owner.name

            def assert_scale_down_is_safe(current: ComputeUnitRecord) -> None:
                if current.capacity_owner_id != owner.capacity_owner_id:
                    raise ConflictError(
                        f"compute pool {name!r} capacity ownership changed during scaling"
                    )
                if desired_machines != 0 and desired_machines >= max(
                    current.desired_machines,
                    current.observed_machines,
                ):
                    return
                if self.capacity_reservations.has_open_reservations(owner.capacity_owner_id):
                    raise ConflictError(f"compute pool {name!r} has active capacity reservations")
                if self._unit_has_active_containers(
                    workspace_id=workspace_id,
                    pool=owner.pool,
                    capacity_owner_id=owner.capacity_owner_id,
                ):
                    raise ConflictError(
                        f"compute pool {name!r} still owns pending or running containers"
                    )
                if desired_machines == 0:
                    for worker in self.scheduler_worker_lookup.list_workers():
                        if worker.capacity_owner_id == owner.capacity_owner_id:
                            self.scheduler_worker_lookup.disable_worker(
                                worker.worker_id,
                                reason=WorkerUnavailableReason.MachineRetired,
                            )

            return self.services.compute.scale_internal_unit(
                workspace_id,
                owner.capacity_owner_id,
                desired_machines,
                before_mutation=assert_scale_down_is_safe,
            )
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc

    def clear_unit_degradation(self, unit_id: str, *, workspace_id: str) -> ComputeUnitRecord:
        unit = self.unit_state_coordinator.unit_by_id(unit_id, workspace_id=workspace_id)
        return self.services.compute.clear_capacity_degradation(
            workspace=workspace_id,
            capacity_owner_id=unit.capacity_owner_id,
        )

    def unit_state(self, unit_id: str, *, workspace_id: str) -> ComputeUnitRecord:
        """Read durable capacity state for one provisioning unit."""

        try:
            owner = self.unit_state_coordinator.unit_by_id(
                unit_id,
                workspace_id=workspace_id,
            )
            name = owner.name
            current = self.services.compute.get_internal_unit(workspace_id, owner.capacity_owner_id)
            if current.capacity_owner_id != owner.capacity_owner_id:
                raise ConflictError(
                    f"compute pool {name!r} capacity ownership does not match durable state"
                )
            return current
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc

    def _unit_has_active_containers(
        self,
        *,
        workspace_id: str,
        pool: MachinePool,
        capacity_owner_id: str,
    ) -> bool:
        owner_worker_ids = {
            worker.worker_id
            for worker in self.scheduler_worker_lookup.list_workers()
            if worker.capacity_owner_id == capacity_owner_id
        }
        if any(
            state.status
            in {
                SchedulerContainerStatus.Pending,
                SchedulerContainerStatus.Running,
            }
            and state.workspace_id == workspace_id
            for worker_id in owner_worker_ids
            for state in self.scheduler_container_lookup.list_by_worker(worker_id)
        ):
            return True
        for container in self.services.containers.list(
            workspace_id=workspace_id,
            statuses=(ContainerStatus.Pending, ContainerStatus.Running),
        ):
            worker_id = container.runtime_worker_id or container.worker_id or ""
            if worker_id in owner_worker_ids:
                return True
            if not worker_id:
                return True
            if container.stub_id is None:
                continue
            with suppress(NotFoundError):
                stub = self.control_plane.get_stub(
                    container.stub_id,
                    workspace=workspace_id,
                )
                if stub.config.runtime.pool_selector == pool:
                    return True
        return False

    def delete_pool_for_workspace_deletion(self, name: str, *, workspace_id: str) -> None:
        pools = self.services.compute.list_pools_for_workspace_deletion(workspace_id)
        pool = next((candidate for candidate in pools if candidate.name == name), None)
        if pool is None:
            return
        with self.capacity_reservations.mutation_lock(pool.capacity_owner_id):
            if self.capacity_reservations.has_open_reservations(pool.capacity_owner_id):
                raise ConflictError(f"compute pool {name!r} has active capacity reservations")
            self._delete_pool_enrollments(
                workspace_id,
                pool,
                deleting_workspace=True,
                require_host_decommission=pool.provider == "agent",
            )
            self._delete_pool_workers(pool.capacity_owner_id)
            self.services.compute.delete_unit_for_workspace_deletion(
                pool.capacity_owner_id,
                workspace_id=workspace_id,
            )
            self.unit_state_coordinator.delete_compute_unit_state(
                pool.capacity_owner_id,
                workspace_id=workspace_id,
            )
            self.scheduler_pool_state_repository.delete_unit_state(pool.capacity_owner_id)

    def machine_join_command(
        self,
        request: MachineJoinCommandRequest,
        *,
        user_id: str,
        owner_token_id: str,
    ) -> MachineJoinCommandResponse:
        """Mint the join command for the account's fleet in the pool it names.

        The self-hosted fleet is created on first join and owned server-side.
        A caller may name any group, including one a connected account also
        feeds — that is how a fleet mixes joined and provisioned machines.
        Requested GPU types extend the fleet's accepted set.
        """
        plan = self._mint_account_join_credential(request, user_id=user_id, token_id=owner_token_id)
        return MachineJoinCommandResponse(
            command=agent_install_command(
                self.gateway_endpoint.http_url,
                plan.token,
                agent_version=self.agent_artifact_version,
                agent_sha256_by_arch=self.agent_sha256_by_arch,
            ),
            expires_at=plan.expires_at,
        )

    def machine_join_token(
        self,
        request: MachineJoinCommandRequest,
        *,
        user_id: str,
        owner_token_id: str,
    ) -> MachineJoinTokenResponse:
        """Mint the bare join credential for the account's fleet.

        Same fleet resolution and same mint as `machine_join_command`; only the
        rendering differs.
        """
        plan = self._mint_account_join_credential(request, user_id=user_id, token_id=owner_token_id)
        return MachineJoinTokenResponse(token=plan.token, expires_at=plan.expires_at)

    def _mint_account_join_credential(
        self,
        request: MachineJoinCommandRequest,
        *,
        user_id: str,
        token_id: str,
    ) -> JoinTokenCreationPlan:
        workspace_id = self.account_fleet_workspace_id(user_id)
        try:
            fleet = self._resolve_joined_fleet(
                workspace_id,
                gpu=list(request.gpu),
                pool=MachinePool(request.pool.strip()),
            )
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return self.unit_state_coordinator.create_unit_join_token(
            fleet,
            workspace_id=workspace_id,
            owner_token_id=token_id,
            ttl=request.ttl,
        )

    def account_fleet_workspace_id(self, user_id: str) -> str:
        """The workspace that anchors an account's joined machines.

        A machine serves every workspace its account owns, but the unit that buys
        it and the durable machine row still live in one workspace, so the account's
        first workspace anchors them. Deriving it from the account rather than from
        wherever the caller happens to be is what keeps joining twice from producing
        two fleets for one account.
        """
        with self.services.context.database.session() as session:
            owned = WorkspaceMemberRepository(session).owned_workspace_ids(user_id)
        if not owned:
            raise ConflictError("this account owns no workspace to join a machine into")
        return owned[0]

    def _resolve_joined_fleet(
        self,
        workspace_id: str,
        *,
        gpu: list[str],
        pool: MachinePool = MachinePool(""),
    ) -> ComputeUnitRecord:
        """Find or create the unit that owns joined machines for one pool.

        One unit per group. A workspace joining hosts into a pool a connected
        account also feeds needs its own capacity owner there, or that account's
        drain would treat the joined hosts as its own.

        Nothing declares this unit; it exists because a host asked to join, and
        the identity is derived so asking twice resolves the same row.
        """
        group = pool or MachinePool(SELF_HOSTED_FLEET_POOL_NAME)
        _, unit_name = joined_unit_identity(
            workspace_id=workspace_id,
            pool=group,
            provider="agent",
        )
        try:
            current = self.unit_state_coordinator.unit_by_name(
                unit_name,
                workspace_id=workspace_id,
            )
        except NotFoundError:
            return self.unit_state_coordinator.create_or_update_pool(
                PoolConfig(name=unit_name, providers=["agent"], gpu=gpu),
                workspace_id=workspace_id,
                pool=group,
            )
        config = pool_config_from_unit(current)
        merged = [*config.gpu, *[item for item in gpu if item not in config.gpu]]
        if merged == config.gpu:
            return current
        return self.unit_state_coordinator.create_or_update_pool(
            config.model_copy(update={"providers": ["agent"], "gpu": merged}),
            workspace_id=workspace_id,
            pool=current.pool,
        )

    def create_unit_join_token(
        self,
        unit_id: str,
        *,
        workspace_id: str,
        owner_token_id: str,
        ttl: str = "",
    ) -> JoinTokenCreationPlan:
        unit = self.unit_state_coordinator.unit_by_id(unit_id, workspace_id=workspace_id)
        return self.unit_state_coordinator.create_unit_join_token(
            unit,
            workspace_id=workspace_id,
            owner_token_id=owner_token_id,
            ttl=ttl,
        )

    def revoke_unit_join_token(self, unit_id: str, *, workspace_id: str) -> None:
        unit = self.unit_state_coordinator.unit_by_id(unit_id, workspace_id=workspace_id)
        self.unit_state_coordinator.revoke_unit_join_token(
            unit,
            workspace_id=workspace_id,
        )

    def unit_join_command(
        self,
        unit_id: str,
        *,
        workspace_id: str,
        owner_token_id: str,
        ttl: str = "",
    ) -> UnitJoinCommandResponse:
        plan = self.create_unit_join_token(
            unit_id,
            workspace_id=workspace_id,
            owner_token_id=owner_token_id,
            ttl=ttl,
        )
        return UnitJoinCommandResponse(
            command=agent_install_command(
                self.gateway_endpoint.http_url,
                plan.token,
                agent_version=self.agent_artifact_version,
                agent_sha256_by_arch=self.agent_sha256_by_arch,
            ),
            expires_at=plan.expires_at,
        )

    def unit_machine_views(
        self,
        unit_id: str,
        *,
        workspace_id: str,
        limit: int,
        cursor: str = "",
    ) -> UnitMachineListResponse:
        unit = self.unit_state_coordinator.unit_by_id(unit_id, workspace_id=workspace_id)
        owned = {
            machine.id
            for machine in self.services.compute.list_machines(workspace=workspace_id)
            if machine.capacity_owner_id == unit.capacity_owner_id
        }
        machines = sorted(
            (
                item
                for item in self.machine_views(workspace_id)
                if item.id in owned and item.id > cursor
            ),
            key=lambda item: item.id,
        )
        selected = machines[:limit]
        return UnitMachineListResponse(
            data=selected,
            next=selected[-1].id if len(machines) > limit else "",
        )

    def account_machine_views(self, user_id: str) -> list[UnitMachineResponse]:
        """Every joined machine this account owns, across the workspaces it holds.

        Read from the enrollments rather than from any one workspace's machines:
        the enrollment is what names the account, and a caller asking for their own
        hardware should not have to know which of their workspaces anchors it.
        """
        with self.services.context.database.session() as session:
            enrollments = [
                enrollment
                for enrollment in ComputeMachineEnrollmentRepository(session).list_for_user(user_id)
                if enrollment.status is ComputeMachineEnrollmentStatus.Active
            ]
        owned_machine_ids = {enrollment.machine_id for enrollment in enrollments}
        views = [
            view
            for workspace_id in sorted({enrollment.workspace_id for enrollment in enrollments})
            for view in self.machine_views(workspace_id)
            # Provider-launched nodes enroll through the same path, so they hold
            # enrollments too. They are the connected account's capacity and are
            # reported there; this answer is hardware the customer connected.
            if view.id in owned_machine_ids and view.provider_name == "agent"
        ]
        views.sort(key=lambda item: item.id)
        return views

    def machine_views(self, workspace_id: str) -> list[UnitMachineResponse]:
        machines = self.services.compute.list_machines(workspace=workspace_id)
        with self.services.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            enrollment_by_machine = {
                enrollment.machine_id: enrollment
                for machine in machines
                if (
                    enrollment := enrollments.by_machine(
                        workspace_id,
                        machine.id,
                        pool=machine.pool,
                    )
                )
                is not None
                and enrollment.status is ComputeMachineEnrollmentStatus.Active
            }
        views: list[UnitMachineResponse] = []
        for machine in machines:
            enrollment = enrollment_by_machine.get(machine.id)
            views.append(
                machine_view(
                    machine,
                    _agent_state_from_enrollment(enrollment),
                    network_phase=(
                        enrollment.network_phase
                        if enrollment is not None
                        else PrivateNetworkEnrollmentPhase.Unconfigured
                    ),
                    network_failure_detail=(
                        enrollment.network_failure_detail if enrollment is not None else ""
                    ),
                )
            )
        return views

    def require_workspace_self_hosted_decommissioned(self, workspace_id: str) -> None:
        # Scoped by workspace id rather than resolved through the tenant-facing
        # listing, which accepts only an Active workspace. This guard runs both
        # before a workspace is marked Deleting and again on a retry after an
        # aborted attempt; resolving by status would make every retry raise
        # not-found and strand the workspace and its paid capacity for good.
        with self.services.context.database.session() as session:
            self_hosted_units = [
                pool
                for pool in ComputeUnitRepository(session).list_for_workspace(workspace_id)
                if pool.provider == "agent"
            ]
        if not self_hosted_units:
            return
        with self.services.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            enrolled_pool_names = {
                unit.name
                for unit in self_hosted_units
                if enrollments.list_for_unit(workspace_id, unit.capacity_owner_id)
            }
        if enrolled_pool_names:
            pools = ", ".join(sorted(enrolled_pool_names))
            raise ConflictError(
                "workspace still has enrolled self-hosted machines in pools "
                f"{pools}; run 'lazycloud-agent leave' on each owning host before deletion"
            )

    def delete_machine(
        self,
        machine_id: str,
        *,
        workspace_id: str,
        pool: MachinePool = MachinePool(""),
    ) -> None:
        try:
            with self.services.context.database.session() as session:
                enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
                    workspace_id,
                    machine_id,
                    pool=pool,
                )
            if enrollment is not None:
                unit = self.unit_state_coordinator.unit_by_capacity_owner(
                    enrollment.capacity_owner_id,
                    workspace_id=workspace_id,
                )
                if unit.provider == "agent":
                    raise ConflictError(
                        "enrolled self-hosted machines must be removed with "
                        "'lazycloud-agent leave' on the owning host"
                    )
                self._delete_enrolled_machine(enrollment)
            else:
                machine = self.services.compute.list_machines(workspace=workspace_id)
                if pool and not any(
                    item.id == machine_id and item.pool == pool for item in machine
                ):
                    msg = f"machine not found in pool: {machine_id}"
                    raise KeyError(msg)
                self.services.compute.delete_machine(machine_id, workspace=workspace_id)
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc

    def _delete_enrolled_machine(self, enrollment: ComputeMachineEnrollmentRecord) -> None:
        worker_id = agent_machine_worker_id(enrollment.machine_id)
        worker = self.scheduler_worker_lookup.get_worker(worker_id)
        if worker is not None:
            admin = self._scheduler_worker_admin()
            admin.drain_worker(worker_id)
            with suppress(NotFoundError):
                admin.delete_worker(worker_id)
        revoked = self._revoke_enrollment_authority(enrollment)
        self._remove_enrollment_private_network_identity(revoked)
        with self.services.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            current = enrollments.by_machine(
                enrollment.workspace_id,
                enrollment.machine_id,
                pool=enrollment.pool,
                for_update=True,
            )
            if current is None:
                raise KeyError(f"machine not found: {enrollment.machine_id}")
            WorkerRepository(session).records.delete(
                worker_id,
                workspace_id=enrollment.workspace_id,
            )
            deleted = MachineRepository(session).records.delete(
                current.machine_id,
                workspace_id=enrollment.workspace_id,
            )
            if not deleted:
                raise KeyError(f"machine not found: {current.machine_id}")
        self.compute_states.delete_agent_machine_state_for_machine(
            enrollment.workspace_id,
            enrollment.machine_id,
        )

    def _delete_pool_enrollments(
        self,
        workspace_id: str,
        unit: ComputeUnitRecord,
        *,
        deleting_workspace: bool = False,
        require_host_decommission: bool = False,
    ) -> None:
        with self.services.context.database.session() as session:
            enrollment_records = ComputeMachineEnrollmentRepository(session).list_for_unit(
                workspace_id,
                unit.capacity_owner_id,
            )
            credential_records = ComputeJoinCredentialRepository(session).list_for_unit(
                workspace_id,
                unit.capacity_owner_id,
            )
        if require_host_decommission and enrollment_records:
            raise ConflictError(
                f"compute pool {unit.name!r} still has enrolled self-hosted machines; "
                "run 'lazycloud-agent leave' on each owning host before deletion"
            )
        revoked_enrollments = [
            self._revoke_enrollment_authority(
                enrollment,
                deleting_workspace=deleting_workspace,
            )
            for enrollment in enrollment_records
        ]
        for enrollment in revoked_enrollments:
            self._remove_enrollment_private_network_identity(enrollment)
        for enrollment in enrollment_records:
            self.compute_states.delete_agent_token_state(enrollment.credential_hash)
            self.compute_states.delete_agent_machine_state_for_machine(
                enrollment.workspace_id,
                enrollment.machine_id,
            )
        for credential in credential_records:
            self.compute_states.revoke_join_token_state(credential.token_hash)
        with self.services.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            machines = MachineRepository(session)
            credentials = ComputeJoinCredentialRepository(session)
            enrollments.delete_for_unit(workspace_id, unit.capacity_owner_id)
            if not deleting_workspace:
                workers = WorkerRepository(session)
                for enrollment in enrollment_records:
                    workers.records.delete(
                        agent_machine_worker_id(enrollment.machine_id),
                        workspace_id=workspace_id,
                    )
                    machines.records.delete(enrollment.machine_id, workspace_id=workspace_id)
            credentials.delete_for_unit(workspace_id, unit.capacity_owner_id)

    def _revoke_enrollment_authority(
        self,
        enrollment: ComputeMachineEnrollmentRecord,
        *,
        deleting_workspace: bool = False,
    ) -> ComputeMachineEnrollmentRecord:
        current_time = utc_now()
        join_token_hash = ""
        with self.services.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            current = enrollments.by_machine(
                enrollment.workspace_id,
                enrollment.machine_id,
                pool=enrollment.pool,
                for_update=True,
            )
            if current is None:
                raise KeyError(f"machine not found: {enrollment.machine_id}")
            if current.status is not ComputeMachineEnrollmentStatus.Revoked:
                revoked = current.model_copy(
                    update={
                        "status": ComputeMachineEnrollmentStatus.Revoked,
                        "network_generation": current.network_generation + 1,
                        "network_phase": PrivateNetworkEnrollmentPhase.Revoked,
                        "schedulable": False,
                        "readiness_phase": MachineReadinessPhase.Revoked,
                        "revoked_at": current_time,
                        "updated_at": current_time,
                    }
                )
                current = (
                    enrollments.save_for_workspace_deletion(revoked)
                    if deleting_workspace
                    else enrollments.save(revoked)
                )
            if current.join_credential_id:
                credentials = ComputeJoinCredentialRepository(session)
                credential = credentials.get(current.join_credential_id, for_update=True)
                if credential is not None:
                    join_token_hash = credential.token_hash
                    if credential.status is ComputeCredentialStatus.Active:
                        revoked_credential = credential.revoke(now=current_time)
                        if deleting_workspace:
                            credentials.save_for_workspace_deletion(revoked_credential)
                        else:
                            credentials.save(revoked_credential)
            machines = MachineRepository(session)
            machine = machines.get(current.machine_id, workspace_id=enrollment.workspace_id)
            if (
                not deleting_workspace
                and machine is not None
                and machine.status is not ResourceStatus.Stopped
            ):
                machines.upsert(
                    machine.model_copy(
                        update={
                            "status": ResourceStatus.Stopped,
                            "updated_at": current_time,
                        }
                    ),
                    workspace_id=current.workspace_id,
                )
        self.compute_states.delete_agent_token_state(current.credential_hash)
        if join_token_hash:
            self.compute_states.revoke_join_token_state(join_token_hash)
        return current

    def _scheduler_worker_admin(self) -> SchedulerWorkerAdminService:
        return SchedulerWorkerAdminService(
            self.scheduler_worker_lookup,
            self.scheduler_container_lookup,
            stop_container=self._stop_container_by_id,
        )

    def _delete_pool_workers(self, capacity_owner_id: str) -> None:
        admin = self._scheduler_worker_admin()
        worker_ids = sorted(
            worker.worker_id
            for worker in self.scheduler_worker_lookup.list_workers()
            if worker.capacity_owner_id == capacity_owner_id
        )
        for worker_id in worker_ids:
            admin.drain_worker(worker_id)
            with suppress(NotFoundError):
                admin.delete_worker(worker_id)

    def join_agent(self, request: JoinAgentRequest) -> JoinAgentResponse:
        try:
            repository = self.compute_states
            join_request = AgentJoinRequest(
                machine_fingerprint=request.machine_fingerprint,
                hostname=request.hostname,
                os=request.os,
                arch=request.arch,
                cpu_count=request.cpu_count,
                cpu_millicores=request.cpu_millicores,
                memory_mb=request.memory_mb,
                gpu=request.gpu,
                gpu_ids=request.gpu_ids,
                gpu_count=request.gpu_count,
                executor=request.executor,
                schedulable=request.schedulable,
                preflight=request.preflight,
            )
            fingerprint_hash = hash_machine_fingerprint(request.machine_fingerprint)
            token_hash = hash_compute_token(request.join_token)
            current_time = utc_now()
            previous_token_hash = ""
            with self.services.context.database.session() as session:
                credentials = ComputeJoinCredentialRepository(session)
                enrollments = ComputeMachineEnrollmentRepository(session)
                credential = credentials.get_by_hash(token_hash, for_update=True)
                token_state = _join_token_state(credential)
                pool_state = self.unit_state_coordinator.private_unit_for_join_token(token_state)
                existing = (
                    enrollments.by_fingerprint(
                        token_state.owner_user_id,
                        fingerprint_hash,
                        for_update=True,
                    )
                    if token_state is not None and token_state.owner_user_id
                    else None
                )
                if (
                    existing is not None
                    and existing.status is not ComputeMachineEnrollmentStatus.Active
                ):
                    existing = None
                if (
                    existing is not None
                    and token_state is not None
                    and existing.workspace_id != token_state.workspace_id
                ):
                    # One host is one machine per account, so re-joining it under a
                    # second workspace would have to move its unit, its durable
                    # machine row, and whatever is running on it. Say which
                    # workspace holds it instead of silently re-homing the host.
                    raise ConflictError(
                        "this host is already joined to your account under another "
                        "workspace; run 'lazycloud-agent leave' on it first"
                    )
                existing_agent = _agent_state_from_enrollment(existing)
                existing_agents = (
                    [
                        _agent_state_from_enrollment(item)
                        for item in enrollments.list_for_unit(
                            token_state.workspace_id,
                            token_state.capacity_owner_id,
                        )
                        if item.status is ComputeMachineEnrollmentStatus.Active
                    ]
                    if token_state is not None
                    else []
                )
                plan = plan_agent_join(
                    token_state,
                    pool_state,
                    join_request,
                    existing_machine_gpus=[agent.gpus for agent in existing_agents if agent],
                    existing_agent=existing_agent,
                    credential_id=existing.id if existing is not None else "",
                )
                if (
                    not plan.accepted
                    or plan.agent_state is None
                    or credential is None
                    or token_state is None
                ):
                    raise InvalidInputError(plan.err_msg or "agent join rejected")
                agent_state = plan.agent_state
                bootstrap_pool = (
                    pool_state.model_copy(update={"config": plan.pool_config_update})
                    if pool_state is not None and plan.pool_config_update is not None
                    else pool_state
                )
                if bootstrap_pool is None:
                    raise NotFoundError("pool not found")
                bootstrap = build_agent_bootstrap_config(
                    agent_state.workspace_id,
                    bootstrap_pool,
                    self.gateway_endpoint,
                    self.agent_image,
                    gateway_runtime_http_url=self.runtime_origin(),
                    executor=agent_state.executor,
                )
                consumes_use = existing is None
                if consumes_use and credential.use_count >= credential.max_uses:
                    raise ConflictError("join token machine limit has been reached")
                readiness_phase = (
                    MachineReadinessPhase.Joining
                    if agent_state.preflight_passed
                    else MachineReadinessPhase.Blocked
                )
                machine_repository = MachineRepository(session)
                durable_machine = machine_repository.get(
                    agent_state.machine_id,
                    workspace_id=agent_state.workspace_id,
                )
                machine_repository.upsert(
                    Machine(
                        id=agent_state.machine_id,
                        pool=agent_state.pool,
                        capacity_owner_id=agent_state.capacity_owner_id,
                        provider="agent",
                        status=(
                            ResourceStatus.Created
                            if readiness_phase is MachineReadinessPhase.Joining
                            else ResourceStatus.Failed
                        ),
                        cpu=agent_state.cpu_millicores / 1000,
                        memory=f"{agent_state.memory_mb}Mi",
                        gpu=agent_state.gpus[0] if agent_state.gpus else None,
                        labels={
                            "hostname": agent_state.hostname,
                            "os": agent_state.os,
                            "arch": agent_state.arch,
                            "gpu_count": str(agent_state.gpu_count),
                            "source": "attached",
                        },
                        created_at=(
                            durable_machine.created_at
                            if durable_machine is not None
                            else current_time
                        ),
                        updated_at=current_time,
                    ),
                    workspace_id=agent_state.workspace_id,
                )
                worker_id = agent_machine_worker_id(agent_state.machine_id)
                worker_repository = WorkerRepository(session)
                durable_worker = worker_repository.get(
                    worker_id,
                    workspace_id=agent_state.workspace_id,
                )
                worker_repository.upsert(
                    Worker(
                        id=worker_id,
                        machine_id=agent_state.machine_id,
                        pool=agent_state.pool,
                        status=(
                            durable_worker.status
                            if durable_worker is not None
                            else ResourceStatus.Created
                        ),
                        labels={
                            **(durable_worker.labels if durable_worker is not None else {}),
                            "hostname": agent_state.hostname,
                            "source": "attached",
                        },
                        last_seen_at=current_time,
                        created_at=(
                            durable_worker.created_at
                            if durable_worker is not None
                            else current_time
                        ),
                    ),
                    workspace_id=agent_state.workspace_id,
                )
                previous_token_hash = existing.credential_hash if existing is not None else ""
                saved_enrollment = _save_machine_enrollment(
                    enrollments,
                    agent_state,
                    fingerprint_hash=fingerprint_hash,
                    join_credential_id=credential.id,
                    readiness_phase=readiness_phase,
                    existing=existing,
                )
                agent_state = agent_state.model_copy(
                    update={
                        "credential_id": saved_enrollment.id,
                        "credential_generation": saved_enrollment.credential_generation,
                    }
                )
                updated_credential = credential.with_use_count(
                    credential.use_count + int(consumes_use),
                    now=current_time,
                )
                credentials.save(updated_credential)
            if plan.should_save_pool and plan.pool_config_update is not None:
                self.unit_state_coordinator.save_compute_pool_config_update(
                    agent_state.workspace_id,
                    bootstrap_pool,
                    plan.pool_config_update,
                )
            if plan.should_save_join_token and plan.binding and plan.binding.state:
                repository.save_join_token_state(
                    plan.binding.state,
                    ttl_seconds=plan.binding.ttl_seconds or DEFAULT_PRIVATE_JOIN_TTL_SECONDS,
                )
            if previous_token_hash and previous_token_hash != agent_state.token_hash:
                repository.delete_agent_token_state(previous_token_hash)
            repository.save_join_token_state(
                token_state.model_copy(update={"use_count": updated_credential.use_count}),
                ttl_seconds=max(
                    int((updated_credential.expires_at - current_time).total_seconds()),
                    1,
                ),
            )
            repository.save_agent_token_state(agent_state)
            self.services.events.emit(
                "agent.join",
                resource_type="agent",
                resource_id=agent_state.machine_id,
                message=f"agent joined pool {agent_state.pool}",
                data={
                    "pool": agent_state.pool,
                    "machine_id": agent_state.machine_id,
                },
                workspace_id=agent_state.workspace_id,
            )
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return JoinAgentResponse(
            workspace_id=agent_state.workspace_id,
            pool=agent_state.pool,
            machine_id=plan.machine_id,
            agent_token=plan.agent_token,
            credential_id=agent_state.credential_id,
            credential_generation=agent_state.credential_generation,
            capacity_state=agent_state.capacity_state,
            bootstrap=bootstrap,
        )

    def leave_agent(self, request: LeaveAgentRequest) -> LeaveAgentResponse:
        state = self._require_agent_state(request.agent_token)
        if request.machine_id and request.machine_id != state.machine_id:
            raise InvalidInputError("agent leave machine identity does not match its credential")
        with self.services.context.database.session() as session:
            enrollment = ComputeMachineEnrollmentRepository(session).by_machine(
                state.workspace_id,
                state.machine_id,
                pool=state.pool,
            )
        if enrollment is None or enrollment.credential_hash != state.token_hash:
            raise InvalidInputError("agent credential is no longer current")
        self._record_agent_cache_destruction(state, request)
        self._delete_enrolled_machine(enrollment)
        return LeaveAgentResponse(machine_id=state.machine_id)

    def _record_agent_cache_destruction(
        self,
        state: ComputeAgentTokenState,
        request: LeaveAgentRequest,
    ) -> None:
        owner = WorkerCacheStorageOwnerRecord(
            kind=WorkerCacheStorageOwnerKind.Machine,
            owner_id=state.machine_id,
        )
        try:
            current = self.services.compute.source_cache_lifecycle.get(owner)
        except NotFoundError:
            if request.cache_generation_id or request.cache_session_fence is not None:
                raise ConflictError(
                    "agent cache destruction acknowledgment has no current server generation"
                ) from None
            return
        if current.complete:
            return
        if not request.cache_generation_id or request.cache_session_fence is None:
            raise ConflictError(
                "current machine-owned source cache must be destroyed by "
                "'lazycloud-agent leave' before authority can be removed"
            )
        if (
            current.generation_id != request.cache_generation_id
            or current.session_fence != request.cache_session_fence
            or current.worker_id != agent_machine_worker_id(state.machine_id)
        ):
            raise ConflictError(
                "agent cache destruction acknowledgment is not the current machine session"
            )
        destroyed = self.services.compute.source_cache_lifecycle.record_destroyed(
            WorkerCacheStorageDestructionEvidence(
                owner=owner,
                generation_id=request.cache_generation_id,
                observed_at=utc_now(),
            )
        )
        if not destroyed.complete:
            raise ConflictError("machine-owned source cache destruction is incomplete")

    def register_agent_private_network(
        self,
        request: RegisterAgentPrivateNetworkRequest,
    ) -> RegisterAgentPrivateNetworkResponse:
        try:
            public_key = validate_wireguard_public_key(request.public_key)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        state = self._require_agent_state(request.agent_token)
        now = utc_now()
        with self.services.context.database.session() as session:
            gateway = WireGuardGatewayRepository(session).current()
            if gateway is None:
                raise UpstreamUnavailableError("WireGuard gateway is not ready")
            enrollments = ComputeMachineEnrollmentRepository(session)
            enrollment = enrollments.by_machine(
                state.workspace_id,
                state.machine_id,
                pool=state.pool,
                for_update=True,
            )
            if (
                enrollment is None
                or enrollment.status is not ComputeMachineEnrollmentStatus.Active
                or enrollment.credential_hash != state.token_hash
            ):
                raise InvalidInputError("agent credential is no longer current")
            peers = WireGuardPeerRepository(session)
            peer = peers.by_enrollment(enrollment.id, for_update=True)
            if peer is None:
                peers.lock_allocator()
                peer = peers.by_enrollment(enrollment.id, for_update=True)
            if peer is None:
                generation = enrollment.network_generation + 1
                peer = WireGuardPeer(
                    id=str(uuid4()),
                    enrollment_id=enrollment.id,
                    workspace_id=enrollment.workspace_id,
                    machine_id=enrollment.machine_id,
                    public_key=public_key,
                    address=allocate_wireguard_agent_address(
                        enrollment.id,
                        is_allocated=peers.address_allocated,
                    ),
                    generation=generation,
                    created_at=now,
                    updated_at=now,
                )
            else:
                generation = max(peer.generation, enrollment.network_generation) + 1
                updates: dict[str, object] = {
                    "generation": generation,
                    "last_handshake_at": None,
                    "updated_at": now,
                }
                if peer.public_key != public_key or peer.status is WireGuardPeerStatus.Revoked:
                    updates.update(
                        {
                            "public_key": public_key,
                            "status": WireGuardPeerStatus.Active,
                            "revoked_at": None,
                        }
                    )
                peer = peer.model_copy(update=updates)
            saved_peer = peers.save(peer)
            enrollments.save(
                enrollment.model_copy(
                    update={
                        "heartbeat_confirmed": False,
                        "schedulable": False,
                        "readiness_phase": MachineReadinessPhase.Joining,
                        "network_generation": generation,
                        "network_phase": PrivateNetworkEnrollmentPhase.AwaitingHandshake,
                        "network_peer_id": saved_peer.id,
                        "network_public_key": saved_peer.public_key,
                        "network_address": saved_peer.address,
                        "network_verified_at": None,
                        "network_failure_detail": "",
                        "updated_at": now,
                    }
                )
            )
        self.compute_states.save_agent_token_state(
            state.model_copy(update={"heartbeat_confirmed": False, "schedulable": False})
        )
        configuration = WireGuardPeerConfiguration(
            peer_id=saved_peer.id,
            address=saved_peer.address,
            server_public_key=gateway.public_key,
            endpoint=gateway.endpoint,
            allowed_ips=(str(WIREGUARD_PLATFORM_NETWORK),),
            persistent_keepalive_seconds=WIREGUARD_KEEPALIVE_SECONDS,
            generation=saved_peer.generation,
        )
        return RegisterAgentPrivateNetworkResponse(**configuration.model_dump())

    def list_agent_routes(
        self,
        request: ListAgentRoutesRequest,
    ) -> ListAgentRoutesResponse:
        try:
            state = self._require_agent_state(request.agent_token)
            routes = self.compute_states.list_agent_route_states(
                state.workspace_id,
                state.capacity_owner_id,
                state.machine_id,
            )
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return ListAgentRoutesResponse(
            routes=[self._agent_route_view(route) for route in routes],
        )

    def update_agent_route_status(
        self,
        request: UpdateAgentRouteStatusRequest,
    ) -> UpdateAgentRouteStatusResponse:
        try:
            state = self._require_agent_state(request.agent_token)
            route = self.compute_states.get_agent_route_state(
                state.workspace_id,
                state.capacity_owner_id,
                state.machine_id,
                request.route_id,
            )
            plan = plan_route_status_update(
                state,
                route if route is not None else None,
                AgentRouteStatusRequest(
                    route_id=request.route_id,
                    state=request.state,
                    proxy_target=request.proxy_target,
                    error=request.error,
                    attrs=request.attrs,
                ),
            )
            if plan.already_gone:
                # Nothing to save, and nothing wrong. The route the agent named
                # no longer exists, which is the state this update was asking
                # for; raising here bricked the agent on an ordinary race.
                return UpdateAgentRouteStatusResponse(route_id=request.route_id)
            if not plan.accepted or plan.updated is None:
                raise InvalidInputError(plan.err_msg or "agent route status update rejected")
            self.compute_states.save_agent_route_state(plan.updated)
            self.scheduler_container_lookup.update_backend_route(plan.updated)
            if plan.should_emit_event:
                event_data: dict[str, JsonValue] = dict(plan.event_attrs)
                self.services.events.emit(
                    "agent.route",
                    resource_type="agent-route",
                    resource_id=request.route_id,
                    message=f"agent route {request.route_id} changed state",
                    data=event_data,
                    workspace_id=state.workspace_id,
                )
            if plan.should_prewarm:
                self._prewarm_route(plan.updated, state)
        except (KeyError, ValueError) as exc:
            raise _domain_error(exc) from exc
        return UpdateAgentRouteStatusResponse(route_id=request.route_id)

    def _prewarm_route(
        self,
        route: AgentBackendRoute,
        agent_state: ComputeAgentTokenState,
    ) -> None:
        self.route_prewarmer.prewarm_route(route, agent_state)

    def stream_agent(self, request: StreamAgentRequest) -> StreamAgentResponse:
        try:
            provided = self._agent_state_for_token(request.agent_token)
            current = (
                self.compute_states.get_agent_token_state(provided.token_hash)
                if provided is not None
                else None
            )
            routes = []
            slots = []
            if provided is not None:
                routes = [
                    route
                    for route in self.compute_states.list_agent_route_states(
                        provided.workspace_id,
                        provided.capacity_owner_id,
                        provided.machine_id,
                    )
                ]
                slots = self.compute_states.list_agent_worker_slot_states(
                    provided.workspace_id,
                    provided.capacity_owner_id,
                    provided.machine_id,
                )
            snapshot = plan_agent_stream_snapshot(provided, current, routes, slots)
            current_state = snapshot.current.state
            if not snapshot.current.accepted or current_state is None:
                return StreamAgentResponse(ok=False, err_msg=snapshot.current.err_msg)
            try:
                self._require_verified_private_network_identity(current_state)
            except ValueError as exc:
                return StreamAgentResponse(ok=False, err_msg=str(exc), retryable=True)
            heartbeat = plan_agent_heartbeat_touch(current_state)
            if heartbeat.should_save and heartbeat.state is not None:
                response_state = self._persist_agent_state(heartbeat.state)
            else:
                response_state = heartbeat.state or current_state
            bootstrap_unit = self.unit_state_coordinator.unit_by_capacity_owner(
                response_state.capacity_owner_id,
                workspace_id=response_state.workspace_id,
            )
            agent_slots = self._agent_slots_for_machine(
                response_state,
                billing_owner=billing_owner_for_unit(bootstrap_unit),
            )
            bootstrap_pool = self.unit_state_coordinator.private_unit_state(
                bootstrap_unit,
                workspace_id=response_state.workspace_id,
            )
            bootstrap = build_agent_bootstrap_config(
                response_state.workspace_id,
                bootstrap_pool,
                self.gateway_endpoint,
                self.agent_image,
                gateway_runtime_http_url=self.runtime_origin(),
                executor=response_state.executor,
            )
        except (KeyError, ValueError) as exc:
            return StreamAgentResponse(ok=False, err_msg=str(exc))
        return StreamAgentResponse(
            ok=True,
            credential_id=response_state.credential_id,
            credential_generation=response_state.credential_generation,
            capacity_state=response_state.capacity_state,
            bootstrap=bootstrap,
            routes=[self._agent_route_view(route) for route in snapshot.routes],
            slots=[agent_worker_slot_view(slot) for slot in agent_slots],
        )

    def record_agent_capacity_interruption(
        self,
        request: AgentCapacityInterruptionRequest,
    ) -> AgentCapacityInterruptionResponse:
        token_hash = hash_compute_token(request.agent_token)
        changed = False
        with self.services.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            enrollment = enrollments.by_credential_hash(token_hash, for_update=True)
            if enrollment is None or enrollment.status is not ComputeMachineEnrollmentStatus.Active:
                raise ConflictError("agent credential is no longer current")
            if (
                enrollment.machine_id != request.machine_id
                or enrollment.id != request.credential_id
                or enrollment.credential_generation != request.credential_generation
            ):
                raise ConflictError("agent capacity interruption session fence is stale")
            current_observed_at = enrollment.capacity_observed_at
            if current_observed_at is not None and request.observed_at < current_observed_at:
                raise ConflictError("agent capacity interruption observation is stale")
            same_transition = (
                enrollment.capacity_state is request.state
                and enrollment.capacity_reason == request.reason
                and enrollment.capacity_observed_at == request.observed_at
                and enrollment.capacity_notice_at == request.notice_at
            )
            if current_observed_at == request.observed_at and not same_transition:
                raise ConflictError("agent capacity interruption observation conflicts")
            updated = enrollment
            if not same_transition:
                changed = True
                updated = enrollments.save(
                    enrollment.model_copy(
                        update={
                            "schedulable": False,
                            "capacity_state": request.state,
                            "capacity_reason": request.reason,
                            "capacity_observed_at": request.observed_at,
                            "capacity_notice_at": request.notice_at,
                            "updated_at": utc_now(),
                        }
                    )
                )
        state = _agent_state_from_enrollment(updated)
        if state is None:
            raise ConflictError("agent enrollment is no longer active")
        self.compute_states.save_agent_token_state(state)
        if self.capacity_interruption_sink is not None:
            self.capacity_interruption_sink.preempt_agent_capacity(state)
        if changed:
            self.services.events.emit(
                "agent.capacity.interruption",
                resource_type="agent",
                resource_id=state.machine_id,
                message=f"agent capacity changed to {state.capacity_state.value}",
                data={
                    "pool": state.pool,
                    "machine_id": state.machine_id,
                    "state": state.capacity_state.value,
                    "reason": state.capacity_reason,
                    "credential_generation": state.credential_generation,
                },
                workspace_id=state.workspace_id,
            )
        return AgentCapacityInterruptionResponse(
            machine_id=state.machine_id,
            credential_id=state.credential_id,
            credential_generation=state.credential_generation,
            state=state.capacity_state,
            reason=state.capacity_reason,
            observed_at=state.capacity_observed_at or request.observed_at,
            notice_at=state.capacity_notice_at,
            changed=changed,
        )

    def _agent_route_view(self, route: AgentBackendRoute | AgentBackendRoute) -> AgentRoute:
        if self.route_authenticator is None:
            raise RuntimeError("backend route authenticator is not configured")
        return agent_route_view(
            route,
            proxy_auth_token=self.route_authenticator.credential(route.route_id),
        )

    def _agent_slots_for_machine(
        self,
        agent_state: ComputeAgentTokenState,
        *,
        billing_owner: UsageBillingOwner,
    ) -> list[ComputeAgentWorkerSlotState]:
        slots = self.compute_states.list_agent_worker_slot_states(
            agent_state.workspace_id,
            agent_state.capacity_owner_id,
            agent_state.machine_id,
        )
        worker = self._agent_machine_worker(agent_state)
        if worker is None:
            self._prune_agent_worker_slots(agent_state, keep_worker_id="", slots=slots)
            return []

        existing = next((slot for slot in slots if slot.worker_id == worker.worker_id), None)
        token_plan = self._agent_worker_token(
            agent_state,
            worker_id=worker.worker_id,
            existing_slot=existing,
            billing_owner=billing_owner,
        )
        slot_plan = plan_agent_worker_slot(
            agent_state,
            agent_worker_record(worker),
            slots,
            token_plan,
            billing_owner=billing_owner,
            cluster_name=self.agent_cluster_name,
            worker_image=agent_worker_image(
                self.agent_worker_image_registry,
                self.agent_worker_image_name,
                self.agent_worker_image_tag,
            ),
        )
        self._prune_agent_worker_slots(
            agent_state,
            keep_worker_id=worker.worker_id,
            slots=slots,
        )
        if not slot_plan.accepted or slot_plan.slot is None:
            if token_plan.should_create and token_plan.worker_token_id:
                self._revoke_agent_worker_token(
                    agent_state.workspace_id,
                    token_plan.worker_token_id,
                )
            raise ValueError(slot_plan.err_msg or "agent worker slot could not be created")

        slot = slot_plan.slot.model_copy(
            update={"metadata": {**slot_plan.slot.metadata, "worker_token": slot_plan.worker_token}}
        )
        try:
            saved = self.compute_states.save_agent_worker_slot_state(slot)
        except Exception:
            if token_plan.should_create and token_plan.worker_token_id:
                self._revoke_agent_worker_token(
                    agent_state.workspace_id,
                    token_plan.worker_token_id,
                )
            raise
        if slot_plan.created_slot:
            self.services.events.emit(
                "agent.worker-slot",
                resource_type="agent",
                resource_id=agent_state.machine_id,
                message=f"agent worker slot created for {worker.worker_id}",
                data={
                    "pool": agent_state.pool,
                    "machine_id": agent_state.machine_id,
                    "worker_id": worker.worker_id,
                },
                workspace_id=agent_state.workspace_id,
            )
        return [saved]

    def _agent_machine_worker(
        self,
        agent_state: ComputeAgentTokenState,
    ) -> SchedulerWorkerRecord | None:
        worker = self.scheduler_worker_lookup.get_worker(
            agent_machine_worker_id(agent_state.machine_id)
        )
        if worker is None:
            return None
        if (
            worker.machine_id != agent_state.machine_id
            or worker.pool != agent_state.pool
            or worker.status is SchedulerWorkerStatus.Unavailable
        ):
            return None
        with suppress(SchedulerRepositoryError):
            worker = self.scheduler_worker_lookup.reconcile_worker_capacity(worker.worker_id)
        return worker

    def _agent_worker_token(
        self,
        agent_state: ComputeAgentTokenState,
        *,
        worker_id: str,
        existing_slot: ComputeAgentWorkerSlotState | None,
        billing_owner: UsageBillingOwner,
    ) -> AgentWorkerTokenPlan:
        """Mint the credential this machine's worker registers with.

        Its kind is the whole of the tenancy decision: a worker registers as
        private or shared from the token alone, and everything downstream is
        stamped from that. The fleet is capacity the platform bought, so its
        machines serve every account; a customer's connection provisions hardware
        that account owns, and its machines serve only that account.
        """
        existing_token = self._existing_agent_worker_token(agent_state, existing_slot)
        token_plan = plan_agent_worker_token(
            existing_slot,
            expected_worker_id=worker_id,
            existing_token=existing_token,
        )
        if token_plan.accepted:
            return token_plan

        if existing_slot is not None and existing_slot.worker_token_id:
            self._revoke_agent_worker_token(
                agent_state.workspace_id,
                existing_slot.worker_token_id,
            )
        raw_token, token_record = self.services.auth.create_token(
            f"agent-worker-{agent_state.machine_id}-{uuid4().hex[:8]}",
            scopes=[AuthScope.Worker.value],
            kind=(
                TokenKind.Worker
                if billing_owner is UsageBillingOwner.PlatformFleet
                else TokenKind.WorkerPrivate
            ),
            workspace_id=agent_state.workspace_id,
            worker_id=worker_id,
            reusable=True,
        )
        return plan_agent_worker_token(
            existing_slot,
            expected_worker_id=worker_id,
            existing_token=existing_token,
            created_token=WorkerTokenRecord(
                key=raw_token,
                external_id=token_record.id,
                active=token_record.status.value == "active",
                disabled_by_cluster_admin=token_record.disabled_by_admin,
                worker_id=token_record.worker_id,
                reusable=token_record.reusable,
            ),
        )

    def _existing_agent_worker_token(
        self,
        agent_state: ComputeAgentTokenState,
        existing_slot: ComputeAgentWorkerSlotState | None,
    ) -> WorkerTokenRecord | None:
        if existing_slot is None or not existing_slot.worker_token_id:
            return None
        raw_token = existing_slot.metadata.get("worker_token")
        if not isinstance(raw_token, str) or raw_token == "":
            return None
        record = self.services.auth.get_workspace_token(
            agent_state.workspace_id,
            existing_slot.worker_token_id,
        )
        if record is None:
            return None
        token_type = (
            WorkerTokenKind.WorkerPrivate
            if record.kind is TokenKind.WorkerPrivate
            else WorkerTokenKind.Worker
        )
        return WorkerTokenRecord(
            key=raw_token,
            external_id=record.id,
            active=record.status is TokenStatus.Active,
            disabled_by_cluster_admin=record.disabled_by_admin,
            token_type=token_type,
            worker_id=record.worker_id,
            reusable=record.reusable,
        )

    def _prune_agent_worker_slots(
        self,
        agent_state: ComputeAgentTokenState,
        *,
        keep_worker_id: str,
        slots: list[ComputeAgentWorkerSlotState],
    ) -> None:
        for slot in slots:
            if not slot.worker_id or slot.worker_id == keep_worker_id:
                continue
            if slot.worker_token_id:
                self._revoke_agent_worker_token(
                    agent_state.workspace_id,
                    slot.worker_token_id,
                )
            self.compute_states.delete_agent_worker_slot_state(
                agent_state.workspace_id,
                agent_state.capacity_owner_id,
                agent_state.machine_id,
                slot.worker_id,
            )
            self.services.events.emit(
                "agent.worker-slot.pruned",
                resource_type="agent",
                resource_id=agent_state.machine_id,
                message=f"agent worker slot pruned for {slot.worker_id}",
                data={
                    "pool": agent_state.pool,
                    "machine_id": agent_state.machine_id,
                    "worker_id": slot.worker_id,
                },
                workspace_id=agent_state.workspace_id,
            )

    def _revoke_agent_worker_token(self, workspace_id: str, token_id: str) -> None:
        token = self.services.auth.get_workspace_token(workspace_id, token_id)
        if token is not None and token.status is TokenStatus.Active:
            self.services.auth.revoke_workspace_token(workspace_id, token_id)

    def stream_agent_telemetry(
        self,
        request: AgentTelemetryRequest,
    ) -> AgentTelemetryResponse:
        try:
            state = self._agent_state_for_token(request.agent_token)
            decision = validate_agent_telemetry_token(
                request.agent_token,
                state_found=state is not None,
            )
            if not decision.accepted or state is None:
                return AgentTelemetryResponse(ok=False, err_msg=decision.reason)
            updated_state = state
            if request.metrics is not None:
                snapshot: AgentMetricSnapshotProtocol = HttpAgentMetricSnapshot.model_validate(
                    request.metrics.model_dump(mode="json")
                )
                plan = plan_agent_metric_update(
                    agent_telemetry_state(state),
                    snapshot,
                    pool=PoolTelemetryState(transport=agent_pool_transport(state)),
                )
                metadata: dict[str, JsonValue] = {
                    **state.metadata,
                    "metrics": plan.metrics.model_dump(mode="json"),
                    "node_usage_seconds": plan.node_usage_seconds,
                    "node_usage_metadata": plan.node_usage_metadata,
                }
                updated_state = state.model_copy(
                    update={
                        "last_heartbeat_at": plan.heartbeat_at,
                        "last_disconnect_at": plan.last_disconnect_at,
                        "heartbeat_confirmed": True,
                        "schedulable": (
                            state.preflight_passed
                            and state.capacity_state is AgentCapacityState.Available
                        ),
                        "metadata": metadata,
                    }
                )
                self._record_node_usage(updated_state, plan)
                self._persist_agent_state(updated_state)
                event_data: dict[str, JsonValue] = dict(plan.event_attrs)
                self.services.events.emit(
                    "agent.metrics",
                    resource_type="agent",
                    resource_id=state.machine_id,
                    message=f"agent metrics received for {state.machine_id}",
                    data=event_data,
                    workspace_id=state.workspace_id,
                )
            for log in request.logs:
                redacted_line = redact_telemetry_line(log.line)
                self.services.events.emit(
                    "agent.log",
                    resource_type="agent",
                    resource_id=updated_state.machine_id,
                    message=redacted_line,
                    data=log.model_copy(update={"line": redacted_line}).model_dump(mode="json"),
                    workspace_id=updated_state.workspace_id,
                )
            for event in request.events:
                self.services.events.emit(
                    event.action or "agent.event",
                    resource_type="agent",
                    resource_id=updated_state.machine_id,
                    message=event.message,
                    data=event.model_dump(mode="json"),
                    workspace_id=updated_state.workspace_id,
                )
        except (KeyError, ValueError) as exc:
            return AgentTelemetryResponse(ok=False, err_msg=str(exc))
        return AgentTelemetryResponse(ok=True)

    def _record_node_usage(
        self,
        state: ComputeAgentTokenState,
        plan: AgentMetricUpdatePlan,
    ) -> None:
        seconds = plan.node_usage_seconds
        if seconds <= 0:
            return
        metadata = plan.node_usage_metadata
        heartbeat_at = plan.heartbeat_at
        workspace_id = self._usage_workspace_id(state.workspace_id)
        self.services.usage.record(
            id=usage_record_id(
                UsageMetric.NodeUsage.value,
                workspace_id,
                state.machine_id,
                heartbeat_at.isoformat() if heartbeat_at is not None else "",
            ),
            workspace_id=workspace_id,
            resource_type="node",
            resource_id=state.machine_id,
            metric=UsageMetric.NodeUsage,
            quantity=float(seconds),
            unit=UsageUnit.Seconds,
            labels={
                "pool": state.pool,
                "machine_id": state.machine_id,
                "node_type": str(metadata.get("node_type") or ""),
                "capacity_source": str(metadata.get("capacity_source") or ""),
                "pool_mode": str(metadata.get("pool_mode") or ""),
                "transport": str(metadata.get("transport") or ""),
            },
            metadata=metadata,
        )

    def _usage_workspace_id(self, workspace: str) -> str:
        with self.services.context.database.session() as session:
            return self.services.context.workspace(session, workspace or "default").id

    def _object_by_key(self, bucket: str, key: str, *, workspace_id: str) -> ObjectRecord | None:
        if not key:
            return None
        try:
            return self.objects.get_for_workspace(
                workspace_id=workspace_id,
                bucket=bucket,
                key=key,
            )
        except NotFoundError:
            return None

    def _container_for_workspace(
        self,
        container_id: str,
        workspace_id: str,
    ) -> ContainerRecord:
        container = self.services.containers.get(container_id)
        if container.workspace_id != workspace_id:
            msg = f"container not found: {container_id}"
            raise NotFoundError(msg)
        return container

    def _task_for_workspace(self, task_id: str, workspace_id: str) -> Task:
        task = self.services.tasks.get(task_id)
        if task.workspace_id != workspace_id:
            msg = f"task not found: {task_id}"
            raise NotFoundError(msg)
        return task

    def _agent_state_for_token(self, agent_token: str) -> ComputeAgentTokenState | None:
        token_hash = hash_compute_token(agent_token)
        with self.services.context.database.session() as session:
            enrollment = ComputeMachineEnrollmentRepository(session).by_credential_hash(token_hash)
        if enrollment is None or enrollment.status is not ComputeMachineEnrollmentStatus.Active:
            return None
        state = self.compute_states.get_agent_token_state(token_hash)
        if (
            state is not None
            and state.credential_id == enrollment.id
            and state.credential_generation == enrollment.credential_generation
        ):
            authoritative = state.model_copy(
                update={
                    # The enrollment owns tenancy, so a hot record that predates the
                    # machine having an account converges here rather than staying
                    # unschedulable until the agent re-joins.
                    "owner_user_id": enrollment.user_id,
                    "schedulable": enrollment.schedulable,
                    "capacity_state": enrollment.capacity_state,
                    "capacity_reason": enrollment.capacity_reason,
                    "capacity_observed_at": enrollment.capacity_observed_at,
                    "capacity_notice_at": enrollment.capacity_notice_at,
                }
            )
            if authoritative != state:
                self.compute_states.save_agent_token_state(authoritative)
            return authoritative
        state = _agent_state_from_enrollment(enrollment)
        if state is not None:
            self.compute_states.save_agent_token_state(state)
        return state

    async def sweep_disconnected_agents(
        self,
        database: AsyncDatabaseClient,
        redis: AsyncRedisClient,
        *,
        now: datetime | None = None,
        limit: int = DISCONNECT_SWEEP_LIMIT,
    ) -> list[str]:
        """Record the machines that stopped reporting, and tell their owners.

        The scan is a shortlist, never the decision. Each machine is decided
        again under its own row lock, so a heartbeat that lands between the two
        keeps the machine, and two control planes sweeping at once still write
        and tell once.

        Returns the machine ids newly marked.
        """

        current_time = now or utc_now()
        cutoff = current_time - timedelta(seconds=AGENT_HEARTBEAT_TIMEOUT_SECONDS)

        def list_candidates(session: Session) -> list[ComputeMachineEnrollmentRecord]:
            return ComputeMachineEnrollmentRepository(session).list_silent_since(
                cutoff=cutoff,
                limit=limit,
            )

        candidates = await database.run_transaction(list_candidates)
        marked: list[str] = []
        compute_states = AsyncRedisComputeStateRepository(redis)
        for candidate in candidates:
            # One machine that cannot be written must not cost the rest their
            # sweep. An enrollment whose workspace is no longer active raises
            # from the machine write, and it stays a candidate, so letting it
            # escape would put it at the head of every later scan and stop the
            # fleet being swept at all.
            try:
                disconnected = await database.run_transaction(
                    partial(
                        self._mark_agent_disconnected_in_session,
                        candidate=candidate,
                        now=current_time,
                    )
                )
            except (DomainError, ValueError) as exc:
                await self._record_disconnect_failure(database, candidate, exc)
                continue
            if disconnected is None:
                continue
            await compute_states.save_agent_token_state(disconnected)
            marked.append(disconnected.machine_id)
        return marked

    def _mark_agent_disconnected_in_session(
        self,
        session: Session,
        candidate: ComputeMachineEnrollmentRecord,
        *,
        now: datetime,
    ) -> ComputeAgentTokenState | None:
        """Write the disconnect if the locked row still says the machine is gone.

        Narrow on purpose: only the disconnect and the phase it implies. The
        heartbeat path re-states the whole enrollment from what the machine
        reported, and re-stating it from a row read seconds ago would put back a
        heartbeat that arrived in between, writing off a machine that had just
        come back.

        The row lock plus the re-plan is what makes the telling exactly once.
        Whichever caller commits the disconnect is the one that saw a plan
        asking for it; the next reads the row it wrote and has nothing to do.
        """

        enrollments = ComputeMachineEnrollmentRepository(session)
        enrollment = enrollments.by_machine(
            candidate.workspace_id,
            candidate.machine_id,
            pool=candidate.pool,
            for_update=True,
        )
        if enrollment is None or enrollment.status is not ComputeMachineEnrollmentStatus.Active:
            return None
        state = _agent_state_from_enrollment(enrollment)
        if state is None:
            return None
        plan = plan_agent_disconnect(agent_telemetry_state(state), now=now)
        if plan.action is not AgentDisconnectAction.MarkDisconnected:
            return None
        state = state.model_copy(update={"last_disconnect_at": plan.disconnected_at})
        readiness_phase = _agent_readiness_phase(state)
        enrollments.save(
            enrollment.model_copy(
                update={
                    "last_disconnect_at": plan.disconnected_at,
                    "readiness_phase": readiness_phase,
                    "updated_at": now,
                }
            )
        )
        machines = MachineRepository(session)
        machine = machines.get(state.machine_id, workspace_id=state.workspace_id)
        if machine is not None:
            machines.upsert(
                machine.model_copy(
                    update={
                        "status": MACHINE_STATUS_FOR_READINESS[readiness_phase],
                        "updated_at": now,
                    }
                ),
                workspace_id=state.workspace_id,
            )
        self._write_agent_disconnected_event(session, state, plan.reason, now=now)
        return state

    async def _record_disconnect_failure(
        self,
        database: AsyncDatabaseClient,
        candidate: ComputeMachineEnrollmentRecord,
        exc: Exception,
    ) -> None:
        """Say that a machine could not be written off, without saying it to the customer.

        Cluster-scoped: failing to record a disconnect is a defect in this
        platform, and the workspace feed belongs to the customer whose machine
        it failed to describe.
        """

        # Recording the failure must never replace the failure being recorded.
        def record(session: Session) -> None:
            self.services.events.emit_in_session(
                session,
                "agent.disconnect.failed",
                resource_type="agent",
                resource_id=candidate.machine_id,
                message=(
                    f"could not mark machine {candidate.machine_id} disconnected "
                    f"({type(exc).__name__})"
                ),
                level=EventLevel.Error,
                data={
                    "machine_id": candidate.machine_id,
                    "pool": candidate.pool,
                    "workspace_id": candidate.workspace_id,
                    "error_type": type(exc).__name__,
                },
            )

        with suppress(Exception):
            await database.run_transaction(record)

    def _write_agent_disconnected_event(
        self,
        session: Session,
        state: ComputeAgentTokenState,
        reason: str,
        *,
        now: datetime,
    ) -> None:
        """Tell the owner in the transaction that writes the disconnect.

        The disconnect is what takes this machine out of the next scan, so a
        telling that failed after it committed is never retried by anything.
        Written on the caller's session, the two land together or neither does.
        """

        telemetry = agent_telemetry_state(state)
        last_seen = agent_machine_last_seen(telemetry)
        silence = agent_silence_description(telemetry, now=now)
        data: dict[str, JsonValue] = {
            "machine_id": state.machine_id,
            "pool": state.pool,
            "capacity_owner_id": state.capacity_owner_id,
            "last_seen_at": last_seen.isoformat() if last_seen is not None else None,
            "reason": reason,
        }
        self.services.events.emit_in_session(
            session,
            "agent.disconnected",
            resource_type="agent",
            resource_id=state.machine_id,
            message=f"machine {state.machine_id} stopped reporting {silence} ago",
            level=EventLevel.Warning,
            data=data,
            workspace_id=state.workspace_id,
        )

    def _persist_agent_state(self, state: ComputeAgentTokenState) -> ComputeAgentTokenState:
        readiness_phase = _agent_readiness_phase(state)
        current_time = utc_now()
        with self.services.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            enrollment = enrollments.by_machine(
                state.workspace_id,
                state.machine_id,
                pool=state.pool,
                for_update=True,
            )
            if (
                enrollment is None
                or enrollment.status is not ComputeMachineEnrollmentStatus.Active
                or enrollment.credential_hash != state.token_hash
                or enrollment.id != state.credential_id
                or enrollment.credential_generation != state.credential_generation
            ):
                raise ValueError("agent credential is no longer current")
            state = state.model_copy(
                update={
                    "owner_user_id": enrollment.user_id,
                    "capacity_state": enrollment.capacity_state,
                    "capacity_reason": enrollment.capacity_reason,
                    "capacity_observed_at": enrollment.capacity_observed_at,
                    "capacity_notice_at": enrollment.capacity_notice_at,
                    "schedulable": (
                        state.schedulable
                        and enrollment.capacity_state is AgentCapacityState.Available
                    ),
                }
            )
            snapshot = _machine_enrollment_snapshot(
                state,
                fingerprint_hash=enrollment.machine_fingerprint_hash,
                join_credential_id=enrollment.join_credential_id,
                readiness_phase=readiness_phase,
            )
            enrollments.save(snapshot.update_record(enrollment, updated_at=current_time))
            machines = MachineRepository(session)
            machine = machines.get(state.machine_id, workspace_id=state.workspace_id)
            if machine is None:
                raise ValueError("agent machine no longer exists")
            machine_status = MACHINE_STATUS_FOR_READINESS[readiness_phase]
            machines.upsert(
                Machine(
                    id=machine.id,
                    pool=machine.pool,
                    # Rebuilt field by field, so anything omitted here is reset
                    # on every heartbeat.
                    capacity_owner_id=state.capacity_owner_id,
                    provider=machine.provider,
                    status=machine_status,
                    cpu=machine.cpu,
                    memory=machine.memory,
                    gpu=machine.gpu,
                    address=machine.address,
                    labels=machine.labels,
                    created_at=machine.created_at,
                    updated_at=current_time,
                ),
                workspace_id=state.workspace_id,
            )
        self.compute_states.save_agent_token_state(state)
        return state

    def _require_agent_state(self, agent_token: str) -> ComputeAgentTokenState:
        state = self._agent_state_for_token(agent_token)
        if state is None:
            msg = "invalid agent token"
            raise ValueError(msg)
        return state

    def _require_verified_private_network_identity(
        self,
        state: ComputeAgentTokenState,
    ) -> ComputeMachineEnrollmentRecord:
        enrollment, peer = self._private_network_identity(state)
        if enrollment.network_phase is PrivateNetworkEnrollmentPhase.Connected:
            return enrollment
        target = (
            f"{ipaddress.ip_interface(enrollment.network_address).ip}:"
            f"{WIREGUARD_AGENT_ROUTE_PROXY_PORT}"
        )
        try:
            connection = self.private_network_connector.connect(
                target,
                PRIVATE_NETWORK_PROBE_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            detail = (
                "WireGuard connected, but LazyCloud could not reach TCP "
                f"{WIREGUARD_AGENT_ROUTE_PROXY_PORT} on {WIREGUARD_INTERFACE}; allow traffic "
                f"from {WIREGUARD_PLATFORM_NETWORK}"
            )
            self._record_private_network_failure(
                state,
                peer_id=peer.id,
                generation=peer.generation,
                detail=detail,
            )
            raise ValueError(detail) from exc
        connection.close()

        with self.services.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            enrollment = enrollments.by_machine(
                state.workspace_id,
                state.machine_id,
                pool=state.pool,
                for_update=True,
            )
            if enrollment is None or not enrollment.network_peer_id:
                raise ValueError("agent WireGuard peer is not registered")
            peer = WireGuardPeerRepository(session).by_enrollment(
                enrollment.id,
                for_update=True,
            )
            if peer is None or not _wireguard_peer_matches_enrollment(peer, enrollment):
                raise ValueError("agent WireGuard peer does not match its enrollment")
            if peer.last_handshake_at is None:
                raise ValueError("agent WireGuard handshake has not been observed")
            verified_at = utc_now()
            verified = enrollment.model_copy(
                update={
                    "network_phase": PrivateNetworkEnrollmentPhase.Connected,
                    "network_verified_at": verified_at,
                    "network_failure_detail": "",
                    "updated_at": verified_at,
                }
            )
            return enrollments.save(verified)

    def _private_network_identity(
        self,
        state: ComputeAgentTokenState,
    ) -> tuple[ComputeMachineEnrollmentRecord, WireGuardPeer]:
        with self.services.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            enrollment = enrollments.by_machine(
                state.workspace_id,
                state.machine_id,
                pool=state.pool,
            )
            if enrollment is None or not enrollment.network_peer_id:
                raise ValueError("agent WireGuard peer is not registered")
            peer = WireGuardPeerRepository(session).by_enrollment(enrollment.id)
        if peer is None or not _wireguard_peer_matches_enrollment(peer, enrollment):
            raise ValueError("agent WireGuard peer does not match its enrollment")
        if peer.last_handshake_at is None:
            raise ValueError("agent WireGuard handshake has not been observed")
        return enrollment, peer

    def _record_private_network_failure(
        self,
        state: ComputeAgentTokenState,
        *,
        peer_id: str,
        generation: int,
        detail: str,
    ) -> None:
        with self.services.context.database.session() as session:
            enrollments = ComputeMachineEnrollmentRepository(session)
            enrollment = enrollments.by_machine(
                state.workspace_id,
                state.machine_id,
                pool=state.pool,
                for_update=True,
            )
            if (
                enrollment is None
                or enrollment.network_peer_id != peer_id
                or enrollment.network_generation != generation
            ):
                return
            enrollments.save(
                enrollment.model_copy(
                    update={
                        "network_phase": PrivateNetworkEnrollmentPhase.Failed,
                        "network_verified_at": None,
                        "network_failure_detail": detail,
                        "updated_at": utc_now(),
                    }
                )
            )

    def _remove_enrollment_private_network_identity(
        self,
        enrollment: ComputeMachineEnrollmentRecord,
    ) -> None:
        now = utc_now()
        with self.services.context.database.session() as session:
            peers = WireGuardPeerRepository(session)
            peer = peers.by_enrollment(enrollment.id, for_update=True)
            if peer is None or peer.status is WireGuardPeerStatus.Revoked:
                return
            peers.save(
                peer.model_copy(
                    update={
                        "status": WireGuardPeerStatus.Revoked,
                        "revoked_at": now,
                        "updated_at": now,
                    }
                )
            )


def _wireguard_peer_matches_enrollment(
    peer: WireGuardPeer,
    enrollment: ComputeMachineEnrollmentRecord,
) -> bool:
    return (
        peer.status is WireGuardPeerStatus.Active
        and peer.id == enrollment.network_peer_id
        and peer.public_key == enrollment.network_public_key
        and peer.address == enrollment.network_address
        and peer.generation == enrollment.network_generation
    )


def _join_token_state(
    credential: ComputeJoinCredentialRecord | None,
) -> ComputeJoinTokenState | None:
    if credential is None:
        return None
    return ComputeJoinTokenState(
        token_hash=credential.token_hash,
        owner_user_id=credential.user_id,
        workspace_id=credential.workspace_id,
        capacity_owner_id=credential.capacity_owner_id,
        pool=credential.pool,
        machine_id=credential.machine_id,
        credential_id=credential.id,
        created_by_token_id=credential.created_by_token_id or "workspace",
        max_uses=credential.max_uses,
        use_count=credential.use_count,
        created_at=credential.created_at,
        expires_at=credential.expires_at,
        revoked=credential.status is not ComputeCredentialStatus.Active,
    )


def _agent_state_from_enrollment(
    enrollment: ComputeMachineEnrollmentRecord | None,
) -> ComputeAgentTokenState | None:
    if enrollment is None:
        return None
    return ComputeAgentTokenState(
        token_hash=enrollment.credential_hash,
        owner_user_id=enrollment.user_id,
        workspace_id=enrollment.workspace_id,
        capacity_owner_id=enrollment.capacity_owner_id,
        pool=enrollment.pool,
        machine_id=enrollment.machine_id,
        credential_id=enrollment.id,
        credential_generation=enrollment.credential_generation,
        machine_fingerprint=enrollment.machine_fingerprint_hash,
        hostname=enrollment.hostname,
        os=enrollment.os,
        arch=enrollment.arch,
        cpu_count=enrollment.cpu_count,
        cpu_millicores=enrollment.cpu_millicores,
        memory_mb=enrollment.memory_mb,
        gpus=enrollment.gpus,
        gpu_ids=enrollment.gpu_ids,
        gpu_count=enrollment.gpu_count,
        executor=enrollment.executor,
        preflight_passed=enrollment.preflight_passed,
        heartbeat_confirmed=enrollment.heartbeat_confirmed,
        schedulable=(
            enrollment.schedulable and enrollment.status is ComputeMachineEnrollmentStatus.Active
        ),
        capacity_state=enrollment.capacity_state,
        capacity_reason=enrollment.capacity_reason,
        capacity_observed_at=enrollment.capacity_observed_at,
        capacity_notice_at=enrollment.capacity_notice_at,
        preflight=enrollment.preflight,
        agent_version=enrollment.agent_version,
        created_at=enrollment.created_at,
        last_join_at=enrollment.last_join_at,
        last_heartbeat_at=enrollment.last_heartbeat_at,
        last_disconnect_at=enrollment.last_disconnect_at,
    )


def _agent_readiness_phase(state: ComputeAgentTokenState) -> MachineReadinessPhase:
    if not state.preflight_passed:
        return MachineReadinessPhase.Blocked
    if not state.heartbeat_confirmed or state.last_heartbeat_at is None:
        return MachineReadinessPhase.Joining
    if state.last_disconnect_at is not None and state.last_disconnect_at >= state.last_heartbeat_at:
        return MachineReadinessPhase.Offline
    return MachineReadinessPhase.Ready


def _save_machine_enrollment(
    repository: ComputeMachineEnrollmentRepository,
    state: ComputeAgentTokenState,
    *,
    fingerprint_hash: str,
    join_credential_id: str,
    readiness_phase: MachineReadinessPhase,
    existing: ComputeMachineEnrollmentRecord | None,
) -> ComputeMachineEnrollmentRecord:
    enrollment = _machine_enrollment_snapshot(
        state,
        fingerprint_hash=fingerprint_hash,
        join_credential_id=join_credential_id,
        readiness_phase=readiness_phase,
    )
    if existing is not None:
        return repository.save(enrollment.update_record(existing, updated_at=utc_now()))
    return repository.create(enrollment)


def _machine_enrollment_snapshot(
    state: ComputeAgentTokenState,
    *,
    fingerprint_hash: str,
    join_credential_id: str | None,
    readiness_phase: MachineReadinessPhase,
) -> ComputeMachineEnrollmentCreate:
    return ComputeMachineEnrollmentCreate(
        user_id=state.owner_user_id,
        workspace_id=state.workspace_id,
        capacity_owner_id=state.capacity_owner_id,
        pool=state.pool,
        machine_id=state.machine_id,
        machine_fingerprint_hash=fingerprint_hash,
        join_credential_id=join_credential_id,
        credential_hash=state.token_hash,
        credential_generation=state.credential_generation,
        status=ComputeMachineEnrollmentStatus.Active,
        preflight_passed=state.preflight_passed,
        heartbeat_confirmed=state.heartbeat_confirmed,
        schedulable=state.schedulable,
        capacity_state=state.capacity_state,
        capacity_reason=state.capacity_reason,
        capacity_observed_at=state.capacity_observed_at,
        capacity_notice_at=state.capacity_notice_at,
        readiness_phase=readiness_phase,
        hostname=state.hostname,
        os=state.os,
        arch=state.arch,
        cpu_count=state.cpu_count,
        cpu_millicores=state.cpu_millicores,
        memory_mb=state.memory_mb,
        gpus=state.gpus,
        gpu_ids=state.gpu_ids,
        gpu_count=state.gpu_count,
        executor=state.executor,
        preflight=state.preflight,
        agent_version=state.agent_version,
        last_join_at=state.last_join_at or utc_now(),
        last_heartbeat_at=state.last_heartbeat_at,
        last_disconnect_at=state.last_disconnect_at,
    )


__all__ = [
    "SELF_HOSTED_FLEET_POOL_NAME",
    "AgentCapacityInterruptionSink",
    "GatewayControlService",
]
