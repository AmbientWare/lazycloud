from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent.binary import AgentBinarySettings
from agent.service import AgentService
from anyio import from_thread
from compute.agent_control import AgentImageConfig, GatewayEndpointConfig
from compute.aws_connections import AwsAccountConnectionDirectory, AwsAccountConnectionService
from compute.policy import AwsDefaultCapacityBaseline, WorkspaceComputePolicyService
from compute.provider_launches import ProviderNodeLaunchService
from compute.request_placement import ComputeCapacityPlacementService
from compute.service import ComputeService
from compute.state import ComputeAgentTokenState, RedisComputeStateRepository
from compute.telemetry import AGENT_INTAKE_PRESENCE_ROLE
from control.apps import (
    AppService,
    DatabaseAppExecutionAdmission,
    DatabaseAppImageAvailability,
)
from control.custom_domains import CustomDomainService
from control.deployment_cleanup import AppDeploymentLifecycleService
from control.deployment_registration import DeploymentRegistrationService
from control.deployment_resources import DeploymentResourceService
from control.deployments import CronJobService, DeploymentService
from control.routes import RouteService
from control.service import ControlPlaneService, WorkspaceBucketClient
from coordination.event_bus import RedisEventBus
from coordination.process_presence import RedisProcessPresence
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from database.context import ServiceContext
from database.records.apps import AutoscalingStubRecord, StubRecord
from execution.artifacts.service import ArtifactStorageService
from execution.collections.redis import RedisMapService, RedisSimpleQueueService
from execution.collections.service import CollectionService
from execution.containers.preemption import PreemptedContainerService
from execution.containers.runtime_state import RedisContainerRuntimeStateRepository
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from execution.containers.service import ContainerService
from execution.endpoints.dispatch import (
    AsyncEndpointInstanceDispatcher,
    AsyncEndpointResponseStream,
)
from execution.endpoints.service import (
    EndpointControlService,
    EndpointDispatchStateRepository,
    EndpointIngressDispatchSession,
)
from execution.functions.service import FunctionControlService
from execution.pods.service import PodControlService
from execution.secrets.crypto import WorkspaceSecretCipher
from execution.secrets.service import SecretService
from execution.shells.service import ShellControlService
from execution.signals.redis import RedisSignalRepository, RedisSignalService
from execution.task_progress import TaskProgressService
from execution.task_rerun import TaskRerunService
from execution.tasks import TaskService
from execution.volumes.control import VolumeControlService
from execution.volumes.records import VolumeService
from gateway.container_readiness import AsyncRedisContainerReadiness
from gateway.container_transport import HttpContainerServiceTransportFactory
from gateway.machine_lifecycle import MachineLifecycleService
from gateway.pod_proxy import (
    AsyncPodProxyHttpClient,
    AsyncRedisPodProxyConnectionRepository,
    PodProxySocketClient,
)
from gateway.pool_bootstrap import pool_bootstrap_provisioner
from gateway.provider_enrollment import ProviderNodeEnrollmentService
from gateway.route_prewarm import RoutePrewarmService
from gateway.service import GatewayControlService
from gateway.settings import GatewaySettings
from gateway.shell_proxy import connect_shell_backend
from identity.auth import AuthService, AuthTokenCache
from identity.invitations import WorkspaceInvitationService
from identity.sign_in import BillingProvisioner, SignInService
from identity.users import UserService
from images.control import ImageControlService
from images.publication import (
    ArchiveImageBuildPublicationPublisher,
    CacheImageBuildPublicationPublisher,
    CompositeImageBuildPublicationPublisher,
    ImageBuildArchiveObjectStore,
    ImageBuildPublicationPublisher,
)
from images.service import ImageBuildService
from images.settings import (
    ImageBuildContainerSettings,
    ImageBuildExecutionSettings,
    ImageBuildRegistrySettings,
)
from images.submission import ImageBuildSubmissionService
from networking.async_http import AsyncBackendHttpClient
from networking.dialer import (
    BackendRouteDialer,
    BackendRouteDialerConfig,
)
from networking.settings import BackendRouteSettings
from observability.events import EventService
from observability.metrics import MetricsService
from observability.settings import (
    VolumeMeteringSettings,
    WorkspaceChangeStreamSettings,
)
from observability.stream_state import AsyncTaskChangeReader, RedisEventStreamRepository
from observability.usage import UsageService, WorkerEventService
from observability.workspace_changes import (
    AsyncWorkspaceChangeService,
    WorkspaceChangeRepository,
    WorkspaceChangeService,
)
from operations.app_lifecycle import ProductionAppExecutionLifecycleEffects
from operations.container_shutdown import (
    ContainerShutdownService,
    DatabaseContainerStorageRelease,
    DatabaseDurableWorkerAbsence,
)
from operations.management import ManagementService
from provider_aws import AwsProvider, AwsProviderSettings
from provider_clients import (
    AwsProviderNodeIdentityAdapter,
    ProductionRegistryCredentialResolver,
    configured_aws_compute_catalog,
    workspace_compute_provider_resolver,
)
from provider_clients.provider_nodes import configured_provider_node_identity_registry
from provider_clients.settings import (
    AwsAccountConnectionSettings,
    AwsCapacityReconciliationSettings,
    AwsCapacitySettings,
    PlatformCapacitySettings,
)
from provider_clients.workspace_compute import configured_platform_compute_providers
from provider_cloudflare import CloudflareSettings
from provider_github import GitHubAppSettings
from provider_resend import ResendSettings
from provider_stripe import StripeSettings
from scheduler.autoscaler_operations import AutoscalerOperationsService
from scheduler.autoscaler_states import AutoscalerStateService
from scheduler.autoscaling import (
    AutoscalingDriver,
    EndpointAutoscaler,
    EndpointAutoscalingDispatchObservation,
    FunctionAutoscaler,
    PodAutoscaler,
)
from scheduler.capacity_reservations import (
    CapacityReservationService,
    RedisCapacityReservationRepository,
)
from scheduler.compute_hooks import SchedulerComputeHooks
from scheduler.compute_placement import SchedulerComputePlacement
from scheduler.containers import (
    CONTAINER_DISPATCH_WAKE_SCOPE,
    SchedulerContainerRequestService,
)
from scheduler.preemption import (
    CapacityInterruption,
    SchedulerCapacityInterruption,
    SchedulerCapacityInterruptionService,
    SchedulerGpuBackfillPreemptionService,
    SchedulerWorkerMaintenanceService,
    SchedulerWorkerPreemptionService,
)
from scheduler.routes import SchedulerBackendRouteResolver
from scheduler.services import SchedulerWorkloadDirectory
from scheduler.state import (
    AsyncRedisSchedulerContainerReader,
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerNetworkIpRepository,
    RedisWorkerPoolStateRepository,
)
from scheduler.worker_rollout import WorkerWorkloadDrainService
from scheduler.workers import SchedulerWorkerAdminService
from scheduler.workspace_owners import DatabaseWorkspaceOwners
from shared.container_requests import StopContainerReason
from shared.http.endpoints import (
    EndpointForwardRequest,
    EndpointForwardResponse,
    StartEndpointServeRequest,
    StartEndpointServeResponse,
)
from shared.http.functions import (
    FunctionClaimRequest,
    FunctionClaimResponse,
    FunctionInvokeBody,
    FunctionInvokeResponse,
    FunctionMonitorRequest,
    FunctionMonitorResponse,
    FunctionRetireRequest,
    FunctionRetireResponse,
    FunctionSetResultBody,
    FunctionSetResultResponse,
)
from shared.identity import WorkspaceRecord
from shared.image_building.credentials import parse_ecr_registry
from shared.payments import PaymentProvider
from shared.scheduling import SchedulerWorkerRequest
from shared.workspace_storage import WorkspaceStorageIssuer
from storage.image_archive import IMAGE_ARCHIVE_EXTENSION, ImageArchiveSettings
from storage.retention_settings import RetentionSettings
from storage.service import CacheStorage, ObjectByteClient, ObjectStorage
from storage.volume_filesystem import (
    VolumeFilesystem,
    WorkspaceVolumeFilesystem,
    WorkspaceVolumeObjectClient,
    workspace_volume_store_resolver,
)
from storage.volume_metering import PersistentVolumeMeteringService
from storage.workspace_storage_issuers import (
    WorkspaceStorageRouter,
    external_workspace_storage_settings,
)
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings
from worker.container_client.scheduler import (
    SchedulerContainerClientFactory,
    SchedulerContainerServiceStopper,
)
from worker.image_lifecycle import ImageRegistryStore
from worker.origin_access import ImageRegistryCredentials
from worker.settings import ContainerServiceSettings
from worker_repository.checkpoint_records import CheckpointService
from worker_repository.credentials import WorkerCredentialService
from worker_repository.image_build_dispatch import DurableImageBuildDispatch
from worker_repository.origin_credentials import (
    CacheOriginCredentialConfig,
    PresignedPutClient,
    WorkerCacheOriginCredentialService,
)
from worker_repository.source_cache import WorkerSourceCacheService

