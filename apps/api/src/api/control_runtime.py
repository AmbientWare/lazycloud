from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from threading import Lock

from coordination.redis_client import RedisClient, RedisSettings
from execution.artifacts.service import ArtifactStorageService
from execution.collections.redis import RedisMapService, RedisSimpleQueueService
from execution.pods.service import PodControlService
from execution.shells.service import ShellControlService
from execution.signals.redis import RedisSignalService
from execution.volumes.control import VolumeControlService
from gateway.service import GatewayControlService
from gateway.settings import GatewaySettings
from identity.token_invalidation import AuthTokenInvalidation, configure_token_invalidation
from images.control import ImageControlService
from images.execution import ImageBuildExecutorKind
from images.settings import (
    ImageBuildContainerSettings,
    ImageBuildExecutionSettings,
    ImageBuildRegistrySettings,
)
from networking.settings import (
    BackendRouteSettings,
    TailnetControlSettings,
    TailnetRuntimeSettings,
)
from networking.tailnet import TailnetRuntime
from observability.settings import (
    TelemetrySettings,
    UsageMetricsSettings,
    UsagePricingSettings,
    VolumeMeteringSettings,
    WorkspaceChangeStreamSettings,
)
from observability.telemetry import TelemetryConfig
from provider_clients.release import resolve_deployment_release
from provider_clients.settings import AwsCapacityReconciliationSettings
from shared.app_identity import CONTROL_PLANE_SERVICE_NAME
from shared.compute_policy import MachinePool, UnitName
from shared.enums import StringEnum
from storage.image_archive import ImageArchiveSettings
from storage.retention_settings import RetentionSettings
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings
from worker.settings import ContainerServiceSettings

from api.server.services import (
    ApiOwnedResource,
    ApiServices,
    EndpointApiService,
    FunctionApiService,
    TaskQueueApiService,
)
from api.server.worker_repository_service import WorkerRepositoryService
from api.settings import (
    AgentRouteReconciliationSettings,
    CapacityBootstrapSettings,
    TcpIngressSettings,
)
from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


class ControlPlaneRuntimeState(StringEnum):
    New = "new"
    Starting = "starting"
    Serving = "serving"
    Stopping = "stopping"
    Closed = "closed"


type ApiServicesFactory = Callable[[], ApiServices]


