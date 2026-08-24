from __future__ import annotations

import socket
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent.binary import AgentBinarySettings
from agent.service import AgentService
from compute.agent_control import AgentImageConfig, GatewayEndpointConfig
from compute.aws_connections import AwsAccountConnectionDirectory, AwsAccountConnectionService
from compute.policy import AwsDefaultCapacityBaseline, WorkspaceComputePolicyService
from compute.request_placement import ComputeCapacityPlacementService
from compute.service import ComputeService
from compute.state import ComputeAgentTokenState, RedisComputeStateRepository
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
from control.workspace_storage_state import ControlPlaneWorkspaceStorageState
from coordination.event_bus import RedisEventBus
from coordination.redis_client import RedisClient
from coordination.wake_signal import RedisWakeSignal
from database.context import ServiceContext
from database.records.apps import StubRecord
from execution.artifacts.service import ArtifactStorageService
from execution.collections.redis import RedisMapService, RedisSimpleQueueService
from execution.collections.service import CollectionService
from execution.containers.preemption import PreemptedContainerService
from execution.containers.readiness import ContainerReadiness
from execution.containers.runtime_state import RedisContainerRuntimeStateRepository
from execution.containers.scheduling import ContainerSchedulingPersistenceService
from execution.containers.service import ContainerService
from execution.endpoints.dispatch import (
    ACTIVE_ENDPOINT_DISPATCH_STATUSES,
    EndpointInstanceDispatcher,
    EndpointResponseStream,
)
from execution.endpoints.service import (
    EndpointControlService,
    EndpointDispatchStateRepository,
    EndpointIngressDispatchSession,
)
from execution.functions.service import FunctionControlService
from execution.pods.service import PodControlService
from execution.secrets.service import SecretService
from execution.shells.service import ShellControlService
from execution.signals.redis import RedisSignalRepository, RedisSignalService
from execution.task_rerun import TaskRerunService
from execution.tasks import TaskService
from execution.volumes.control import VolumeControlService
from execution.volumes.records import VolumeService
from gateway.container_readiness import RedisContainerReadiness
from gateway.container_transport import HttpContainerServiceTransportFactory
from gateway.machine_lifecycle import MachineLifecycleService
from gateway.pod_proxy import PodProxyHttpClient, RedisPodProxyConnectionRepository
from gateway.pool_bootstrap import pool_bootstrap_provisioner
from gateway.provider_enrollment import ProviderNodeEnrollmentService
from gateway.route_prewarm import RoutePrewarmService, TailnetPeerStatusProvider
from gateway.service import GatewayControlService
from gateway.settings import GatewaySettings
from gateway.shell_proxy import connect_shell_backend
from identity.auth import AuthService, AuthTokenCache
from identity.sign_in import BillingProvisioner, SignInService
from identity.users import UserService
from images.control import ImageControlService
from images.execution import (
    ImageBuildExecutor,
    ImageBuildExecutorKind,
)
from images.publication import (
    ArchiveImageBuildPublicationPublisher,
    CacheImageBuildPublicationPublisher,
    CompositeImageBuildPublicationPublisher,
    ImageBuildArchiveObjectStore,
    ImageBuildPublicationPublisher,
)
from images.scheduler_lifecycle import SchedulerImageBuildContainerStateStore
from images.service import ImageBuildService
from images.settings import (
    ImageBuildContainerSettings,
    ImageBuildExecutionSettings,
    ImageBuildRegistrySettings,
)
from networking.control_plane_origin import RedisControlPlaneOriginRepository
from networking.dialer import (
    BackendRouteDialer,
    BackendRouteDialerConfig,
    TailnetPeerResolver,
    TailnetPeerWaiter,
)
from networking.settings import (
    BackendRouteSettings,
    TailnetControlSettings,
    TailnetRuntimeSettings,
)
from networking.tailnet import TailnetRuntime
from networking.tailnet_control import TailscaleTailnetControl
from observability.events import EventService
from observability.metrics import MetricsService
from observability.settings import (
    VolumeMeteringSettings,
    WorkspaceChangeStreamSettings,
)
from observability.stream_state import RedisEventStreamRepository
from observability.usage import UsageService, WorkerEventService
from observability.workspace_changes import WorkspaceChangeRepository, WorkspaceChangeService
from operations.app_lifecycle import ProductionAppExecutionLifecycleEffects
from operations.container_shutdown import (
    ContainerShutdownService,
    DatabaseDurableWorkerAbsence,
)
from operations.management import ManagementService
from provider_clients import (
    AwsProviderNodeIdentityAdapter,
    ProductionRegistryCredentialResolver,
    configured_aws_compute_catalog,
    workspace_compute_provider_resolver,
)
from provider_clients.settings import (
    AwsAccountConnectionSettings,
    AwsCapacityReconciliationSettings,
    AwsCapacitySettings,
)
from provider_cloudflare import CloudflareSettings
from provider_github import GitHubAppSettings
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
    SchedulerWorkerPreemptionService,
)
from scheduler.routes import SchedulerBackendRouteResolver
from scheduler.services import SchedulerWorkloadDirectory
from scheduler.state import (
    RedisSchedulerContainerRepository,
    RedisSchedulerWorkerRepository,
    RedisWorkerNetworkIpRepository,
    RedisWorkerPoolStateRepository,
)
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
    FunctionSetResultBody,
    FunctionSetResultResponse,
)
from shared.identity import WorkspaceRecord, WorkspaceStorageConfig
from shared.payments import PaymentProvider
from shared.workspace_storage import WorkspaceStorageIssuer
from storage.image_archive import (
    IMAGE_ARCHIVE_EXTENSION,
    ImageArchiveSettings,
    ResolvedImageArchiveSettings,
)
from storage.retention_settings import RetentionSettings
from storage.service import CacheStorage, ObjectByteClient, ObjectStorage
from storage.volume_filesystem import (
    VolumeFilesystem,
    WorkspaceVolumeFilesystem,
    workspace_volume_store_resolver,
)
from storage.volume_metering import PersistentVolumeMeteringService
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings
from worker.container_client.scheduler import (
    SchedulerContainerClientFactory,
    SchedulerContainerServiceStopper,
)
from worker.image_lifecycle import ImageRegistryStore
from worker.settings import ContainerServiceSettings
from worker_repository.checkpoint_records import CheckpointService
from worker_repository.credentials import WorkerCredentialService
from worker_repository.image_build_container_execution import (
    ContainerServiceImageBuildExecutorFactory,
    ContainerServiceTransportFactory,
    SchedulerImageBuildContainerAddressResolver,
    StaticImageBuildContainerServiceTokenProvider,
)
from worker_repository.image_build_credentials import RedisImageBuildCredentialCache
from worker_repository.image_build_scheduler_execution import SchedulerImageBuildExecutor
from worker_repository.origin_credentials import (
    CacheOriginCredentialConfig,
    PresignedPutClient,
    WorkerCacheOriginCredentialService,
)
from worker_repository.source_cache import WorkerSourceCacheService