from api.server.async_io import ApiAsyncIo
from api.server.provider_compute import (
    BoundedProviderNodeIdentityHttpClient,
    RedisProviderNodeIdentityReplayGuard,
    aws_account_connection_composition_from_settings,
)
from api.server.worker_repository_service import (
    WorkerRepositoryDependencies,
    WorkerRepositoryService,
)
from api.server.workspace_storage_composition import managed_workspace_storage_issuer
from api.settings import (
    AgentDisconnectReconciliationSettings,
    AgentRouteReconciliationSettings,
    PublicIngressSettings,
    TcpIngressSettings,
)
from billing import BillingAccountService, DatabaseBillingAdmission
from database import AsyncDatabaseClient, DatabaseClient


@dataclass(frozen=True, slots=True)
class ApiContainerSchedulingFailureHandler:
    containers: ContainerSchedulingPersistenceService
    images: ImageBuildService

    def mark_scheduling_failed(
        self,
        request: SchedulerWorkerRequest,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> None:
        self.containers.mark_scheduling_failed(request, reason, now=now)
        self.images.fail_container_build(
            request.container_id, reason, workspace_id=request.workspace_id
        )


@dataclass(frozen=True, slots=True)
class SchedulerAgentCapacityInterruptionSink:
    interruptions: SchedulerCapacityInterruption

    def preempt_agent_capacity(self, state: ComputeAgentTokenState) -> None:
        observed_at = state.capacity_observed_at
        if observed_at is None:
            raise RuntimeError("agent capacity interruption has no authoritative observation time")
        self.interruptions.preempt_interruption(
            CapacityInterruption(
                enrollment_id=state.credential_id,
                credential_generation=state.credential_generation,
                workspace_id=state.workspace_id,
                pool=state.pool,
                machine_id=state.machine_id,
                state=state.capacity_state,
                reason=state.capacity_reason,
                observed_at=observed_at,
                notice_at=state.capacity_notice_at,
            )
        )


@runtime_checkable
class ApiOwnedResource(Protocol):
    def close(self) -> None: ...


class FunctionApiService(Protocol):
    def function_invoke(self, request: FunctionInvokeBody) -> FunctionInvokeResponse: ...

    def function_invoke_stream(
        self,
        initial: FunctionInvokeResponse,
        *,
        headless: bool = False,
        keepalive_interval_seconds: float = 5.0,
    ) -> AsyncIterator[FunctionInvokeResponse]: ...

    def unclaimed_task_counts(self, stub_ids: Sequence[str]) -> dict[str, int]: ...

    def start_function_container(self, stub_id: str) -> bool: ...

    def containers_holding_work(self, container_ids: Sequence[str]) -> set[str]: ...

    def fail_unclaimed_tasks(
        self,
        stub_id: str,
        *,
        error: str,
        limit: int = 100,
    ) -> int: ...

    def function_claim(self, request: FunctionClaimRequest) -> FunctionClaimResponse: ...

    def function_retire(
        self,
        request: FunctionRetireRequest,
        *,
        workspace_id: str,
    ) -> FunctionRetireResponse: ...

    def function_set_result(self, request: FunctionSetResultBody) -> FunctionSetResultResponse: ...

    def function_monitor(self, request: FunctionMonitorRequest) -> FunctionMonitorResponse: ...


class EndpointApiService(Protocol):
    def start_endpoint_serve(
        self,
        request: StartEndpointServeRequest,
    ) -> StartEndpointServeResponse: ...

    async def forward_endpoint_request(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse: ...

    async def forward_endpoint_health(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse: ...

    async def prepare_asgi_websocket(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointIngressDispatchSession: ...

    async def prepare_asgi_http(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointIngressDispatchSession: ...

    async def heartbeat_asgi_websocket(self, task_id: str) -> None: ...

    async def open_asgi_websocket_socket(
        self,
        session: EndpointIngressDispatchSession,
    ) -> socket.socket | None: ...

    async def open_asgi_http_stream(
        self,
        session: EndpointIngressDispatchSession,
        request: EndpointForwardRequest,
    ) -> AsyncEndpointResponseStream: ...

    async def finish_asgi_http(
        self,
        task_id: str,
        *,
        status_code: int | None = None,
        body_size_bytes: int = 0,
        cancelled: bool = False,
        error: str | None = None,
    ) -> None: ...

    async def finish_asgi_websocket(
        self,
        task_id: str,
        *,
        cancelled: bool = False,
        error: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ApiSchedulerWorkloadControl:
    control_plane: ControlPlaneService

    def list_stubs(self, *, workspace: str | None = None) -> list[StubRecord]:
        return self.control_plane.list_stubs(workspace=workspace)

    def list_autoscaling_stubs(
        self,
        stub_ids: Sequence[str] | None = None,
    ) -> list[AutoscalingStubRecord]:
        return self.control_plane.list_autoscaling_stubs(stub_ids)

    def get_stub(
        self,
        stub_id_or_name: str,
        *,
        workspace: str | None = None,
    ) -> StubRecord:
        return self.control_plane.get_stub(stub_id_or_name, workspace=workspace)

    def get_workspace(self, workspace: str = "default") -> WorkspaceRecord:
        return self.control_plane.get_workspace(workspace)

    def set_autoscaling_enabled(
        self,
        stub_id_or_name: str,
        *,
        workspace: str,
        enabled: bool,
    ) -> StubRecord:
        return self.control_plane.update_stub_config(
            stub_id_or_name,
            workspace=workspace,
            fields={"metadata.autoscaling_enabled": enabled},
        ).stub


@dataclass(frozen=True, slots=True)
class ApiEndpointDispatchAutoscalingReader:
    repository: EndpointDispatchStateRepository

    def active_counts_by_stub(self, stub_ids: Sequence[str]) -> dict[str, int]:
        return self.repository.active_counts_by_stub(stub_ids)

    def observations_by_stub(
        self,
        stub_ids: Sequence[str],
        *,
        finished_since: datetime,
    ) -> dict[str, list[EndpointAutoscalingDispatchObservation]]:
        return {
            stub_id: [
                EndpointAutoscalingDispatchObservation(
                    container_id=record.container_id,
                    active=record.active,
                    finished_at=record.finished_at,
                )
                for record in records
            ]
            for stub_id, records in self.repository.observations_by_stub(
                stub_ids,
                finished_since=finished_since,
            ).items()
        }


@runtime_checkable
class _RuntimeWorkspaceBucketClient(Protocol):
    def create_bucket(self, bucket: str | None = None) -> None: ...

    def validate_bucket_access(self, bucket: str | None = None) -> None: ...

    def configure_workspace_bucket(self, bucket: str, *, public_origin: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ApiServiceCore:
    context: ServiceContext
    auth: AuthService
    users: UserService
    invitations: WorkspaceInvitationService
    sign_in: SignInService
    auth_token_cache: AuthTokenCache
    tcp_ingress_settings: TcpIngressSettings
    agent_route_reconciliation_settings: AgentRouteReconciliationSettings
    agent_disconnect_reconciliation_settings: AgentDisconnectReconciliationSettings
    gateway_settings: GatewaySettings
    stripe_settings: StripeSettings
    resend_settings: ResendSettings
    payment_provider: Callable[[], PaymentProvider]
    workspace_change_stream_settings: WorkspaceChangeStreamSettings
    agent_binary_settings: AgentBinarySettings
    aws_account_connection_settings: AwsAccountConnectionSettings
    aws_capacity_settings: AwsCapacitySettings
    platform_capacity_settings: PlatformCapacitySettings
    aws_capacity_reconciliation_settings: AwsCapacityReconciliationSettings
    backend_route_settings: BackendRouteSettings
    object_store_settings: S3ObjectStoreSettings
    workspace_storage_issuer: WorkspaceStorageIssuer
    image_archive_settings: ImageArchiveSettings
    image_archive_presigner: PresignedPutClient
    image_build_registry_settings: ImageBuildRegistrySettings
    container_service_settings: ContainerServiceSettings
    events: EventService
    workspace_changes: WorkspaceChangeService
    tasks: TaskService
    control_plane_service: ControlPlaneService
    aws_account_connection_directory: AwsAccountConnectionDirectory
    workspace_compute_policy_service: WorkspaceComputePolicyService
    apps: AppService
    deployments: DeploymentService
    deployment_resources: DeploymentResourceService
    custom_domains: CustomDomainService
    cron_jobs: CronJobService
    collections: CollectionService
    secrets: SecretService
    volumes: VolumeService
    compute: ComputeService
    provider_node_launches: ProviderNodeLaunchService
    containers: ContainerService
    container_shutdowns: ContainerShutdownService
    scheduler_workers: RedisSchedulerWorkerRepository
    scheduler_containers: RedisSchedulerContainerRepository
    scheduler_container_requests: SchedulerContainerRequestService
    scheduler_pool_states: RedisWorkerPoolStateRepository
    capacity_reservation_repository: RedisCapacityReservationRepository
    scheduler_workloads: SchedulerWorkloadDirectory
    images: ImageBuildService
    agents: AgentService
    object_storage: ObjectStorage
    cache_storage: CacheStorage
    metrics: MetricsService
    worker_events: WorkerEventService
    usage: UsageService
    routes: RouteService
    checkpoints: CheckpointService
    autoscaler_states: AutoscalerStateService
    volume_metering: PersistentVolumeMeteringService
    volume_filesystem: VolumeFilesystem
    payment_admission: DatabaseBillingAdmission
    redis_client: RedisClient
    binary_redis_client: RedisClient
    async_io: ApiAsyncIo | None
    aws_connections: AwsAccountConnectionService | None
    owns_redis_client: bool
    owns_binary_redis_client: bool
    owned_resources: tuple[ApiOwnedResource, ...]
    client_release_version: str | None

    @property
    def database(self) -> DatabaseClient:
        return self.context.database

    @property
    def root(self) -> Path:
        return self.context.paths.root

    def redis(self) -> RedisClient:
        return self.redis_client

    def binary_redis(self) -> RedisClient:
        return self.binary_redis_client

    def require_async_io(self) -> ApiAsyncIo:
        if self.async_io is None:
            raise RuntimeError("API asynchronous I/O resources were not composed")
        return self.async_io


@dataclass(frozen=True, slots=True)
class ApiServices(ApiServiceCore):
    signal_service: RedisSignalService
    map_service: RedisMapService
    simple_queue_service: RedisSimpleQueueService
    artifact_service: ArtifactStorageService
    endpoint_service: EndpointApiService
    function_service: FunctionApiService
    gateway_service: GatewayControlService
    image_service: ImageControlService
    pod_service: PodControlService
    shell_service: ShellControlService
    volume_service: VolumeControlService
    worker_repository_service: WorkerRepositoryService
    provider_node_enrollment_service: ProviderNodeEnrollmentService | None
    machine_lifecycle_service: MachineLifecycleService
    backend_route_resolver: SchedulerBackendRouteResolver
    backend_route_dialer_config: BackendRouteDialerConfig
    task_rerun_service: TaskRerunService
    autoscaler_operations_service: AutoscalerOperationsService
    scheduler_worker_admin_service: SchedulerWorkerAdminService

    @classmethod
    def create(
        cls,
        database: DatabaseClient,
        *,
        tcp_ingress_settings: TcpIngressSettings | None = None,
        agent_route_reconciliation_settings: AgentRouteReconciliationSettings | None = None,
        agent_disconnect_reconciliation_settings: (
            AgentDisconnectReconciliationSettings | None
        ) = None,
        gateway_settings: GatewaySettings | None = None,
        stripe_settings: StripeSettings | None = None,
        resend_settings: ResendSettings | None = None,
        workspace_change_stream_settings: WorkspaceChangeStreamSettings | None = None,
        agent_binary_settings: AgentBinarySettings | None = None,
        aws_account_connection_settings: AwsAccountConnectionSettings | None = None,
        aws_capacity_settings: AwsCapacitySettings | None = None,
        platform_capacity_settings: PlatformCapacitySettings | None = None,
        aws_capacity_reconciliation_settings: AwsCapacityReconciliationSettings | None = None,
        backend_route_settings: BackendRouteSettings | None = None,
        object_store_settings: S3ObjectStoreSettings | None = None,
        workspace_storage_issuer: WorkspaceStorageIssuer | None = None,
        object_storage: ObjectStorage | None = None,
        object_store_client: ObjectByteClient | None = None,
        workspace_storage_client: WorkspaceBucketClient | None = None,
        image_build_execution_settings: ImageBuildExecutionSettings | None = None,
        image_build_registry_settings: ImageBuildRegistrySettings | None = None,
        image_build_container_settings: ImageBuildContainerSettings | None = None,
        container_service_settings: ContainerServiceSettings | None = None,
        retention_settings: RetentionSettings | None = None,
        volume_metering_settings: VolumeMeteringSettings | None = None,
        volume_metering: PersistentVolumeMeteringService | None = None,
        volume_filesystem: VolumeFilesystem | None = None,
        root: Path | None = None,
        create_schema: bool = True,
        redis_client: RedisClient,
        binary_redis_client: RedisClient,
        async_io: ApiAsyncIo | None = None,
        owns_redis_client: bool = False,
        owns_binary_redis_client: bool = False,
        signal_service: RedisSignalService | None = None,
        map_service: RedisMapService | None = None,
        simple_queue_service: RedisSimpleQueueService | None = None,
        artifact_service: ArtifactStorageService | None = None,
        endpoint_service: EndpointApiService | None = None,
        function_service: FunctionApiService | None = None,
        gateway_service: GatewayControlService | None = None,
        image_service: ImageControlService | None = None,
        pod_service: PodControlService | None = None,
        shell_service: ShellControlService | None = None,
        volume_service: VolumeControlService | None = None,
        worker_repository_service: WorkerRepositoryService | None = None,
        owned_resources: tuple[ApiOwnedResource, ...] = (),
        client_release_version: str | None = None,
    ) -> ApiServices:
        context = ServiceContext.create(database, root=root, create_schema=create_schema)
        auth_token_cache = AuthTokenCache()
        auth = AuthService(context, token_cache=auth_token_cache)
        users = UserService(context)
        tcp_ingress_config = tcp_ingress_settings or TcpIngressSettings()
        agent_route_reconciliation_config = (
            agent_route_reconciliation_settings or AgentRouteReconciliationSettings()
        )
        agent_disconnect_reconciliation_config = (
            agent_disconnect_reconciliation_settings or AgentDisconnectReconciliationSettings()
        )
        gateway_config = gateway_settings or GatewaySettings()
        stripe_config = stripe_settings or StripeSettings()
        # Read here only so the webhook endpoint can check the signature on a
        # delivery report. Sending belongs to the scheduler.
        resend_config = resend_settings or ResendSettings()
        workspace_change_stream_config = (
            workspace_change_stream_settings or WorkspaceChangeStreamSettings()
        )
        agent_artifact_config = agent_binary_settings or AgentBinarySettings()
        aws_account_connection_config = (
            aws_account_connection_settings or AwsAccountConnectionSettings()
        )
        aws_capacity_config = aws_capacity_settings or AwsCapacitySettings()
        platform_capacity_config = platform_capacity_settings or PlatformCapacitySettings()
        aws_capacity_reconciliation_config = (
            aws_capacity_reconciliation_settings or AwsCapacityReconciliationSettings()
        )
        resolved_backend_route_settings = backend_route_settings or BackendRouteSettings()
        object_store_config = object_store_settings or S3ObjectStoreSettings()
        image_archive_config = ImageArchiveSettings(bucket=object_store_config.bucket)
        image_build_execution_config = (
            image_build_execution_settings or ImageBuildExecutionSettings()
        )
        image_build_registry_config = image_build_registry_settings or ImageBuildRegistrySettings()
        image_build_container_config = (
            image_build_container_settings or ImageBuildContainerSettings()
        )
        container_service_config = container_service_settings or ContainerServiceSettings()
        retention_config = retention_settings or RetentionSettings()
        volume_metering_config = volume_metering_settings or VolumeMeteringSettings()
        redis = redis_client
        stream_events = RedisEventStreamRepository(redis)
        async_database = async_io.database if async_io is not None else None
        async_workspace_changes = (
            AsyncWorkspaceChangeService(
                async_io.redis,
                max_length=workspace_change_stream_config.max_length,
            )
            if async_io is not None
            else None
        )
        events = EventService(
            context,
            stream_events=stream_events,
            async_database=async_database,
        )
        workspace_changes = WorkspaceChangeService(
            WorkspaceChangeRepository(
                redis,
                max_length=workspace_change_stream_config.max_length,
            )
        )
        container_repository = RedisSchedulerContainerRepository(redis)
        tasks = TaskService(
            context,
            events,
            log_streams=stream_events,
            progress=TaskProgressService(context, container_repository),
            workspace_changes=workspace_changes,
            async_database=async_database,
            async_workspace_changes=async_workspace_changes,
        )
        secrets = SecretService(context, events, workspace_changes=workspace_changes)
        # Same decision as the provider resolver below: a deployment without
        # connected AWS advertises no AWS catalog, and building one would demand
        # the capacity and agent-artifact configuration it has no reason to hold.
        aws_compute_catalog = (
            configured_aws_compute_catalog(
                aws_capacity_config,
                agent_artifact_config,
            )
            if aws_account_connection_config.configured
            else ()
        )
        compute_policies = WorkspaceComputePolicyService(
            context,
            available_catalog=aws_compute_catalog,
        )
        routes = RouteService(context)
        cache_storage = CacheStorage(context)
        if object_storage is not None and object_store_client is not None:
            raise ValueError("object_storage and object_store_client are mutually exclusive")
        owned_runtime_resources = list(owned_resources)
        if object_storage is not None:
            object_storage_service = object_storage
            object_storage_service.allowed_buckets = frozenset(
                {*object_storage_service.allowed_buckets, image_archive_config.bucket}
            )
        elif object_store_client is not None:
            object_storage_service = ObjectStorage(
                context,
                object_client=object_store_client,
                default_bucket=object_store_config.bucket,
                allowed_buckets=(image_archive_config.bucket,),
            )
        else:
            object_storage_service = ObjectStorage.from_settings(
                context,
                object_store_config,
                allowed_buckets=(image_archive_config.bucket,),
            )
            if isinstance(object_storage_service.object_client, ApiOwnedResource):
                owned_runtime_resources.append(object_storage_service.object_client)
        if not isinstance(object_storage_service.object_client, PresignedPutClient):
            raise RuntimeError("the primary object store must support signed archive uploads")
        resolved_image_archive_presigner = object_storage_service.object_client
        control_plane = ControlPlaneService(
            context,
            public_http_origin=gateway_config.public_http_url,
            workspace_storage_client=(
                workspace_storage_client
                or _workspace_bucket_client(object_storage_service.object_client)
            ),
            workspace_storage_client_factory=lambda storage: S3ObjectStoreClient.from_settings(
                external_workspace_storage_settings(storage)
            ),
            workspace_changes=workspace_changes,
        )
        workspace_storage_issuer = workspace_storage_issuer or WorkspaceStorageRouter(
            managed=managed_workspace_storage_issuer(object_store_config)
        )
        payment_provider = stripe_config.provider_factory()
        # No mailer here. Inviting queues a message and returns. The scheduler's
        # drain holds the email credential and talks to the provider.
        invitations = WorkspaceInvitationService(
            context,
            invitations_url=f"{gateway_config.public_http_url.rstrip('/')}/invitations",
        )
        # Neither adapter is constructed here — both are callables that read their
        # credential when first asked — so a deployment that has not configured a
        # GitHub App or a payment credential still starts and fails at the sign-in
        # route naming what is missing, instead of refusing to serve anything at all.
        sign_in = SignInService(
            context=context,
            redis=redis,
            provider_factory=GitHubAppSettings().provider,
            provision_default_workspace=control_plane.ensure_default_workspace,
            provision_billing_account=_billing_account_provisioner(context, payment_provider),
        )
        if volume_filesystem is None:
            if not isinstance(object_storage_service.object_client, WorkspaceVolumeObjectClient):
                raise RuntimeError("workspace volumes require the configured object client")
            resolved_volume_filesystem = WorkspaceVolumeFilesystem(
                resolve_store=workspace_volume_store_resolver(
                    context.database,
                    object_store=object_storage_service.object_client,
                )
            )
            owned_runtime_resources.append(resolved_volume_filesystem)
        else:
            resolved_volume_filesystem = volume_filesystem
        worker_repository = RedisSchedulerWorkerRepository(redis)
        pool_state_repository = RedisWorkerPoolStateRepository(redis)
        capacity_reservation_repository = RedisCapacityReservationRepository(redis)
        usage = UsageService(
            context,
            workspace_changes=workspace_changes,
            async_database=async_database,
            async_workspace_changes=async_workspace_changes,
        )
        volume_metering_service = volume_metering or (
            PersistentVolumeMeteringService.from_settings(
                context,
                filesystem=resolved_volume_filesystem,
                interval_seconds=volume_metering_config.interval_seconds,
            )
        )
        aws_connection_directory = AwsAccountConnectionDirectory(context)

        def platform_capacity_workspace(workspace: str) -> str:
            with context.database.session() as session:
                return context.workspace(session, workspace).id

        def provider_node_cipher(workspace_id: str) -> WorkspaceSecretCipher:
            with context.database.session() as session:
                workspace = context.workspace(session, workspace_id)
            return WorkspaceSecretCipher.from_workspace(workspace)

        provider_node_launches = ProviderNodeLaunchService(
            database=context.database,
            cipher_for_workspace=provider_node_cipher,
        )
        # Connected AWS is an optional deployment shape. When it is unconfigured there is
        # no connection to resolve, and building the resolver would demand the remote
        # network configuration a local stack has no reason to hold. A half-configured
        # deployment never reaches here: the settings validator rejects it.
        provider_resolver = (
            workspace_compute_provider_resolver(
                aws_capacity_config,
                agent_artifact_config,
                connections=aws_connection_directory.list_for_workspace,
                platform_connections=aws_connection_directory.list_platform,
                capacity_workspace=aws_connection_directory.capacity_workspace,
                platform_providers=configured_platform_compute_providers(
                    platform_capacity_config,
                    launch_credentials=provider_node_launches,
                    capacity_workspace=platform_capacity_workspace,
                    redis=redis,
                ),
                gateway_origin=gateway_config.public_http_url,
                presigned_origin=object_store_config.endpoint_url,
                backend_route=resolved_backend_route_settings,
            )
            if aws_account_connection_config.configured or platform_capacity_config.configured
            else None
        )

        pool_bootstrap = None
        if provider_resolver is not None:
            agent_version, agent_sha256 = agent_artifact_config.require_amd64()
            pool_bootstrap = pool_bootstrap_provisioner(
                control_plane_url=gateway_config.public_http_url,
                agent_version=agent_version,
                agent_sha256=agent_sha256,
                agent_binary_url=aws_capacity_config.agent_binary_url,
            )

        scheduler_hooks = SchedulerComputeHooks(
            RedisComputeStateRepository(redis),
            worker_repository,
            agent_intake=RedisProcessPresence(redis, AGENT_INTAKE_PRESENCE_ROLE),
        )
        compute = ComputeService(
            context,
            provider_resolver=provider_resolver,
            pool_bootstrap_factory=pool_bootstrap,
            scheduler_hooks=scheduler_hooks,
            workspace_changes=workspace_changes,
            capacity_owner_mutations=capacity_reservation_repository,
        )
        compute_policies.aws_default_capacity = AwsDefaultCapacityBaseline(compute)
        compute_policies.worker_state = scheduler_hooks
        aws_composition = aws_account_connection_composition_from_settings(
            context=context,
            pool_drainer=compute,
            connection_settings=aws_account_connection_config,
            capacity_settings=aws_capacity_config,
            gateway_origin=gateway_config.public_http_url,
            backend_route=resolved_backend_route_settings,
            workspace_changes=workspace_changes,
            capacity_baseline=compute_policies,
            admission=DatabaseBillingAdmission(),
        )
        placement_resources = (
            aws_composition.deployment_bucket_access if aws_composition is not None else None
        )
        container_runtime_state = RedisContainerRuntimeStateRepository(redis)
        scheduling_persistence = ContainerSchedulingPersistenceService(
            context,
            events,
            workspace_changes,
            runtime_state=container_runtime_state,
        )
        container_scheduler = SchedulerContainerRequestService(
            worker_repository,
            container_repository,
            placement=SchedulerComputePlacement(
                ComputeCapacityPlacementService(
                    context,
                    compute_policies,
                    compute,
                )
            ),
            failure_handler=scheduling_persistence,
            assignments=scheduling_persistence,
            usage=usage,
            dispatch_wake=RedisWakeSignal(redis, CONTAINER_DISPATCH_WAKE_SCOPE),
            lifecycle_events=stream_events,
            workspace_owners=DatabaseWorkspaceOwners(context),
        )
        payment_admission = DatabaseBillingAdmission()
        container_shutdowns = ContainerShutdownService(
            container_repository,
            RedisEventBus(redis),
            redis,
            storage_release=DatabaseContainerStorageRelease(context),
            durable_worker_absence=DatabaseDurableWorkerAbsence(context, worker_repository),
        )
        containers = ContainerService(
            context,
            events,
            tasks,
            DatabaseAppExecutionAdmission(),
            payment_admission,
            scheduler=container_scheduler,
            scheduler_cancellation=container_scheduler,
            event_bus=RedisEventBus(redis),
            workspace_changes=workspace_changes,
            runtime_state=container_runtime_state,
            container_shutdowns=container_shutdowns,
            workers=worker_repository,
        )
        container_scheduler.backfill_preemption = SchedulerGpuBackfillPreemptionService(
            worker_repository, container_repository, containers
        )
        deployment_lifecycle = AppDeploymentLifecycleService(
            context,
            workspace_changes=workspace_changes,
            placement_resources=placement_resources,
        )
        apps = AppService(
            context,
            deployment_lifecycle,
            ProductionAppExecutionLifecycleEffects(
                context,
                containers,
                tasks,
                redis,
                container_shutdowns,
            ),
            DatabaseAppImageAvailability(),
            workspace_changes=workspace_changes,
        )
        cron_jobs = CronJobService(
            context,
            workspace_changes=workspace_changes,
        )
        deployments = DeploymentService(
            context,
            events,
            compute_policies,
            DeploymentRegistrationService(apps, control_plane),
            cron_jobs,
            payment_admission,
            workspace_changes=workspace_changes,
            placement_resources=placement_resources,
        )
        if not isinstance(object_storage_service.object_client, ImageBuildArchiveObjectStore):
            raise RuntimeError("the primary object store must verify immutable archive candidates")
        resolved_image_archive_store = object_storage_service.object_client
        publication_publisher = _image_build_publication_publisher(
            cache_storage,
            image_archive_config,
            image_build_execution_config,
            image_build_registry_config,
            context=context,
            archive_store=resolved_image_archive_store,
        )
        images = ImageBuildService(
            context,
            ImageBuildSubmissionService(
                context.database,
                DurableImageBuildDispatch(
                    context.database, container_scheduler, containers, image_build_container_config
                ),
            ),
            events,
            publication_publisher,
            archive_settings=image_archive_config,
            archive_store=resolved_image_archive_store,
        )
        container_scheduler.failure_handler = ApiContainerSchedulingFailureHandler(
            scheduling_persistence, images
        )
        deployment_resources = DeploymentResourceService(context)
        custom_domains = CustomDomainService(
            context=context,
            provider_factory=CloudflareSettings().provider,
            platform_base_domain=gateway_config.public_base_domain,
            admission=DatabaseBillingAdmission(),
        )
        collections = CollectionService(context)
        volumes = VolumeService(context, workspace_changes=workspace_changes)
        scheduler_workloads = ApiSchedulerWorkloadControl(control_plane)
        agents = AgentService(context, workspace_changes=workspace_changes)
        metrics = MetricsService()
        worker_events = WorkerEventService(context)
        checkpoints = CheckpointService(
            context,
            retention_seconds=retention_config.checkpoint_seconds,
        )
        autoscaler_states = AutoscalerStateService(context)
        core = ApiServiceCore(
            client_release_version=client_release_version,
            context=context,
            auth=auth,
            users=users,
            invitations=invitations,
            sign_in=sign_in,
            auth_token_cache=auth_token_cache,
            tcp_ingress_settings=tcp_ingress_config,
            agent_route_reconciliation_settings=agent_route_reconciliation_config,
            agent_disconnect_reconciliation_settings=agent_disconnect_reconciliation_config,
            gateway_settings=gateway_config,
            stripe_settings=stripe_config,
            resend_settings=resend_config,
            payment_provider=payment_provider,
            workspace_change_stream_settings=workspace_change_stream_config,
            agent_binary_settings=agent_artifact_config,
            aws_account_connection_settings=aws_account_connection_config,
            aws_capacity_settings=aws_capacity_config,
            platform_capacity_settings=platform_capacity_config,
            aws_capacity_reconciliation_settings=aws_capacity_reconciliation_config,
            backend_route_settings=resolved_backend_route_settings,
            object_store_settings=object_store_config,
            workspace_storage_issuer=workspace_storage_issuer,
            image_archive_settings=image_archive_config,
            image_archive_presigner=resolved_image_archive_presigner,
            image_build_registry_settings=image_build_registry_config,
            container_service_settings=container_service_config,
            events=events,
            workspace_changes=workspace_changes,
            tasks=tasks,
            control_plane_service=control_plane,
            aws_account_connection_directory=aws_connection_directory,
            workspace_compute_policy_service=compute_policies,
            apps=apps,
            deployments=deployments,
            deployment_resources=deployment_resources,
            custom_domains=custom_domains,
            cron_jobs=cron_jobs,
            collections=collections,
            secrets=secrets,
            volumes=volumes,
            compute=compute,
            provider_node_launches=provider_node_launches,
            containers=containers,
            container_shutdowns=container_shutdowns,
            scheduler_workers=worker_repository,
            scheduler_containers=container_repository,
            scheduler_container_requests=container_scheduler,
            scheduler_pool_states=pool_state_repository,
            capacity_reservation_repository=capacity_reservation_repository,
            scheduler_workloads=scheduler_workloads,
            images=images,
            agents=agents,
            object_storage=object_storage_service,
            cache_storage=cache_storage,
            metrics=metrics,
            worker_events=worker_events,
            usage=usage,
            routes=routes,
            checkpoints=checkpoints,
            autoscaler_states=autoscaler_states,
            volume_metering=volume_metering_service,
            payment_admission=payment_admission,
            volume_filesystem=resolved_volume_filesystem,
            aws_connections=aws_composition.service if aws_composition is not None else None,
            redis_client=redis,
            binary_redis_client=binary_redis_client,
            async_io=async_io,
            owns_redis_client=owns_redis_client,
            owns_binary_redis_client=owns_binary_redis_client,
            owned_resources=tuple(owned_runtime_resources),
        )
        return _compose_api_services(
            core,
            signal_service=signal_service,
            map_service=map_service,
            simple_queue_service=simple_queue_service,
            artifact_service=artifact_service,
            endpoint_service=endpoint_service,
            function_service=function_service,
            gateway_service=gateway_service,
            image_service=image_service,
            pod_service=pod_service,
            shell_service=shell_service,
            volume_service=volume_service,
            worker_repository_service=worker_repository_service,
        )

    def with_route_services(
        self,
        *,
        signal_service: RedisSignalService | None = None,
        map_service: RedisMapService | None = None,
        simple_queue_service: RedisSimpleQueueService | None = None,
        artifact_service: ArtifactStorageService | None = None,
        endpoint_service: EndpointApiService | None = None,
        function_service: FunctionApiService | None = None,
        gateway_service: GatewayControlService | None = None,
        image_service: ImageControlService | None = None,
        pod_service: PodControlService | None = None,
        shell_service: ShellControlService | None = None,
        volume_service: VolumeControlService | None = None,
        worker_repository_service: WorkerRepositoryService | None = None,
    ) -> ApiServices:
        return _compose_api_services(
            self,
            signal_service=(signal_service if signal_service is not None else self.signal_service),
            map_service=map_service if map_service is not None else self.map_service,
            simple_queue_service=(
                simple_queue_service
                if simple_queue_service is not None
                else self.simple_queue_service
            ),
            artifact_service=(
                artifact_service if artifact_service is not None else self.artifact_service
            ),
            endpoint_service=(
                endpoint_service if endpoint_service is not None else self.endpoint_service
            ),
            function_service=(
                function_service if function_service is not None else self.function_service
            ),
            gateway_service=(
                gateway_service if gateway_service is not None else self.gateway_service
            ),
            image_service=image_service if image_service is not None else self.image_service,
            pod_service=pod_service if pod_service is not None else self.pod_service,
            shell_service=shell_service if shell_service is not None else self.shell_service,
            volume_service=(volume_service if volume_service is not None else self.volume_service),
            worker_repository_service=(
                worker_repository_service
                if worker_repository_service is not None
                else self.worker_repository_service
            ),
        )

    def close(self) -> None:
        failures: list[Exception] = []
        route_prewarm_quiesced = True
        try:
            self.gateway_service.route_prewarmer.close()
        except Exception as exc:
            failures.append(exc)
            route_prewarm_quiesced = False
        try:
            self.auth_token_cache.close()
        except Exception as exc:
            failures.append(exc)
        if route_prewarm_quiesced:
            for resource in reversed(self.owned_resources):
                try:
                    resource.close()
                except Exception as exc:
                    failures.append(exc)
            if self.owns_redis_client:
                try:
                    self.redis_client.close()
                except Exception as exc:
                    failures.append(exc)
            if self.owns_binary_redis_client and self.binary_redis_client is not self.redis_client:
                try:
                    self.binary_redis_client.close()
                except Exception as exc:
                    failures.append(exc)
            try:
                self.context.database.dispose()
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup("API service shutdown was incomplete", failures)


def _compose_api_services(
    core: ApiServiceCore,
    *,
    signal_service: RedisSignalService | None,
    map_service: RedisMapService | None,
    simple_queue_service: RedisSimpleQueueService | None,
    artifact_service: ArtifactStorageService | None,
    endpoint_service: EndpointApiService | None,
    function_service: FunctionApiService | None,
    gateway_service: GatewayControlService | None,
    image_service: ImageControlService | None,
    pod_service: PodControlService | None,
    shell_service: ShellControlService | None,
    volume_service: VolumeControlService | None,
    worker_repository_service: WorkerRepositoryService | None,
) -> ApiServices:
    redis = core.redis()
    scheduler_workers = core.scheduler_workers
    scheduler_containers = core.scheduler_containers
    scheduler_pool_states = core.scheduler_pool_states
    route_resolver = SchedulerBackendRouteResolver(core.routes, scheduler_containers)
    route_dialer_config = core.backend_route_settings.to_dialer_config()
    transport_factory = HttpContainerServiceTransportFactory(
        route_resolver=route_resolver,
        route_dialer_config=route_dialer_config,
    )
    container_clients = SchedulerContainerClientFactory(
        scheduler_containers=scheduler_containers,
        transport_factory=transport_factory,
        service_token=core.container_service_settings.token.get_secret_value(),
    )
    proxy_client = PodProxySocketClient(
        route_resolver=route_resolver,
        route_dialer_config=route_dialer_config,
    )
    async_io = core.async_io
    async_database = async_io.database if async_io is not None else None
    async_scheduler_containers: AsyncRedisSchedulerContainerReader | None = None
    async_http: AsyncBackendHttpClient | None = None
    async_container_readiness: AsyncRedisContainerReadiness | None = None
    async_dispatcher: AsyncEndpointInstanceDispatcher | None = None
    if async_io is not None:
        async_scheduler_containers = AsyncRedisSchedulerContainerReader(async_io.redis)
        async_http = AsyncBackendHttpClient(
            route_resolver=route_resolver,
            route_dialer_config=route_dialer_config,
        )
        async_container_readiness = AsyncRedisContainerReadiness(async_io.redis, async_http)
        async_dispatcher = AsyncEndpointInstanceDispatcher(
            async_scheduler_containers,
            async_http,
            async_container_readiness,
            route_resolver=route_resolver,
            route_dialer_config=route_dialer_config,
        )
    endpoint = endpoint_service or EndpointControlService(
        core,
        async_database=async_database,
        async_dispatcher=async_dispatcher,
        gateway_http_url=lambda: core.gateway_settings.public_http_url,
    )
    function = function_service or FunctionControlService(
        core,
        gateway_http_url=lambda: core.gateway_settings.public_http_url,
        async_database=async_database,
        task_changes=AsyncTaskChangeReader(async_io.realtime) if async_io is not None else None,
    )
    gateway = gateway_service or _gateway_control_service(
        core,
        scheduler_workers=scheduler_workers,
        scheduler_containers=scheduler_containers,
        scheduler_pool_states=scheduler_pool_states,
        container_clients=container_clients,
        async_http=async_http,
        endpoint_dispatcher=async_dispatcher,
    )
    image = image_service or ImageControlService(
        core,
        base_image_digest_inspector=core.image_build_registry_settings.create_inspector(),
        build_context_reader=core.object_storage,
        registry_credential_resolver=ProductionRegistryCredentialResolver(),
    )
    pod = pod_service or _pod_control_service(
        core,
        scheduler_containers=scheduler_containers,
        container_clients=container_clients,
        proxy_client=proxy_client,
        async_database=async_database,
        async_scheduler_containers=async_scheduler_containers,
        async_http=async_http,
        async_container_readiness=async_container_readiness,
    )
    shell = shell_service or ShellControlService(
        core,
        scheduler_containers=scheduler_containers,
        container_clients=container_clients,
        backend_connector=partial(
            connect_shell_backend,
            route_resolver=route_resolver,
            route_dialer_config=route_dialer_config,
        ),
        async_database=async_database,
        async_scheduler_containers=async_scheduler_containers,
    )
    worker_repository = worker_repository_service or _worker_repository_service(
        core,
        scheduler_workers=scheduler_workers,
        scheduler_containers=scheduler_containers,
    )
    public_ingress_config = PublicIngressSettings()
    provider_node_enrollment = (
        ProviderNodeEnrollmentService(
            gateway=gateway,
            compute=core.compute,
            events=core.events,
            rate_limiter=redis,
            proof_max_inflight=public_ingress_config.provider_node_proof_max_inflight,
            client_ip_header=public_ingress_config.client_ip_header,
            launches=core.provider_node_launches,
            identity_verifier=configured_provider_node_identity_registry(
                aws=AwsProviderNodeIdentityAdapter(
                    http_client=BoundedProviderNodeIdentityHttpClient(),
                    replay_guard=RedisProviderNodeIdentityReplayGuard(redis),
                ),
                platform_settings=core.platform_capacity_settings,
                redis=redis,
            ),
        )
        if core.compute.provider_resolver is not None
        and core.compute.pool_bootstrap_factory is not None
        else None
    )
    autoscaler_operations = AutoscalerOperationsService(
        core,
        function_autoscaler=AutoscalingDriver(
            core,
            redis=redis,
            workload=FunctionAutoscaler(core, functions=function),
            container_states=scheduler_containers,
            container_requests=scheduler_workers,
        ),
        endpoint_autoscaler=AutoscalingDriver(
            core,
            redis=redis,
            workload=EndpointAutoscaler(
                core,
                endpoints=endpoint,
                dispatches=ApiEndpointDispatchAutoscalingReader(
                    EndpointDispatchStateRepository(core)
                ),
            ),
            container_states=scheduler_containers,
            container_requests=scheduler_workers,
        ),
        pod_autoscaler=AutoscalingDriver(
            core,
            redis=redis,
            workload=PodAutoscaler(
                core,
                redis=redis,
                pods=pod,
            ),
            container_states=scheduler_containers,
            container_requests=scheduler_workers,
        ),
    )
    scheduler_worker_admin = SchedulerWorkerAdminService(
        scheduler_workers,
        scheduler_containers,
        stop_container=lambda container_id: _stop_scheduler_worker_container(
            core,
            container_id,
        ),
    )
    return ApiServices(
        client_release_version=core.client_release_version,
        context=core.context,
        auth=core.auth,
        users=core.users,
        invitations=core.invitations,
        sign_in=core.sign_in,
        auth_token_cache=core.auth_token_cache,
        tcp_ingress_settings=core.tcp_ingress_settings,
        agent_route_reconciliation_settings=core.agent_route_reconciliation_settings,
        agent_disconnect_reconciliation_settings=core.agent_disconnect_reconciliation_settings,
        gateway_settings=core.gateway_settings,
        stripe_settings=core.stripe_settings,
        resend_settings=core.resend_settings,
        payment_provider=core.payment_provider,
        workspace_change_stream_settings=core.workspace_change_stream_settings,
        agent_binary_settings=core.agent_binary_settings,
        aws_account_connection_settings=core.aws_account_connection_settings,
        aws_capacity_settings=core.aws_capacity_settings,
        platform_capacity_settings=core.platform_capacity_settings,
        aws_capacity_reconciliation_settings=core.aws_capacity_reconciliation_settings,
        backend_route_settings=core.backend_route_settings,
        object_store_settings=core.object_store_settings,
        workspace_storage_issuer=core.workspace_storage_issuer,
        image_archive_settings=core.image_archive_settings,
        image_archive_presigner=core.image_archive_presigner,
        image_build_registry_settings=core.image_build_registry_settings,
        container_service_settings=core.container_service_settings,
        events=core.events,
        workspace_changes=core.workspace_changes,
        tasks=core.tasks,
        control_plane_service=core.control_plane_service,
        aws_account_connection_directory=core.aws_account_connection_directory,
        workspace_compute_policy_service=core.workspace_compute_policy_service,
        apps=core.apps,
        deployments=core.deployments,
        deployment_resources=core.deployment_resources,
        custom_domains=core.custom_domains,
        cron_jobs=core.cron_jobs,
        collections=core.collections,
        secrets=core.secrets,
        volumes=core.volumes,
        compute=core.compute,
        provider_node_launches=core.provider_node_launches,
        containers=core.containers,
        container_shutdowns=core.container_shutdowns,
        scheduler_workers=core.scheduler_workers,
        scheduler_containers=core.scheduler_containers,
        scheduler_container_requests=core.scheduler_container_requests,
        scheduler_pool_states=core.scheduler_pool_states,
        capacity_reservation_repository=core.capacity_reservation_repository,
        scheduler_workloads=core.scheduler_workloads,
        images=core.images,
        agents=core.agents,
        object_storage=core.object_storage,
        cache_storage=core.cache_storage,
        metrics=core.metrics,
        worker_events=core.worker_events,
        usage=core.usage,
        routes=core.routes,
        checkpoints=core.checkpoints,
        autoscaler_states=core.autoscaler_states,
        volume_metering=core.volume_metering,
        payment_admission=core.payment_admission,
        volume_filesystem=core.volume_filesystem,
        redis_client=core.redis_client,
        binary_redis_client=core.binary_redis_client,
        async_io=core.async_io,
        aws_connections=core.aws_connections,
        owns_redis_client=core.owns_redis_client,
        owns_binary_redis_client=core.owns_binary_redis_client,
        owned_resources=core.owned_resources,
        signal_service=signal_service or RedisSignalService(RedisSignalRepository(redis)),
        map_service=map_service or RedisMapService(core.binary_redis()),
        simple_queue_service=(simple_queue_service or RedisSimpleQueueService(core.binary_redis())),
        artifact_service=artifact_service
        or ArtifactStorageService(core.context, object_storage=core.object_storage),
        endpoint_service=endpoint,
        function_service=function,
        gateway_service=gateway,
        image_service=image,
        pod_service=pod,
        shell_service=shell,
        volume_service=(
            volume_service or VolumeControlService(core, filesystem=core.volume_filesystem)
        ),
        worker_repository_service=worker_repository,
        provider_node_enrollment_service=provider_node_enrollment,
        machine_lifecycle_service=MachineLifecycleService(
            gateway=gateway,
            provider_compute=core.compute,
        ),
        backend_route_resolver=route_resolver,
        backend_route_dialer_config=route_dialer_config,
        task_rerun_service=TaskRerunService(core, function_invoker=function),
        autoscaler_operations_service=autoscaler_operations,
        scheduler_worker_admin_service=scheduler_worker_admin,
    )


def _stop_scheduler_worker_container(
    core: ApiServiceCore,
    container_id: str,
) -> None:
    core.containers.stop(container_id, reason=StopContainerReason.Admin)


def _gateway_control_service(
    core: ApiServiceCore,
    *,
    scheduler_workers: RedisSchedulerWorkerRepository,
    scheduler_containers: RedisSchedulerContainerRepository,
    scheduler_pool_states: RedisWorkerPoolStateRepository,
    container_clients: SchedulerContainerClientFactory,
    async_http: AsyncBackendHttpClient | None,
    endpoint_dispatcher: AsyncEndpointInstanceDispatcher | None,
) -> GatewayControlService:
    def endpoint_rollout_readiness(stub_id: str, container_ids: list[str]) -> set[str]:
        if endpoint_dispatcher is None:
            raise RuntimeError("endpoint rollout readiness requires asynchronous API I/O")
        return from_thread.run(endpoint_dispatcher.ready_container_ids, stub_id, container_ids)

    compute_states = RedisComputeStateRepository(core.redis())
    route_dialer = BackendRouteDialer(
        config=core.backend_route_settings.to_dialer_config(),
    )
    return GatewayControlService(
        core,
        control_plane=core.control_plane_service,
        management=ManagementService(core),
        compute_state=compute_states,
        scheduler_workers=scheduler_workers,
        scheduler_containers=scheduler_containers,
        scheduler_pool_states=scheduler_pool_states,
        capacity_reservations=CapacityReservationService(
            core.capacity_reservation_repository,
            lambda: [],
        ),
        object_storage=core.object_storage,
        agent_image=AgentImageConfig(),
        event_streams=RedisEventStreamRepository(core.redis()),
        route_prewarmer=RoutePrewarmService(route_dialer, core.events),
        container_stopper=SchedulerContainerServiceStopper(container_clients),
        container_client_factory=container_clients,
        endpoint_rollout_readiness=endpoint_rollout_readiness,
        route_authenticator=core.backend_route_settings.to_authenticator(),
        gateway_endpoint=GatewayEndpointConfig(http_url=core.gateway_settings.public_http_url),
        agent_artifact_version=core.agent_binary_settings.binary_version,
        agent_sha256_by_arch=core.agent_binary_settings.binary_sha256_by_arch,
        runtime_origin=lambda: core.gateway_settings.runtime_callback_http_url,
        capacity_interruption_sink=SchedulerAgentCapacityInterruptionSink(
            SchedulerCapacityInterruptionService(
                SchedulerWorkerPreemptionService(
                    scheduler_workers,
                    scheduler_containers,
                    core.containers,
                ),
                scheduler_workers,
                maintenance=SchedulerWorkerMaintenanceService(scheduler_workers),
                workload_drains=WorkerWorkloadDrainService(
                    core.context.database, scheduler_containers
                ),
            )
        ),
        scheduler_maintenance=SchedulerWorkerMaintenanceService(scheduler_workers),
        async_http_client=async_http,
    )


def _pod_control_service(
    core: ApiServiceCore,
    *,
    scheduler_containers: RedisSchedulerContainerRepository,
    container_clients: SchedulerContainerClientFactory,
    proxy_client: PodProxySocketClient,
    async_database: AsyncDatabaseClient | None,
    async_scheduler_containers: AsyncRedisSchedulerContainerReader | None,
    async_http: AsyncBackendHttpClient | None,
    async_container_readiness: AsyncRedisContainerReadiness | None,
) -> PodControlService:
    async_io = core.async_io
    return PodControlService(
        core,
        gateway_http_url=core.gateway_settings.public_http_url,
        scheduler_containers=scheduler_containers,
        container_clients=container_clients,
        async_database=async_database,
        async_scheduler_containers=async_scheduler_containers,
        async_pod_proxy_http_client=(
            AsyncPodProxyHttpClient(async_http) if async_http is not None else None
        ),
        pod_proxy_socket_client=proxy_client,
        pod_proxy_connections=(
            AsyncRedisPodProxyConnectionRepository(async_io.redis) if async_io is not None else None
        ),
        container_readiness_probe=async_container_readiness,
        redis=core.redis(),
    )


def _worker_repository_service(
    core: ApiServiceCore,
    *,
    scheduler_workers: RedisSchedulerWorkerRepository,
    scheduler_containers: RedisSchedulerContainerRepository,
) -> WorkerRepositoryService:
    redis = core.redis()
    return WorkerRepositoryService(
        workers=scheduler_workers,
        containers=scheduler_containers,
        runtime_state=RedisContainerRuntimeStateRepository(redis),
        network=RedisWorkerNetworkIpRepository(redis),
        events=RedisEventBus(redis),
        container_credentials=WorkerCredentialService(
            services=core,
            container_repository=scheduler_containers,
            storage_issuer=core.workspace_storage_issuer,
        ),
        origin_credentials=WorkerCacheOriginCredentialService(
            services=core,
            config=_cache_origin_credential_config(),
            object_store_client=core.image_archive_presigner,
            archive_settings=core.image_archive_settings,
            registry_credentials=_workload_registry_credentials,
        ),
        source_cache=WorkerSourceCacheService(core.context),
        dependencies=WorkerRepositoryDependencies(
            compute=core.compute,
            context=core.context,
            auth=core.auth,
            deployment_resources=core.deployment_resources,
            checkpoints=core.checkpoints,
            images=core.images,
            object_storage=core.object_storage,
            worker_events=core.worker_events,
            usage=core.usage,
            workspace_changes=core.workspace_changes,
            preempted_containers=PreemptedContainerService(
                services=core,
                stubs=core.control_plane_service,
            ),
            tasks=core.tasks,
        ),
        redis=redis,
    )


def _billing_account_provisioner(
    context: ServiceContext, payments: Callable[[], PaymentProvider]
) -> BillingProvisioner:
    """Set a signed-in account up at the payment provider, in its own transaction.

    Composed here because identity does not know what billing is: sign-in is
    handed this the same way it is handed workspace provisioning.

    The provider is resolved before the row is read, so a deployment missing the
    credential refuses every sign-in rather than only the first-ever ones, where
    a returning person signing in quietly would hide the misconfiguration until
    the first invoice.
    """

    def provision(*, user_id: str, workspace_id: str) -> None:
        with context.database.session() as session:
            BillingAccountService(session).billing_account_for(
                payments(), user_id=user_id, workspace_id=workspace_id
            )

    return provision


def _workspace_bucket_client(client: ObjectByteClient) -> WorkspaceBucketClient | None:
    if isinstance(client, _RuntimeWorkspaceBucketClient):
        return client
    return None


def _cache_origin_credential_config() -> CacheOriginCredentialConfig:
    return CacheOriginCredentialConfig(
        image_registry_store=ImageRegistryStore.S3,
        image_archive_extension=IMAGE_ARCHIVE_EXTENSION,
    )


def _workload_registry_credentials(registry: str) -> ImageRegistryCredentials:
    ecr = parse_ecr_registry(registry)
    if ecr is None:
        if registry not in {"localhost", "127.0.0.1"} and not registry.startswith(
            ("localhost:", "127.0.0.1:")
        ):
            raise ValueError("workload image registry must be ECR or a local registry")
        return ImageRegistryCredentials(registry=registry)
    authorization = AwsProvider(AwsProviderSettings(region=ecr.region)).ecr_authorization(registry)
    return ImageRegistryCredentials(
        registry=authorization.registry,
        username=authorization.username,
        password=authorization.password,
        expires_at=authorization.expires_at,
    )


def _image_build_publication_publisher(
    cache_storage: CacheStorage,
    image_archive_settings: ImageArchiveSettings,
    execution_settings: ImageBuildExecutionSettings,
    registry_settings: ImageBuildRegistrySettings,
    *,
    context: ServiceContext,
    archive_store: ImageBuildArchiveObjectStore,
) -> ImageBuildPublicationPublisher:
    publishers: list[ImageBuildPublicationPublisher] = []
    publishers.append(
        ArchiveImageBuildPublicationPublisher(
            archive_store,
            settings=image_archive_settings,
            context=context,
        )
    )
    publishers.append(CacheImageBuildPublicationPublisher(cache_storage))
    registry_publisher = registry_settings.create_publication_publisher(
        default_docker_binary=execution_settings.docker_binary,
    )
    if registry_publisher is not None:
        publishers.append(registry_publisher)
    return CompositeImageBuildPublicationPublisher(tuple(publishers))