@dataclass(slots=True)
class ControlPlaneRuntime:
    telemetry_config: TelemetryConfig = field(default_factory=TelemetryConfig)
    _services: ApiServices | None = field(default=None, repr=False)
    _factory: ApiServicesFactory | None = field(default=None, repr=False)
    _owns_services: bool = field(default=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    state: ControlPlaneRuntimeState = field(default=ControlPlaneRuntimeState.New, init=False)

    @classmethod
    def production(
        cls,
        *,
        telemetry_settings: TelemetrySettings | None = None,
    ) -> ControlPlaneRuntime:
        telemetry = telemetry_settings or TelemetrySettings()
        return cls(
            telemetry_config=telemetry.to_config(service_name=CONTROL_PLANE_SERVICE_NAME),
            _factory=_production_api_services,
            _owns_services=True,
        )

    @classmethod
    def from_services(
        cls,
        services: ApiServices,
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
        taskqueue_service: TaskQueueApiService | None = None,
        worker_repository_service: WorkerRepositoryService | None = None,
    ) -> ControlPlaneRuntime:
        overrides = (
            signal_service,
            map_service,
            simple_queue_service,
            artifact_service,
            endpoint_service,
            function_service,
            gateway_service,
            image_service,
            pod_service,
            shell_service,
            volume_service,
            taskqueue_service,
            worker_repository_service,
        )
        graph = (
            services.with_route_services(
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
                taskqueue_service=taskqueue_service,
                worker_repository_service=worker_repository_service,
            )
            if any(override is not None for override in overrides)
            else services
        )
        return cls(_services=graph, _owns_services=True)

    @property
    def services(self) -> ApiServices:
        with self._lock:
            if self._services is None or self.state is not ControlPlaneRuntimeState.Serving:
                raise RuntimeError("control-plane runtime is not serving")
            return self._services

    def start(self) -> ApiServices:
        with self._lock:
            if self.state is not ControlPlaneRuntimeState.New:
                raise RuntimeError(f"control-plane runtime cannot start from {self.state.value}")
            self.state = ControlPlaneRuntimeState.Starting
            try:
                if self._services is None:
                    if self._factory is None:
                        raise RuntimeError("control-plane runtime has no service factory")
                    self._services = self._factory()
                configure_token_invalidation(
                    AuthTokenInvalidation.from_redis(self._services.redis_client)
                )
                self._services.workspace_compute_policy_service.reconcile_capacity_at_startup()
            except BaseException as startup_error:
                cleanup_error: BaseException | None = None
                try:
                    if self._owns_services and self._services is not None:
                        self._services.close()
                except BaseException as exc:
                    cleanup_error = exc
                finally:
                    configure_token_invalidation(None)
                    self._services = None
                    self.state = ControlPlaneRuntimeState.Closed
                if cleanup_error is not None:
                    raise BaseExceptionGroup(
                        "control-plane startup and rollback both failed",
                        [startup_error, cleanup_error],
                    ) from None
                raise
            self.state = ControlPlaneRuntimeState.Serving
            return self._services

    def stop(self) -> None:
        with self._lock:
            if self.state is ControlPlaneRuntimeState.Closed:
                return
            self.state = ControlPlaneRuntimeState.Stopping
            services = self._services
            try:
                if self._owns_services and services is not None:
                    services.close()
            finally:
                configure_token_invalidation(None)
                self._services = None
                self.state = ControlPlaneRuntimeState.Closed


def _production_api_services() -> ApiServices:
    tcp_ingress_settings = TcpIngressSettings()
    agent_route_reconciliation_settings = AgentRouteReconciliationSettings()
    capacity_bootstrap_settings = CapacityBootstrapSettings()
    gateway_settings = GatewaySettings()
    workspace_change_stream_settings = WorkspaceChangeStreamSettings()
    # The install routes serve exactly the agent artifact this names, so the
    # release resolves before the service graph exists rather than beside it: a
    # control plane that came up without it would answer for a release it does
    # not have.
    release = resolve_deployment_release()
    print(f"control plane {release.describe()}", file=sys.stderr, flush=True)
    agent_binary_settings = release.agent_binaries
    aws_account_connection_settings = release.aws_connections
    aws_capacity_settings = release.aws_capacity
    aws_capacity_reconciliation_settings = AwsCapacityReconciliationSettings()
    redis_settings = RedisSettings()
    object_store_settings = S3ObjectStoreSettings()
    image_archive_settings = ImageArchiveSettings()
    image_build_execution_settings = ImageBuildExecutionSettings()
    image_build_registry_settings = ImageBuildRegistrySettings()
    image_build_container_settings = ImageBuildContainerSettings()
    container_service_settings = ContainerServiceSettings()
    retention_settings = RetentionSettings()
    usage_metrics_settings = UsageMetricsSettings()
    usage_pricing_settings = UsagePricingSettings()
    volume_metering_settings = VolumeMeteringSettings()
    tailnet_runtime_settings = TailnetRuntimeSettings()
    tailnet_control_settings = TailnetControlSettings()
    backend_route_settings = BackendRouteSettings()
    with ExitStack() as rollback:
        database = DatabaseClient.from_settings(
            DatabaseSettings(application_name=DatabaseApplicationName.Api)
        )
        rollback.callback(database.dispose)
        redis_client = RedisClient.from_settings(redis_settings)
        rollback.callback(redis_client.close)
        binary_redis_client = RedisClient.from_settings(
            redis_settings,
            decode_responses=False,
        )
        rollback.callback(binary_redis_client.close)
        tailnet_runtime = TailnetRuntime(tailnet_runtime_settings)
        rollback.callback(tailnet_runtime.close)
        object_store_client = S3ObjectStoreClient.from_settings(object_store_settings)
        rollback.callback(object_store_client.close)
        resolved_image_archive_settings = image_archive_settings.resolve(object_store_settings)
        image_archive_store = (
            S3ObjectStoreClient.from_settings(resolved_image_archive_settings.storage)
            if image_build_execution_settings.executor is ImageBuildExecutorKind.BuildContainer
            else None
        )
        if image_archive_store is not None:
            rollback.callback(image_archive_store.close)
        owned_resources: list[ApiOwnedResource] = [object_store_client]
        if image_archive_store is not None:
            owned_resources.append(image_archive_store)
        services = ApiServices.create(
            database,
            create_schema=False,
            tcp_ingress_settings=tcp_ingress_settings,
            agent_route_reconciliation_settings=agent_route_reconciliation_settings,
            gateway_settings=gateway_settings,
            workspace_change_stream_settings=workspace_change_stream_settings,
            agent_binary_settings=agent_binary_settings,
            aws_account_connection_settings=aws_account_connection_settings,
            aws_capacity_settings=aws_capacity_settings,
            aws_capacity_reconciliation_settings=aws_capacity_reconciliation_settings,
            tailnet_runtime_settings=tailnet_runtime_settings,
            tailnet_control_settings=tailnet_control_settings,
            backend_route_settings=backend_route_settings,
            object_store_settings=object_store_settings,
            object_store_client=object_store_client,
            image_archive_settings=image_archive_settings,
            image_archive_store=image_archive_store,
            image_build_execution_settings=image_build_execution_settings,
            image_build_registry_settings=image_build_registry_settings,
            image_build_container_settings=image_build_container_settings,
            container_service_settings=container_service_settings,
            retention_settings=retention_settings,
            usage_metrics_settings=usage_metrics_settings,
            usage_pricing_settings=usage_pricing_settings,
            volume_metering_settings=volume_metering_settings,
            redis_client=redis_client,
            binary_redis_client=binary_redis_client,
            owns_redis_client=True,
            owns_binary_redis_client=True,
            tailnet_peer_waiter=tailnet_runtime,
            tailnet_peer_resolver=tailnet_runtime,
            tailnet_runtime=tailnet_runtime,
            owned_resources=tuple(owned_resources),
        )
        try:
            _reconcile_bootstrap_capacity(services, capacity_bootstrap_settings)
        except BaseException as startup_error:
            rollback.pop_all()
            try:
                services.close()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "capacity bootstrap and service cleanup both failed",
                    [startup_error, cleanup_error],
                ) from None
            raise
        rollback.pop_all()
        return services


