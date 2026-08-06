from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from uuid import uuid4

import uvicorn
from compute.aws_connections import AwsAccountConnectionService
from execution.artifacts.service import ArtifactStorageService
from execution.collections.redis import RedisMapService, RedisSimpleQueueService
from execution.pods.service import PodControlService
from execution.shells.service import ShellControlService
from execution.signals.redis import RedisSignalService
from execution.volumes.control import VolumeControlService
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from gateway.events import (
    GatewayEventSink,
    GatewayRequestEventMiddleware,
    authorization_header_from_scope,
)
from gateway.service import GatewayControlService
from identity.auth import AuthError, AuthorizationDeniedError
from images.control import ImageControlService
from networking.control_plane_origin import (
    DEFAULT_CONTROL_PLANE_ORIGIN_TTL_SECONDS as CONTROL_PLANE_ORIGIN_TTL_SECONDS,
)
from networking.control_plane_origin import (
    RedisControlPlaneOriginRepository,
    runtime_origin_for_host,
)
from observability.telemetry import setup_telemetry
from pydantic import JsonValue
from shared.app_identity import DISPLAY_NAME
from shared.errors import (
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from shared.events import Event, EventLevel
from shared.http.errors import ErrorResponse
from starlette.types import Scope

from api.control_runtime import ControlPlaneRuntime
from api.server import include_api_routers, service_dependencies
from api.server.host_routing import GeneratedInvokeHostRoutingMiddleware
from api.server.rate_limit import UnauthenticatedRateLimitMiddleware
from api.server.services import (
    ApiServices,
    EndpointApiService,
    FunctionApiService,
    TaskQueueApiService,
)
from api.server.tcp_ingress import tcp_ingress_server_from_settings
from api.server.worker_repository_service import WorkerRepositoryService
from api.settings import PublicIngressSettings
from api.web_static import mount_web_app
from database import ControlPlaneRecoveryFence

logger = logging.getLogger(__name__)

_DOMAIN_ERROR_STATUS: dict[type[DomainError], int] = {
    DomainError: status.HTTP_400_BAD_REQUEST,
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    InvalidInputError: status.HTTP_400_BAD_REQUEST,
    UpstreamUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _domain_error_status(exc: DomainError) -> int:
    for error_type in type(exc).__mro__:
        if issubclass(error_type, DomainError) and error_type in _DOMAIN_ERROR_STATUS:
            return _DOMAIN_ERROR_STATUS[error_type]
    return status.HTTP_400_BAD_REQUEST


def create_app(
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
) -> FastAPI:
    runtime = ControlPlaneRuntime.from_services(
        services,
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
    return _create_app(runtime)


def _create_app(runtime: ControlPlaneRuntime) -> FastAPI:
    cleanup_failures: list[Exception] = []

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI) -> AsyncIterator[None]:
        cleanup_failures.clear()
        try:
            async with AsyncExitStack() as cleanup:
                telemetry = setup_telemetry(runtime.telemetry_config)
                cleanup.callback(
                    _capture_cleanup_failure,
                    cleanup_failures,
                    telemetry.shutdown,
                )
                cleanup.callback(
                    _capture_cleanup_failure,
                    cleanup_failures,
                    runtime.stop,
                )
                api_services = runtime.start()
                cleanup.callback(
                    _unpublish_api_services,
                    lifespan_app,
                    api_services,
                )
                route_repository = service_dependencies.worker_repository_service(api_services)
                tcp_ingress = tcp_ingress_server_from_settings(
                    api_services,
                    service_dependencies.pod_service(api_services),
                    api_services.tcp_ingress_settings,
                )
                recovery_fence = ControlPlaneRecoveryFence(api_services.context.database)
                cleanup.callback(
                    _capture_cleanup_failure,
                    cleanup_failures,
                    recovery_fence.stop_serving,
                )
                recovery_fence.start_serving()
                if api_services.tailnet_runtime is not None:
                    # Fatal rather than logged: the control plane reaches every
                    # agent over the tailnet, so one that comes up without it
                    # serves nothing and reports healthy while doing it. A
                    # deployment that wants no tailnet says so with the disabled
                    # mode, which never reaches here.
                    api_services.tailnet_runtime.start()
                # Published before anything is served, and before this process
                # reports healthy, so nothing that waits on health can observe an
                # unpublished origin.
                _publish_runtime_origin(api_services)
                origin_heartbeat = _create_background_task(
                    _republish_runtime_origin(
                        api_services,
                        event_sink=api_services.events,
                    )
                )
                cleanup.push_async_callback(
                    _capture_task_cleanup_failure,
                    cleanup_failures,
                    origin_heartbeat,
                )
                if tcp_ingress is not None:
                    cleanup.push_async_callback(
                        _capture_async_cleanup_failure,
                        cleanup_failures,
                        tcp_ingress.close,
                    )
                    await tcp_ingress.start()
                route_reconciliation = _create_background_task(
                    _reconcile_agent_routes(
                        route_repository,
                        interval_seconds=(
                            api_services.agent_route_reconciliation_settings.interval_seconds
                        ),
                        event_sink=api_services.events,
                    )
                )
                cleanup.push_async_callback(
                    _capture_task_cleanup_failure,
                    cleanup_failures,
                    route_reconciliation,
                )
                if api_services.aws_connections is not None:
                    reconciliation = api_services.aws_capacity_reconciliation_settings
                    aws_connection_reconciliation = _create_background_task(
                        _reconcile_aws_connections(
                            api_services.aws_connections,
                            interval_seconds=reconciliation.interval_seconds,
                            limit=reconciliation.limit,
                            event_sink=api_services.events,
                        )
                    )
                    cleanup.push_async_callback(
                        _capture_task_cleanup_failure,
                        cleanup_failures,
                        aws_connection_reconciliation,
                    )
                _publish_api_services(lifespan_app, api_services)
                yield
        except BaseException as exc:
            if cleanup_failures:
                raise BaseExceptionGroup(
                    "control-plane lifespan and cleanup failed",
                    [exc, *cleanup_failures],
                ) from None
            raise
        if cleanup_failures:
            raise ExceptionGroup(
                "control-plane shutdown was incomplete",
                cleanup_failures.copy(),
            )

    app = FastAPI(
        title=f"{DISPLAY_NAME} Control Plane",
        version="0.1.0",
        summary="Typed HTTP control plane for remote execution workflows.",
        lifespan=lifespan,
    )
    services_provider = _FastApiServicesProvider(app)
    public_ingress = PublicIngressSettings()
    app.add_middleware(
        GeneratedInvokeHostRoutingMiddleware,
        services_provider=services_provider,
    )
    app.add_middleware(
        UnauthenticatedRateLimitMiddleware,
        redis=lambda: services_provider.current().redis(),
        client_ip_header=public_ingress.client_ip_header,
    )
    app.add_middleware(
        GatewayRequestEventMiddleware,
        event_sink=_CurrentGatewayEventSink(services_provider),
        workspace_resolver=_CurrentWorkspaceResolver(services_provider),
        metrics_sink=_CurrentGatewayMetricsSink(services_provider),
    )

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        # A domain error carries one sentence. Almost every one is raised `from`
        # something that says what actually happened, and that chain ended here
        # unread — so the cheapest failures to explain were the ones this
        # codebase explained least. Expected does not mean uninteresting.
        status_code = _domain_error_status(exc)
        request_id = request.headers.get("x-request-id") or uuid4().hex
        log = logger.warning if status_code < 500 else logger.error
        log(
            "%s serving %s %s (request_id=%s)",
            type(exc).__name__,
            request.method,
            request.url.path,
            request_id,
            exc_info=exc,
        )
        return JSONResponse(
            ErrorResponse(detail=exc.message, code=exc.code).model_dump(),
            status_code=status_code,
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(AuthError)
    async def auth_error_handler(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(
            ErrorResponse(detail=str(exc)).model_dump(),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    @app.exception_handler(AuthorizationDeniedError)
    async def authorization_denied_handler(
        _: Request, exc: AuthorizationDeniedError
    ) -> JSONResponse:
        return JSONResponse(
            ErrorResponse(detail=str(exc)).model_dump(),
            status_code=status.HTTP_403_FORBIDDEN,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = request.headers.get("x-request-id") or uuid4().hex
        logger.exception(
            "unhandled error serving %s %s (request_id=%s)",
            request.method,
            request.url.path,
            request_id,
            exc_info=exc,
        )
        return JSONResponse(
            ErrorResponse(detail=f"internal server error (request id: {request_id})").model_dump(),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            headers={"X-Request-ID": request_id},
        )

    include_api_routers(app)
    mount_web_app(app)
    return app


def create_production_app() -> FastAPI:
    runtime = ControlPlaneRuntime.production()
    return _create_app(runtime)


def _emit_reconciliation_failure(
    event_sink: GatewayEventSink | None,
    loop_name: str,
    exc: Exception,
) -> None:
    """A failed reconcile pass must outlive the log line that mentions it."""
    if event_sink is None:
        return
    with suppress(Exception):
        event_sink.emit(
            f"reconciliation.{loop_name}.failed",
            resource_type="reconciliation-loop",
            resource_id=loop_name,
            message=f"{loop_name} reconciliation failed ({type(exc).__name__}: {exc})",
            level=EventLevel.Error,
            data={"loop": loop_name, "error_type": type(exc).__name__},
        )


def _publish_runtime_origin(api_services: ApiServices) -> str:
    """Record where this control plane is actually reachable.

    The host comes from the tailnet device rather than from configuration,
    because the device name is granted, not chosen: a collision resolves to a
    suffixed name that no deployment file predicts.
    """
    tailnet_runtime = api_services.tailnet_runtime
    host = tailnet_runtime.self_dns_name() if tailnet_runtime is not None else ""
    origin = runtime_origin_for_host(
        api_services.gateway_settings.runtime_callback_http_url,
        host,
    )
    RedisControlPlaneOriginRepository(api_services.redis_client).publish(
        origin,
        ttl_seconds=CONTROL_PLANE_ORIGIN_TTL_SECONDS,
    )
    logger.info("published control-plane runtime origin: %s", origin)
    return origin


async def _republish_runtime_origin(
    api_services: ApiServices,
    *,
    event_sink: GatewayEventSink | None = None,
) -> None:
    """Keep the published origin fresh, and current if the device is renamed.

    The TTL is what makes a stopped control plane stop advertising an address it
    no longer answers on, so this has to outpace it rather than run beside it.
    """
    interval = max(CONTROL_PLANE_ORIGIN_TTL_SECONDS / 3, 1.0)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_publish_runtime_origin, api_services)
        except Exception as exc:
            logger.exception("publishing the control-plane runtime origin failed")
            _emit_reconciliation_failure(event_sink, "control-plane-origin", exc)


async def _reconcile_agent_routes(
    repository: WorkerRepositoryService,
    *,
    interval_seconds: float,
    event_sink: GatewayEventSink | None = None,
) -> None:
    while True:
        try:
            result = await asyncio.to_thread(repository.reconcile_orphan_agent_routes)
            if result.removed:
                logger.info(
                    "reconciled agent route registry: scanned=%s removed=%s",
                    result.scanned,
                    result.removed,
                )
        except Exception as exc:
            logger.exception("agent route registry reconciliation failed")
            _emit_reconciliation_failure(event_sink, "agent-routes", exc)
        await asyncio.sleep(max(interval_seconds, 0.1))


async def _reconcile_aws_connections(
    service: AwsAccountConnectionService,
    *,
    interval_seconds: float,
    limit: int,
    event_sink: GatewayEventSink | None = None,
) -> None:
    while True:
        try:
            batch = await asyncio.to_thread(service.reconcile_due, limit=limit)
            if batch.failure_count:
                logger.warning(
                    "AWS connection reconciliation remains incomplete: "
                    "processed=%s completed=%s failures=%s",
                    batch.processed_count,
                    batch.completed_count,
                    batch.failure_count,
                )
        except Exception as exc:
            logger.exception("AWS connection reconciliation failed")
            _emit_reconciliation_failure(event_sink, "aws-connections", exc)
        await asyncio.sleep(max(interval_seconds, 0.1))


@dataclass(frozen=True, slots=True)
class _FastApiServicesProvider:
    app: FastAPI

    def current(self) -> ApiServices:
        services = getattr(self.app.state, "api_services", None)
        if not isinstance(services, ApiServices):
            raise RuntimeError("FastAPI app is not serving API services")
        return services


@dataclass(frozen=True, slots=True)
class _CurrentGatewayEventSink:
    services_provider: _FastApiServicesProvider

    def emit(
        self,
        action: str,
        *,
        resource_type: str,
        resource_id: str,
        message: str,
        level: EventLevel = EventLevel.Info,
        data: dict[str, JsonValue] | None = None,
        workspace_id: str | None = None,
    ) -> Event:
        return self.services_provider.current().events.emit(
            action,
            resource_type=resource_type,
            resource_id=resource_id,
            message=message,
            level=level,
            data=data,
            workspace_id=workspace_id,
        )


@dataclass(frozen=True, slots=True)
class _CurrentGatewayMetricsSink:
    services_provider: _FastApiServicesProvider

    def increment(
        self,
        name: str,
        amount: float = 1,
        *,
        labels: dict[str, str] | None = None,
    ) -> object:
        return self.services_provider.current().metrics.increment(name, amount, labels=labels)

    def observe_histogram(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> object:
        return self.services_provider.current().metrics.observe_histogram(
            name, value, labels=labels
        )


@dataclass(frozen=True, slots=True)
class _CurrentWorkspaceResolver:
    services_provider: _FastApiServicesProvider

    def __call__(self, scope: Scope) -> str:
        services = self.services_provider.current()
        authorization = authorization_header_from_scope(scope)
        if not authorization:
            return ""
        try:
            token = services.auth.authenticate_header(
                authorization,
                allow_if_no_tokens=False,
            )
        except AuthError:
            return ""
        return token.workspace_id if token is not None else ""


def _capture_cleanup_failure(
    failures: list[Exception],
    cleanup: Callable[[], None],
) -> None:
    try:
        cleanup()
    except Exception as exc:
        failures.append(exc)


async def _capture_async_cleanup_failure(
    failures: list[Exception],
    cleanup: Callable[[], Awaitable[None]],
) -> None:
    try:
        await cleanup()
    except Exception as exc:
        failures.append(exc)


async def _capture_task_cleanup_failure(
    failures: list[Exception],
    task: asyncio.Task[None],
) -> None:
    try:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    except Exception as exc:
        failures.append(exc)


def _create_background_task(
    coroutine: Coroutine[None, None, None],
) -> asyncio.Task[None]:
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


def _publish_api_services(app: FastAPI, services: ApiServices) -> None:
    app.state.api_services = services


def _unpublish_api_services(app: FastAPI, services: ApiServices) -> None:
    if getattr(app.state, "api_services", None) is services:
        del app.state.api_services


def main() -> None:
    uvicorn.run(
        "api.fastapi_app:create_production_app",
        factory=True,
        host="127.0.0.1",
        port=9000,
    )
