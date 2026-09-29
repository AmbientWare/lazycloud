from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from threading import Lock

from control.release_settings import ReleaseSettings
from coordination.redis_client import RedisClient, RedisSettings
from execution.pods.service import PodControlService
from gateway.service import GatewayControlService
from identity.token_invalidation import AuthTokenInvalidation, configure_token_invalidation
from images.control import ImageControlService
from observability.settings import (
    TelemetrySettings,
)
from observability.telemetry import TelemetryConfig
from provider_clients.release import fetch_release_manifest, resolve_deployment_release
from shared.app_identity import CONTROL_PLANE_SERVICE_NAME
from shared.enums import StringEnum
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings

from api.server.async_io import ApiAsyncIo
from api.server.provider_compute import require_connected_aws_deployment_credentials
from api.server.services import (
    ApiProcessServices,
    ApiRoutes,
    ApiServiceCore,
    ApiServices,
    EndpointApiService,
    FunctionApiService,
    RuntimeServiceCore,
    compose_execution_routes,
    compose_management_routes,
    compose_runtime_routes,
    compose_task_routes,
    create_api_infrastructure,
    create_management_core,
    create_runtime_core,
    create_workload_core,
)
from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


class ControlPlaneRuntimeState(StringEnum):
    New = "new"
    Starting = "starting"
    Serving = "serving"
    Stopping = "stopping"
    Closed = "closed"


type ApiServicesFactory = Callable[[], ApiProcessServices]


@dataclass(slots=True)
class ControlPlaneRuntime:
    telemetry_config: TelemetryConfig = field(default_factory=TelemetryConfig)
    _services: ApiProcessServices | None = field(default=None, repr=False)
    _factory: ApiServicesFactory | None = field(default=None, repr=False)
    _owns_services: bool = field(default=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    state: ControlPlaneRuntimeState = field(default=ControlPlaneRuntimeState.New, init=False)

    @classmethod
    def production(cls) -> ControlPlaneRuntime:
        telemetry = TelemetrySettings()
        return cls(
            telemetry_config=telemetry.to_config(service_name=CONTROL_PLANE_SERVICE_NAME),
            _factory=production_management_services,
            _owns_services=True,
        )

    @classmethod
    def from_services(
        cls,
        services: ApiServices,
        *,
        endpoint_service: EndpointApiService | None = None,
        function_service: FunctionApiService | None = None,
        gateway_service: GatewayControlService | None = None,
        image_service: ImageControlService | None = None,
        pod_service: PodControlService | None = None,
    ) -> ControlPlaneRuntime:
        overrides = (
            endpoint_service,
            function_service,
            gateway_service,
            image_service,
            pod_service,
        )
        graph = (
            services.with_route_services(
                endpoint_service=endpoint_service,
                function_service=function_service,
                gateway_service=gateway_service,
                image_service=image_service,
                pod_service=pod_service,
            )
            if any(override is not None for override in overrides)
            else services
        )
        return cls(_services=ApiProcessServices(graph, graph), _owns_services=True)

    @property
    def services(self) -> ApiServiceCore:
        with self._lock:
            if self._services is None or self.state is not ControlPlaneRuntimeState.Serving:
                raise RuntimeError("control-plane runtime is not serving")
            return self._services.core

    @property
    def routes(self) -> ApiRoutes:
        with self._lock:
            if self._services is None or self.state is not ControlPlaneRuntimeState.Serving:
                raise RuntimeError("API runtime is not serving")
            return self._services.routes

    def start(self) -> ApiServiceCore:
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
                    AuthTokenInvalidation.from_redis(self._services.core.redis_client)
                )
            except BaseException as startup_error:
                cleanup_error: BaseException | None = None
                try:
                    if self._owns_services and self._services is not None:
                        self._services.core.close()
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
            return self._services.core

    def stop(self) -> None:
        with self._lock:
            if self.state is ControlPlaneRuntimeState.Closed:
                return
            self.state = ControlPlaneRuntimeState.Stopping
            services = self._services
            try:
                if self._owns_services and services is not None:
                    services.core.close()
            finally:
                configure_token_invalidation(None)
                self._services = None
                self.state = ControlPlaneRuntimeState.Closed


def _production_workload(
    application_name: DatabaseApplicationName, *, client_release_version: str | None
) -> ApiServiceCore:
    redis_settings = RedisSettings()
    object_store_settings = S3ObjectStoreSettings()
    with ExitStack() as rollback:
        database_settings = DatabaseSettings(application_name=application_name)
        database = DatabaseClient.from_settings(database_settings)
        rollback.callback(database.dispose)
        redis_client = RedisClient.from_settings(redis_settings)
        rollback.callback(redis_client.close)
        binary_redis_client = RedisClient.from_settings(redis_settings, decode_responses=False)
        rollback.callback(binary_redis_client.close)
        object_store_client = S3ObjectStoreClient.from_settings(object_store_settings)
        rollback.callback(object_store_client.close)
        infrastructure = create_api_infrastructure(
            database,
            client_release_version=client_release_version,
            object_store_settings=object_store_settings,
            object_store_client=object_store_client,
            redis_client=redis_client,
            binary_redis_client=binary_redis_client,
            async_io=ApiAsyncIo.from_settings(database_settings, redis_settings),
            owns_redis_client=True,
            owns_binary_redis_client=True,
            owned_resources=(object_store_client,),
        )
        rollback.pop_all()
    try:
        return create_workload_core(infrastructure)
    except BaseException:
        infrastructure.close()
        raise


def _production_runtime(application_name: DatabaseApplicationName) -> RuntimeServiceCore:
    release = resolve_deployment_release()
    require_connected_aws_deployment_credentials(release.aws_connections)
    core = _production_workload(application_name, client_release_version=release.version or None)
    try:
        return create_runtime_core(
            core,
            agent_binary_settings=release.agent_binaries,
            aws_account_connection_settings=release.aws_connections,
            aws_capacity_settings=release.aws_capacity,
        )
    except BaseException:
        core.close()
        raise


def production_management_services() -> ApiProcessServices:
    runtime = _production_runtime(DatabaseApplicationName.Api)
    try:
        core = create_management_core(runtime)
        return ApiProcessServices(core, compose_management_routes(core, compose_task_routes(core)))
    except BaseException:
        runtime.close()
        raise


def production_execution_services() -> ApiProcessServices:
    release = ReleaseSettings()
    version = (
        fetch_release_manifest(
            release.manifest_url, timeout_seconds=release.fetch_timeout_seconds
        ).release_version
        if release.manifest_url
        else None
    )
    core = _production_workload(
        DatabaseApplicationName.ExecutionApi, client_release_version=version
    )
    try:
        return ApiProcessServices(core, compose_execution_routes(core, compose_task_routes(core)))
    except BaseException:
        core.close()
        raise


def production_runtime_services() -> ApiProcessServices:
    core = _production_runtime(DatabaseApplicationName.RuntimeApi)
    try:
        return ApiProcessServices(core, compose_runtime_routes(core, compose_task_routes(core)))
    except BaseException:
        core.close()
        raise