def _reconcile_bootstrap_capacity(
    services: ApiServices,
    settings: CapacityBootstrapSettings,
) -> None:
    for unit in settings.units:
        services.compute.create_unit(
            UnitName(unit.name),
            workspace=unit.workspace,
            pool=MachinePool(unit.pool) if unit.pool else None,
            provider=unit.provider,
            capacity_owner_id=unit.capacity_owner_id,
            initial_machines=unit.initial_machines,
            min_machines=unit.min_machines,
            max_machines=unit.max_machines,
            scaling_enabled=unit.scaling_enabled,
            default_eligible=unit.default_eligible,
            priority=unit.priority,
            min_free_cpu_millicores=unit.min_free_cpu_millicores,
            min_free_memory_mib=unit.min_free_memory_mib,
            min_free_gpu_count=unit.min_free_gpu_count,
            worker_cpu_millicores=unit.worker_cpu_millicores,
            worker_memory_mib=unit.worker_memory_mib,
            worker_gpu_type=unit.worker_gpu_type,
            worker_gpu_count=unit.worker_gpu_count,
            worker_runtimes=unit.worker_runtimes,
            worker_preemptible=unit.worker_preemptible,
            idle_drain_timeout_seconds=unit.idle_drain_timeout_seconds,
            scale_up_cooldown_seconds=unit.scale_up_cooldown_seconds,
            scale_down_cooldown_seconds=unit.scale_down_cooldown_seconds,
            registration_timeout_seconds=unit.registration_timeout_seconds,
            transport=unit.transport,
            fallback=unit.fallback,
        )