from api.server.provider_compute import (
    BoundedProviderNodeIdentityHttpClient,
    RedisProviderNodeIdentityReplayGuard,
    aws_account_connection_composition_from_settings,
)
from api.server.worker_repository_service import (
    WorkerRepositoryDependencies,
    WorkerRepositoryService,
)
from api.server.workspace_storage_composition import (
    WorkspaceStorageIssuerFactory,
    WorkspaceStorageIssuerSettings,
)
from api.settings import (
    AgentDisconnectReconciliationSettings,
    AgentRouteReconciliationSettings,
    PublicIngressSettings,
    TcpIngressSettings,
)
from billing import BillingAccountService, DatabaseBillingAdmission
from database import DatabaseClient


class ApiTailnetRuntime(
    TailnetPeerWaiter,
    TailnetPeerResolver,
    TailnetPeerStatusProvider,
    Protocol,
):
    def start(self) -> None: ...

    def close(self) -> None: ...

    def self_dns_name(self) -> str: ...

    def advertise_service(self, service: str, ports: tuple[int, ...]) -> str: ...


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
            )
        )


@runtime_checkable
class ApiOwnedResource(Protocol):
    def close(self) -> None: ...


class FunctionApiService(Protocol):
    def function_invoke(self, request: FunctionInvokeBody) -> FunctionInvokeResponse: ...

    def function_invoke_stream(
        self,
        request: FunctionInvokeBody,
        *,
        poll_interval_seconds: float = 0.25,
        keepalive_interval_seconds: float = 5.0,
    ) -> Iterable[FunctionInvokeResponse]: ...

    def assert_may_accept_invocation(self, stub_id: str) -> None: ...

    def unclaimed_task_count(self, stub_id: str) -> int: ...

    def start_function_container(self, stub_id: str) -> bool: ...

    def containers_holding_work(self, container_ids: Sequence[str]) -> set[str]: ...

    def function_claim(self, request: FunctionClaimRequest) -> FunctionClaimResponse: ...

    def function_set_result(self, request: FunctionSetResultBody) -> FunctionSetResultResponse: ...

    def function_monitor(self, request: FunctionMonitorRequest) -> FunctionMonitorResponse: ...


class EndpointApiService(Protocol):
    def start_endpoint_serve(
        self,
        request: StartEndpointServeRequest,
    ) -> StartEndpointServeResponse: ...

    def forward_endpoint_request(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse: ...

    def forward_endpoint_health(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse: ...

    def prepare_asgi_websocket(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointIngressDispatchSession: ...

    def prepare_asgi_http(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointIngressDispatchSession: ...

    def heartbeat_asgi_websocket(self, task_id: str) -> None: ...

    def open_asgi_websocket_socket(
        self,
        session: EndpointIngressDispatchSession,
    ) -> socket.socket | None: ...

    def open_asgi_http_stream(
        self,
        session: EndpointIngressDispatchSession,
        request: EndpointForwardRequest,
    ) -> EndpointResponseStream: ...

    def finish_asgi_http(
        self,
        task_id: str,
        *,
        status_code: int | None = None,
        body_size_bytes: int = 0,
        cancelled: bool = False,
        error: str | None = None,
    ) -> None: ...

    def finish_asgi_websocket(
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

    def active_count(self, stub_id: str) -> int:
        return self.repository.active_count(stub_id)

    def list_by_stub(self, stub_id: str) -> list[EndpointAutoscalingDispatchObservation]:
        return [
            EndpointAutoscalingDispatchObservation(
                container_id=record.container_id,
                active=record.status in ACTIVE_ENDPOINT_DISPATCH_STATUSES,
                finished_at=record.finished_at,
            )
            for record in self.repository.list_by_stub(stub_id)
        ]


@runtime_checkable
class _RuntimeWorkspaceBucketClient(Protocol):
    def create_bucket(self, bucket: str | None = None) -> None: ...

    def validate_bucket_access(self, bucket: str | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class ApiServiceCore:
    context: ServiceContext
    auth: AuthService
    users: UserService
    sign_in: SignInService
    auth_token_cache: AuthTokenCache
    tcp_ingress_settings: TcpIngressSettings
    agent_route_reconciliation_settings: AgentRouteReconciliationSettings
    agent_disconnect_reconciliation_settings: AgentDisconnectReconciliationSettings
    gateway_settings: GatewaySettings
    stripe_settings: StripeSettings
    payment_provider: Callable[[], PaymentProvider]
    workspace_change_stream_settings: WorkspaceChangeStreamSettings
    agent_binary_settings: AgentBinarySettings
    aws_account_connection_settings: AwsAccountConnectionSettings
    aws_capacity_settings: AwsCapacitySettings
    aws_capacity_reconciliation_settings: AwsCapacityReconciliationSettings
    tailnet_runtime_settings: TailnetRuntimeSettings
    tailnet_control_settings: TailnetControlSettings
    backend_route_settings: BackendRouteSettings
    object_store_settings: S3ObjectStoreSettings
    workspace_storage_issuer: WorkspaceStorageIssuer
    image_archive_settings: ResolvedImageArchiveSettings
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
    aws_connections: AwsAccountConnectionService | None
    owns_redis_client: bool
    owns_binary_redis_client: bool
    tailnet_runtime: ApiTailnetRuntime | None
    owned_resources: tuple[ApiOwnedResource, ...]

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
        workspace_change_stream_settings: WorkspaceChangeStreamSettings | None = None,
        agent_binary_settings: AgentBinarySettings | None = None,
        aws_account_connection_settings: AwsAccountConnectionSettings | None = None,
        aws_capacity_settings: AwsCapacitySettings | None = None,
        aws_capacity_reconciliation_settings: AwsCapacityReconciliationSettings | None = None,
        tailnet_runtime_settings: TailnetRuntimeSettings | None = None,
        tailnet_control_settings: TailnetControlSettings | None = None,
        backend_route_settings: BackendRouteSettings | None = None,
        object_store_settings: S3ObjectStoreSettings | None = None,
        workspace_storage_issuer: WorkspaceStorageIssuer | None = None,
        object_storage: ObjectStorage | None = None,
        object_store_client: ObjectByteClient | None = None,
        workspace_storage_client: WorkspaceBucketClient | None = None,
        image_archive_settings: ImageArchiveSettings | None = None,
        image_archive_store: ImageBuildArchiveObjectStore | None = None,
        image_archive_presigner: PresignedPutClient | None = None,
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
        image_build_executor: ImageBuildExecutor | None = None,
        image_build_container_transport_factory: ContainerServiceTransportFactory | None = None,
        tailnet_peer_waiter: TailnetPeerWaiter | None = None,
        tailnet_peer_resolver: TailnetPeerResolver | None = None,
        redis_client: RedisClient,
        binary_redis_client: RedisClient,
        owns_redis_client: bool = False,
        owns_binary_redis_client: bool = False,
        tailnet_runtime: ApiTailnetRuntime | None = None,
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
        workspace_change_stream_config = (
            workspace_change_stream_settings or WorkspaceChangeStreamSettings()
        )
        agent_artifact_config = agent_binary_settings or AgentBinarySettings()
        aws_account_connection_config = (
            aws_account_connection_settings or AwsAccountConnectionSettings()
        )
        aws_capacity_config = aws_capacity_settings or AwsCapacitySettings()
        aws_capacity_reconciliation_config = (
            aws_capacity_reconciliation_settings or AwsCapacityReconciliationSettings()
        )
        resolved_tailnet_runtime_settings = tailnet_runtime_settings or TailnetRuntimeSettings()
        resolved_tailnet_control_settings = tailnet_control_settings or TailnetControlSettings()
        resolved_backend_route_settings = backend_route_settings or BackendRouteSettings()
        object_store_config = object_store_settings or S3ObjectStoreSettings()
        image_archive_config = (image_archive_settings or ImageArchiveSettings()).resolve(
            object_store_config
        )
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
        events = EventService(context, stream_events=stream_events)
        workspace_changes = WorkspaceChangeService(
            WorkspaceChangeRepository(
                redis,
                max_length=workspace_change_stream_config.max_length,
            )
        )
        tasks = TaskService(
            context,
            events,
            workspace_changes=workspace_changes,
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
        resolved_image_archive_presigner = image_archive_presigner
        if resolved_image_archive_presigner is None and isinstance(
            image_archive_store,
            PresignedPutClient,
        ):
            resolved_image_archive_presigner = image_archive_store
        if (
            resolved_image_archive_presigner is None
            and image_archive_config.storage == object_store_config
            and isinstance(object_storage_service.object_client, PresignedPutClient)
        ):
            resolved_image_archive_presigner = object_storage_service.object_client
        if resolved_image_archive_presigner is None:
            created_image_archive_presigner = S3ObjectStoreClient.from_settings(
                image_archive_config.storage
            )
            resolved_image_archive_presigner = created_image_archive_presigner
            owned_runtime_resources.append(created_image_archive_presigner)
        control_plane = ControlPlaneService(
            context,
            workspace_storage_client=(
                workspace_storage_client
                or _workspace_bucket_client(object_storage_service.object_client)
            ),
            workspace_storage_client_factory=_workspace_storage_client,
            workspace_changes=workspace_changes,
        )
        # One issuer, built once and handed to both the side that provisions a
        # workspace's bucket and the side that vends against it. Two would be two
        # opinions about what a workspace's credential is.
        workspace_storage_issuer = (
            workspace_storage_issuer
            or WorkspaceStorageIssuerFactory(
                state=ControlPlaneWorkspaceStorageState(control_plane),
                settings=WorkspaceStorageIssuerSettings(),
            ).create()
        )
        control_plane.workspace_storage_issuer = workspace_storage_issuer
        payment_provider = stripe_config.provider_factory()
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
        resolved_volume_filesystem = volume_filesystem or WorkspaceVolumeFilesystem(
            resolve_store=workspace_volume_store_resolver(
                lambda workspace_id: control_plane.get_workspace(workspace_id).storage,
                default_endpoint_url=object_store_config.endpoint_url,
                default_presigned_endpoint_url=object_store_config.presigned_endpoint_url,
            )
        )
        worker_repository = RedisSchedulerWorkerRepository(redis)
        container_repository = RedisSchedulerContainerRepository(redis)
        pool_state_repository = RedisWorkerPoolStateRepository(redis)
        capacity_reservation_repository = RedisCapacityReservationRepository(redis)
        container_transport_factory = (
            image_build_container_transport_factory
            or HttpContainerServiceTransportFactory(
                route_resolver=SchedulerBackendRouteResolver(
                    routes,
                    container_repository,
                ),
                route_dialer_config=resolved_backend_route_settings.to_dialer_config(),
                tailnet_peer_waiter=tailnet_peer_waiter,
                tailnet_peer_resolver=tailnet_peer_resolver,
            )
        )
        usage = UsageService(
            context,
            workspace_changes=workspace_changes,
        )
        volume_metering_service = volume_metering or (
            PersistentVolumeMeteringService.from_settings(
                context,
                filesystem=resolved_volume_filesystem,
                interval_seconds=volume_metering_config.interval_seconds,
            )
        )
        aws_connection_directory = AwsAccountConnectionDirectory(context)
        # Connected AWS is an optional deployment shape. When it is unconfigured there is
        # no connection to resolve, and building the resolver would demand the remote
        # network configuration a local stack has no reason to hold. A half-configured
        # deployment never reaches here: the settings validator rejects it.
        provider_resolver = (
            workspace_compute_provider_resolver(
                aws_capacity_config,
                agent_artifact_config,
                connections=aws_connection_directory.list_for_workspace,
                gateway_origin=gateway_config.public_http_url,
                internal_origin=gateway_config.runtime_callback_http_url,
                presigned_origin=object_store_config.presigned_endpoint_url or "",
                tailnet_runtime=resolved_tailnet_runtime_settings,
                tailnet_control=resolved_tailnet_control_settings,
                backend_route=resolved_backend_route_settings,
            )
            if aws_account_connection_config.configured
            else None
        )

        pool_bootstrap = None
        if provider_resolver is not None:
            agent_version, agent_sha256 = agent_artifact_config.require_amd64()
            pool_bootstrap = pool_bootstrap_provisioner(
                context,
                # A node in a customer VPC holds no tailnet session when it
                # first reports, so this is the public origin. The runtime
                # callback origin stays worker-facing and is not interchangeable
                # here. The scheduler must pass the same one: a disagreement
                # shows up as launch templates alternating between versions.
                control_plane_url=gateway_config.public_http_url,
                agent_version=agent_version,
                agent_sha256=agent_sha256,
                agent_binary_url=aws_capacity_config.agent_binary_url,
                worker_image_digest=aws_capacity_config.worker_image_digest,
                tailnet_control=resolved_tailnet_control_settings,
            )

        scheduler_hooks = SchedulerComputeHooks(
            RedisComputeStateRepository(redis),
            worker_repository,
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
            internal_origin=gateway_config.runtime_callback_http_url,
            tailnet_runtime=resolved_tailnet_runtime_settings,
            tailnet_control=resolved_tailnet_control_settings,
            backend_route=resolved_backend_route_settings,
            workspace_changes=workspace_changes,
            capacity_baseline=compute_policies,
            available_catalog=aws_compute_catalog,
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
        )
        container_shutdowns = ContainerShutdownService(
            container_repository,
            RedisEventBus(redis),
            redis,
            DatabaseDurableWorkerAbsence(context, worker_repository),
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
            workspace_changes=workspace_changes,
            placement_resources=placement_resources,
        )
        resolved_image_build_executor = image_build_executor or _image_build_executor(
            image_build_execution_config,
            image_build_container_config,
            container_service_config,
            scheduler=container_scheduler,
            container_repository=container_repository,
            redis_client=redis,
            image_build_container_transport_factory=container_transport_factory,
            containers=containers,
        )
        resolved_image_archive_store = image_archive_store
        if resolved_image_archive_store is None and isinstance(
            resolved_image_archive_presigner,
            ImageBuildArchiveObjectStore,
        ):
            resolved_image_archive_store = resolved_image_archive_presigner
        publication_composition = _image_build_publication_publisher(
            cache_storage,
            image_archive_config,
            image_build_execution_config,
            image_build_registry_config,
            context=context,
            archive_store=resolved_image_archive_store,
            archive_promotion_required=(resolved_image_build_executor.requires_archive_publication),
        )
        if publication_composition.owned_archive_store is not None:
            owned_runtime_resources.append(publication_composition.owned_archive_store)
        images = ImageBuildService(
            context,
            events,
            resolved_image_build_executor,
            publication_composition.publisher,
            archive_settings=image_archive_config,
            archive_store=resolved_image_archive_store,
        )
        deployment_resources = DeploymentResourceService(context)
        custom_domains = CustomDomainService(
            context=context,
            provider_factory=CloudflareSettings().provider,
            platform_base_domain=gateway_config.public_base_domain,
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
        resolved_tailnet_runtime = tailnet_runtime or TailnetRuntime(
            resolved_tailnet_runtime_settings
        )
        core = ApiServiceCore(
            context=context,
            auth=auth,
            users=users,
            sign_in=sign_in,
            auth_token_cache=auth_token_cache,
            tcp_ingress_settings=tcp_ingress_config,
            agent_route_reconciliation_settings=agent_route_reconciliation_config,
            agent_disconnect_reconciliation_settings=agent_disconnect_reconciliation_config,
            gateway_settings=gateway_config,
            stripe_settings=stripe_config,
            payment_provider=payment_provider,
            workspace_change_stream_settings=workspace_change_stream_config,
            agent_binary_settings=agent_artifact_config,
            aws_account_connection_settings=aws_account_connection_config,
            aws_capacity_settings=aws_capacity_config,
            aws_capacity_reconciliation_settings=aws_capacity_reconciliation_config,
            tailnet_runtime_settings=resolved_tailnet_runtime_settings,
            tailnet_control_settings=resolved_tailnet_control_settings,
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
            owns_redis_client=owns_redis_client,
            owns_binary_redis_client=owns_binary_redis_client,
            tailnet_runtime=resolved_tailnet_runtime,
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
        image_dependencies_quiesced = True
        try:
            self.images.close()
        except Exception as exc:
            failures.append(exc)
            image_dependencies_quiesced = self.images.active_background_execution_count == 0
        try:
            self.auth_token_cache.close()
        except Exception as exc:
            failures.append(exc)
        if route_prewarm_quiesced and image_dependencies_quiesced:
            for resource in reversed(self.owned_resources):
                try:
                    resource.close()
                except Exception as exc:
                    failures.append(exc)
            if self.tailnet_runtime is not None:
                try:
                    self.tailnet_runtime.close()
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
    tailnet_runtime = core.tailnet_runtime
    scheduler_containers = core.scheduler_containers
    scheduler_pool_states = core.scheduler_pool_states
    route_resolver = SchedulerBackendRouteResolver(core.routes, scheduler_containers)
    route_dialer_config = core.backend_route_settings.to_dialer_config()
    transport_factory = HttpContainerServiceTransportFactory(
        route_resolver=route_resolver,
        route_dialer_config=route_dialer_config,
        tailnet_peer_waiter=tailnet_runtime,
        tailnet_peer_resolver=tailnet_runtime,
    )
    container_clients = SchedulerContainerClientFactory(
        scheduler_containers=scheduler_containers,
        transport_factory=transport_factory,
        service_token=core.container_service_settings.token.get_secret_value(),
    )
    proxy_client = PodProxyHttpClient(
        route_resolver=route_resolver,
        route_dialer_config=route_dialer_config,
        tailnet_peer_waiter=tailnet_runtime,
        tailnet_peer_resolver=tailnet_runtime,
    )
    container_readiness = RedisContainerReadiness(redis, proxy_client, proxy_client)
    endpoint = endpoint_service or EndpointControlService(
        core,
        dispatcher=EndpointInstanceDispatcher(
            scheduler_containers,
            route_resolver=route_resolver,
            route_dialer_config=route_dialer_config,
            tailnet_peer_waiter=tailnet_runtime,
            tailnet_peer_resolver=tailnet_runtime,
            readiness_probe=container_readiness,
        ),
        gateway_http_url=RedisControlPlaneOriginRepository(core.redis_client).resolve,
    )
    function = function_service or FunctionControlService(
        core,
        gateway_http_url=RedisControlPlaneOriginRepository(core.redis_client).resolve,
    )
    gateway = gateway_service or _gateway_control_service(
        core,
        scheduler_workers=scheduler_workers,
        scheduler_containers=scheduler_containers,
        scheduler_pool_states=scheduler_pool_states,
        container_clients=container_clients,
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
        container_readiness=container_readiness,
    )
    shell = shell_service or ShellControlService(
        core,
        scheduler_containers=scheduler_containers,
        container_clients=container_clients,
        backend_connector=partial(
            connect_shell_backend,
            route_resolver=route_resolver,
            route_dialer_config=route_dialer_config,
            tailnet_peer_waiter=tailnet_runtime,
            tailnet_peer_resolver=tailnet_runtime,
        ),
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
            identity_verifier=AwsProviderNodeIdentityAdapter(
                http_client=BoundedProviderNodeIdentityHttpClient(),
                replay_guard=RedisProviderNodeIdentityReplayGuard(redis),
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
        context=core.context,
        auth=core.auth,
        users=core.users,
        sign_in=core.sign_in,
        auth_token_cache=core.auth_token_cache,
        tcp_ingress_settings=core.tcp_ingress_settings,
        agent_route_reconciliation_settings=core.agent_route_reconciliation_settings,
        agent_disconnect_reconciliation_settings=core.agent_disconnect_reconciliation_settings,
        gateway_settings=core.gateway_settings,
        stripe_settings=core.stripe_settings,
        payment_provider=core.payment_provider,
        workspace_change_stream_settings=core.workspace_change_stream_settings,
        agent_binary_settings=core.agent_binary_settings,
        aws_account_connection_settings=core.aws_account_connection_settings,
        aws_capacity_settings=core.aws_capacity_settings,
        aws_capacity_reconciliation_settings=core.aws_capacity_reconciliation_settings,
        tailnet_runtime_settings=core.tailnet_runtime_settings,
        tailnet_control_settings=core.tailnet_control_settings,
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
        aws_connections=core.aws_connections,
        owns_redis_client=core.owns_redis_client,
        owns_binary_redis_client=core.owns_binary_redis_client,
        tailnet_runtime=core.tailnet_runtime,
        owned_resources=core.owned_resources,
        signal_service=signal_service or RedisSignalService(RedisSignalRepository(redis)),
        map_service=map_service or RedisMapService(core.binary_redis()),
        simple_queue_service=(simple_queue_service or RedisSimpleQueueService(core.binary_redis())),
        artifact_service=artifact_service or ArtifactStorageService(core.context),
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
) -> GatewayControlService:
    compute_states = RedisComputeStateRepository(core.redis())
    route_dialer = BackendRouteDialer(
        config=core.backend_route_settings.to_dialer_config(),
        tailnet_peer_waiter=core.tailnet_runtime,
        tailnet_peer_resolver=core.tailnet_runtime,
    )
    tailnet_control_config = core.tailnet_control_settings.to_control_config()
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
        route_prewarmer=RoutePrewarmService(
            route_dialer,
            core.events,
            peer_provider=core.tailnet_runtime,
        ),
        container_stopper=SchedulerContainerServiceStopper(container_clients),
        container_client_factory=container_clients,
        tailnet=core.tailnet_runtime_settings.to_agent_config(),
        tailnet_control=(
            TailscaleTailnetControl(tailnet_control_config)
            if tailnet_control_config is not None
            else None
        ),
        route_authenticator=core.backend_route_settings.to_authenticator(),
        gateway_endpoint=GatewayEndpointConfig(http_url=core.gateway_settings.public_http_url),
        agent_artifact_version=core.agent_binary_settings.binary_version,
        agent_sha256_by_arch=core.agent_binary_settings.binary_sha256_by_arch,
        runtime_origin=RedisControlPlaneOriginRepository(core.redis_client).resolve,
        capacity_interruption_sink=SchedulerAgentCapacityInterruptionSink(
            SchedulerCapacityInterruptionService(
                SchedulerWorkerPreemptionService(
                    scheduler_workers,
                    scheduler_containers,
                    core.containers,
                ),
                scheduler_workers,
            )
        ),
    )


def _pod_control_service(
    core: ApiServiceCore,
    *,
    scheduler_containers: RedisSchedulerContainerRepository,
    container_clients: SchedulerContainerClientFactory,
    proxy_client: PodProxyHttpClient,
    container_readiness: ContainerReadiness,
) -> PodControlService:
    return PodControlService(
        core,
        gateway_http_url=core.gateway_settings.public_http_url,
        scheduler_containers=scheduler_containers,
        container_clients=container_clients,
        pod_proxy_http_client=proxy_client,
        pod_proxy_socket_client=proxy_client,
        pod_proxy_connections=RedisPodProxyConnectionRepository(core.redis()),
        container_readiness_probe=container_readiness,
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
            platform_storage_endpoint=core.object_store_settings.endpoint_url or "",
            platform_storage_public_endpoint=(
                core.object_store_settings.presigned_endpoint_url or ""
            ),
            storage_issuer=core.workspace_storage_issuer,
        ),
        origin_credentials=WorkerCacheOriginCredentialService(
            services=core,
            config=_cache_origin_credential_config(),
            object_store_client=core.image_archive_presigner,
            archive_settings=core.image_archive_settings,
        ),
        source_cache=WorkerSourceCacheService(core.context),
        dependencies=WorkerRepositoryDependencies(
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


def _workspace_storage_client(storage: WorkspaceStorageConfig) -> S3ObjectStoreClient:
    return S3ObjectStoreClient.from_settings(
        S3ObjectStoreSettings(
            bucket=storage.bucket or "",
            endpoint_url=storage.endpoint_url or None,
            region_name=storage.region or "us-east-1",
            access_key_id=storage.access_key,
            secret_access_key=storage.secret_key,
            force_path_style=storage.force_path_style,
        )
    )


def _workspace_bucket_client(client: ObjectByteClient) -> WorkspaceBucketClient | None:
    if isinstance(client, _RuntimeWorkspaceBucketClient):
        return client
    return None


def _cache_origin_credential_config() -> CacheOriginCredentialConfig:
    return CacheOriginCredentialConfig(
        image_registry_store=ImageRegistryStore.S3,
        image_archive_extension=IMAGE_ARCHIVE_EXTENSION,
    )


@dataclass(frozen=True, slots=True)
class _ImageBuildPublicationComposition:
    publisher: ImageBuildPublicationPublisher
    owned_archive_store: ApiOwnedResource | None = None


def _image_build_publication_publisher(
    cache_storage: CacheStorage,
    image_archive_settings: ResolvedImageArchiveSettings,
    execution_settings: ImageBuildExecutionSettings,
    registry_settings: ImageBuildRegistrySettings,
    *,
    context: ServiceContext,
    archive_store: ImageBuildArchiveObjectStore | None,
    archive_promotion_required: bool,
) -> _ImageBuildPublicationComposition:
    publishers: list[ImageBuildPublicationPublisher] = []
    owned_archive_store: ApiOwnedResource | None = None
    if archive_promotion_required:
        resolved_archive_store = archive_store
        if resolved_archive_store is None:
            created_archive_store = S3ObjectStoreClient.from_settings(
                image_archive_settings.storage
            )
            resolved_archive_store = created_archive_store
            owned_archive_store = created_archive_store
        if not isinstance(resolved_archive_store, ImageBuildArchiveObjectStore):
            raise RuntimeError("image archive object store cannot verify immutable candidates")
        publishers.append(
            ArchiveImageBuildPublicationPublisher(
                resolved_archive_store,
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
    if len(publishers) == 1:
        publisher = publishers[0]
    else:
        publisher = CompositeImageBuildPublicationPublisher(tuple(publishers))
    return _ImageBuildPublicationComposition(
        publisher=publisher,
        owned_archive_store=owned_archive_store,
    )


def _image_build_executor(
    execution_settings: ImageBuildExecutionSettings,
    container_settings: ImageBuildContainerSettings,
    container_service_settings: ContainerServiceSettings,
    *,
    scheduler: SchedulerContainerRequestService,
    container_repository: RedisSchedulerContainerRepository,
    redis_client: RedisClient,
    image_build_container_transport_factory: ContainerServiceTransportFactory | None,
    containers: ContainerService,
) -> ImageBuildExecutor:
    if execution_settings.executor is not ImageBuildExecutorKind.BuildContainer:
        return execution_settings.create_executor()
    if image_build_container_transport_factory is None:
        msg = "build-container image executor requires a container service transport factory"
        raise RuntimeError(msg)

    return SchedulerImageBuildExecutor(
        scheduler=scheduler,
        executor_factory=ContainerServiceImageBuildExecutorFactory(
            SchedulerImageBuildContainerAddressResolver(container_repository),
            image_build_container_transport_factory,
            StaticImageBuildContainerServiceTokenProvider(
                container_service_settings.token.get_secret_value()
            ),
            poll_interval_seconds=container_settings.address_poll_interval_seconds,
        ),
        pending_container_state=SchedulerImageBuildContainerStateStore(container_repository),
        pool_selector=container_settings.pool_selector,
        cpu_millicores=container_settings.cpu_millicores,
        memory_mib=container_settings.memory_mib,
        address_wait_timeout_seconds=container_settings.address_wait_timeout_seconds,
        address_poll_interval_seconds=container_settings.address_poll_interval_seconds,
        credential_cache=RedisImageBuildCredentialCache(redis_client),
        containers=containers,
    )
