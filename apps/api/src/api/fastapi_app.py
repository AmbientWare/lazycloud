from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from uuid import uuid4

import uvicorn
from anyio.to_thread import current_default_thread_limiter
from compute.aws_connections import AwsAccountConnectionService
from compute.telemetry import AGENT_INTAKE_PRESENCE_ROLE
from coordination.process_presence import AsyncRedisProcessPresence, presence_refresh_interval
from coordination.redis_client import AsyncRedisClient, RedisPoolStatus
from coordination.stream_tail import RedisStreamTailStatus
from coordination.token_lock import renew_token_lock_async, try_acquire_token_lock_async
from execution.artifacts.service import ArtifactStorageService
from execution.collections.redis import RedisMapService, RedisSimpleQueueService
from execution.pods.service import PodControlService
from execution.shells.service import ShellControlService
from execution.signals.redis import RedisSignalService
from execution.volumes.control import VolumeControlService
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from gateway.events import (
    AsyncGatewayEventSink,
    GatewayRequestEventMiddleware,
    authorization_header_from_scope,
)
from gateway.service import GatewayControlService
from identity.auth import AuthError, AuthorizationDeniedError
from images.control import ImageControlService
from observability.telemetry import setup_telemetry
from pydantic import JsonValue
from shared.app_identity import DISPLAY_NAME
from shared.errors import (
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    PaymentRequiredError,
    UpstreamUnavailableError,
)
from shared.events import Event, EventLevel
from shared.http.errors import ErrorResponse
from shared.timestamps import utc_now
from starlette.types import Scope

from api.control_runtime import ControlPlaneRuntime
from api.server import include_api_routers, service_dependencies
from api.server.async_io import ApiAsyncIo
from api.server.client_version import ClientVersionMiddleware
from api.server.host_routing import GeneratedInvokeHostRoutingMiddleware
from api.server.public_transfers import PublicTransferMiddleware
from api.server.rate_limit import UnauthenticatedRateLimitMiddleware
from api.server.services import (
    ApiServices,
    EndpointApiService,
    FunctionApiService,
)
from api.server.tcp_ingress import tcp_ingress_server_from_settings
from api.server.worker_repository_service import WorkerRepositoryService
from api.settings import PublicIngressSettings
from api.web_static import mount_web_app
from database import AsyncControlPlaneRecoveryFence, DatabasePoolStatus

logger = logging.getLogger(__name__)

_RUNTIME_PRESSURE_INTERVAL_SECONDS = 1.0

_DOMAIN_ERROR_STATUS: dict[type[DomainError], int] = {
    DomainError: status.HTTP_400_BAD_REQUEST,
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    InvalidInputError: status.HTTP_400_BAD_REQUEST,
    UpstreamUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    PaymentRequiredError: status.HTTP_402_PAYMENT_REQUIRED,
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

                def on_close(release: Callable[[], Awaitable[None]]) -> None:
                    cleanup.push_async_callback(_capture_cleanup_failure, cleanup_failures, release)

                def spawn(coroutine: Coroutine[None, None, None]) -> None:
                    on_close(partial(_cancel_task, _create_background_task(coroutine)))

                telemetry = await asyncio.to_thread(setup_telemetry, runtime.telemetry_config)
                on_close(partial(asyncio.to_thread, telemetry.shutdown))
                on_close(partial(asyncio.to_thread, runtime.stop))
                api_services = await asyncio.to_thread(runtime.start)
                async_io = api_services.require_async_io()
                on_close(async_io.close)
                await async_io.start()
                spawn(_observe_runtime_pressure(api_services, async_io))
                cleanup.callback(_unpublish_api_services, lifespan_app, api_services)
                await api_services.workspace_compute_policy_service.reconcile_capacity_at_startup(
                    async_io.database
                )
                route_repository = service_dependencies.worker_repository_service(api_services)
                tcp_ingress = await tcp_ingress_server_from_settings(
                    api_services,
                    service_dependencies.pod_service(api_services),
                    api_services.tcp_ingress_settings,
                )
                recovery_fence = AsyncControlPlaneRecoveryFence(async_io.database)
                on_close(recovery_fence.stop_serving)
                await recovery_fence.start_serving()
                if tcp_ingress is not None:
                    on_close(tcp_ingress.close)
                    await tcp_ingress.start()
                spawn(
                    _reconcile_agent_routes(
                        route_repository,
                        async_io,
                        interval_seconds=(
                            api_services.agent_route_reconciliation_settings.interval_seconds
                        ),
                        event_sink=api_services.events,
                    )
                )
                spawn(_publish_agent_intake_presence(async_io.redis, utc_now()))
                spawn(
                    _reconcile_agent_disconnects(
                        api_services.gateway_service,
                        async_io,
                        interval_seconds=(
                            api_services.agent_disconnect_reconciliation_settings.interval_seconds
                        ),
                        event_sink=api_services.events,
                    )
                )
                if api_services.aws_connections is not None:
                    reconciliation = api_services.aws_capacity_reconciliation_settings
                    spawn(
                        _reconcile_aws_connections(
                            api_services.aws_connections,
                            interval_seconds=reconciliation.interval_seconds,
                            limit=reconciliation.limit,
                            event_sink=api_services.events,
                        )
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
        PublicTransferMiddleware,
        usage=lambda: services_provider.current().usage,
        client_ip_header=public_ingress.client_ip_header,
    )
    app.add_middleware(
        UnauthenticatedRateLimitMiddleware,
        redis=lambda: services_provider.current().require_async_io().redis,
        client_ip_header=public_ingress.client_ip_header,
    )
    app.add_middleware(
        GatewayRequestEventMiddleware,
        event_sink=_CurrentGatewayEventSink(services_provider),
        workspace_resolver=_CurrentWorkspaceResolver(services_provider),
        metrics_sink=_CurrentGatewayMetricsSink(services_provider),
    )
    app.add_middleware(
        ClientVersionMiddleware,
        recommended_version=lambda: services_provider.current().client_release_version,
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        issues: list[str] = []
        for error in exc.errors():
            location = error["loc"]
            if error["type"] == "extra_forbidden":
                location = location[:-1]
            path = ".".join(str(part) for part in location) or "request"
            issues.append(f"{path}: {error['type']}")
        return JSONResponse(
            ErrorResponse(detail="; ".join(issues), code="invalid_input").model_dump(),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        status_code = _domain_error_status(exc)
        request_id = request.headers.get("x-request-id") or uuid4().hex
        log = logger.warning if status_code < 500 else logger.error
        log(
            "%s serving %s %s (request_id=%s): %s",
            type(exc).__name__,
            request.method,
            request.url.path,
            request_id,
            exc.message,
            exc_info=exc if status_code >= 500 else None,
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


async def _emit_reconciliation_failure(
    event_sink: AsyncGatewayEventSink | None,
    loop_name: str,
    exc: Exception,
) -> None:
    """A failed reconcile pass must outlive the log line that mentions it."""
    if event_sink is None:
        return
    with suppress(Exception):
        await event_sink.emit_async(
            f"reconciliation.{loop_name}.failed",
            resource_type="reconciliation-loop",
            resource_id=loop_name,
            message=f"{loop_name} reconciliation failed ({type(exc).__name__}: {exc})",
            level=EventLevel.Error,
            data={"loop": loop_name, "error_type": type(exc).__name__},
        )


async def _reconcile_agent_routes(
    repository: WorkerRepositoryService,
    async_io: ApiAsyncIo,
    *,
    interval_seconds: float,
    event_sink: AsyncGatewayEventSink | None = None,
) -> None:
    """Remove routes whose backend is gone, from one control plane at a time.

    This scans a shared registry and deletes from it, so running it in every
    control plane would have them racing to delete each other's findings. The
    lease is renewed rather than released, so the winner keeps the work while it
    is alive and another takes over only once its lease expires unrenewed.
    """
    redis = async_io.redis
    lease_key = redis.key("control-plane", "leases", "agent-routes")
    holder = str(uuid4())
    lease_seconds = max(int(interval_seconds * 3), 2)
    holding = False
    while True:
        try:
            # Renewing is not the same call as acquiring. Acquisition is `nx`, so
            # a holder asking for its own live lease is refused and would hand
            # the work to nobody every other cycle.
            holding = holding and await renew_token_lock_async(
                redis, lease_key, holder, ttl_seconds=lease_seconds
            )
            if not holding:
                holding = await try_acquire_token_lock_async(
                    redis, lease_key, holder, ttl_seconds=lease_seconds
                )
            if holding:
                result = await repository.reconcile_orphan_agent_routes(async_io)
                if result.removed:
                    logger.info(
                        "reconciled agent route registry: scanned=%s removed=%s",
                        result.scanned,
                        result.removed,
                    )
        except Exception as exc:
            holding = False
            logger.exception("agent route registry reconciliation failed")
            await _emit_reconciliation_failure(event_sink, "agent-routes", exc)
        await asyncio.sleep(max(interval_seconds, 0.1))


async def _publish_agent_intake_presence(
    redis: AsyncRedisClient,
    started_at: datetime,
) -> None:
    """Say that this process is receiving agent heartbeats, and keep saying it.

    The reclaim terminates machines for going silent, and silence proves nothing
    about a machine while nothing was listening to it. That fact belongs to
    whoever serves the agent stream, and it cannot be inferred by the scheduler:
    a scheduler up for a week has no way to know this process restarted a minute
    ago and took every heartbeat with it.

    Not leader-elected. Every replica that takes agent traffic publishes its own
    start, because the question readers ask is whether *some* intake was up.
    """

    presence = AsyncRedisProcessPresence(redis, AGENT_INTAKE_PRESENCE_ROLE)
    interval_seconds = presence_refresh_interval(presence.ttl_seconds)
    try:
        while True:
            try:
                await presence.publish(started_at)
            except Exception:
                # Reported and retried: the key carries a TTL, so a failed
                # refresh is a countdown rather than a disappearance, and the
                # reclaim declines while it is absent instead of acting.
                logger.exception("agent intake presence refresh failed")
            await asyncio.sleep(interval_seconds)
    finally:
        with suppress(Exception):
            await presence.withdraw()


async def _reconcile_agent_disconnects(
    gateway: GatewayControlService,
    async_io: ApiAsyncIo,
    *,
    interval_seconds: float,
    event_sink: AsyncGatewayEventSink | None = None,
) -> None:
    """Write off machines that stopped reporting, from one control plane at a time.

    Every enrollment write on the live path happens because a heartbeat
    arrived, which is the one thing a machine that has gone will not do. This is
    the writer for its absence.

    The lease keeps the fleet from being scanned by every control plane at once;
    it is not what makes the write safe, because each machine is re-decided
    under its own row lock. Renewed rather than released, so the winner keeps
    the work while it is alive.
    """
    redis = async_io.redis
    lease_key = redis.key("control-plane", "leases", "agent-disconnects")
    holder = str(uuid4())
    lease_seconds = max(int(interval_seconds * 3), 2)
    holding = False
    while True:
        try:
            holding = holding and await renew_token_lock_async(
                redis, lease_key, holder, ttl_seconds=lease_seconds
            )
            if not holding:
                holding = await try_acquire_token_lock_async(
                    redis, lease_key, holder, ttl_seconds=lease_seconds
                )
            if holding:
                marked = await gateway.sweep_disconnected_agents(
                    async_io.database,
                    async_io.redis,
                )
                if marked:
                    logger.info(
                        "marked agent machines disconnected: %s",
                        ", ".join(marked),
                    )
        except Exception as exc:
            holding = False
            logger.exception("agent disconnect reconciliation failed")
            await _emit_reconciliation_failure(event_sink, "agent-disconnects", exc)
        await asyncio.sleep(max(interval_seconds, 0.1))


async def _reconcile_aws_connections(
    service: AwsAccountConnectionService,
    *,
    interval_seconds: float,
    limit: int,
    event_sink: AsyncGatewayEventSink | None = None,
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
            await _emit_reconciliation_failure(event_sink, "aws-connections", exc)
        await asyncio.sleep(max(interval_seconds, 0.1))


async def _observe_runtime_pressure(services: ApiServices, async_io: ApiAsyncIo) -> None:
    loop = asyncio.get_running_loop()
    sample_at = loop.time() + _RUNTIME_PRESSURE_INTERVAL_SECONDS
    while True:
        await asyncio.sleep(max(sample_at - loop.time(), 0))
        observed_at = loop.time()
        lag_seconds = max(observed_at - sample_at, 0)
        try:
            _record_runtime_pressure(services, async_io, lag_seconds)
        except Exception:
            logger.exception("API runtime pressure metrics were not recorded")
        sample_at = observed_at + _RUNTIME_PRESSURE_INTERVAL_SECONDS


def _record_runtime_pressure(
    services: ApiServices,
    async_io: ApiAsyncIo,
    lag_seconds: float,
) -> None:
    services.metrics.set_gauge("api_event_loop_lag_seconds", lag_seconds)
    services.metrics.observe_histogram("api_event_loop_lag_seconds_histogram", lag_seconds)

    thread_pool = current_default_thread_limiter().statistics()
    capacity = float(thread_pool.total_tokens)
    services.metrics.set_gauge(
        "api_threadpool_threads",
        thread_pool.borrowed_tokens,
        labels={"state": "borrowed"},
    )
    services.metrics.set_gauge(
        "api_threadpool_threads",
        capacity,
        labels={"state": "capacity"},
    )
    services.metrics.set_gauge(
        "api_threadpool_waiting_tasks",
        thread_pool.tasks_waiting,
    )
    services.metrics.set_gauge(
        "api_threadpool_pressure_ratio",
        thread_pool.borrowed_tokens / capacity if capacity > 0 else 1,
    )

    _record_database_pool_metrics(
        services,
        client="sync",
        status=services.context.database.pool_status(),
    )
    _record_database_pool_metrics(
        services,
        client="async",
        status=async_io.database.pool_status(),
    )
    _record_redis_pool_metrics(
        services,
        client="sync-text",
        status=services.redis().pool_status(),
    )
    _record_redis_pool_metrics(
        services,
        client="sync-binary",
        status=services.binary_redis().pool_status(),
    )
    _record_redis_pool_metrics(
        services,
        client="async-text",
        status=async_io.redis.pool_status(),
    )
    _record_redis_pool_metrics(
        services,
        client="async-binary",
        status=async_io.binary_redis.pool_status(),
    )
    _record_realtime_metrics(services, async_io.realtime.status())


def _record_realtime_metrics(services: ApiServices, status: RedisStreamTailStatus) -> None:
    services.metrics.set_gauge("api_realtime_stream_sources", status.sources)
    for kind, count in status.subscribers.items():
        services.metrics.set_gauge(
            "api_realtime_stream_subscribers",
            count,
            labels={"kind": kind},
        )
    for kind, count in status.overflows.items():
        services.metrics.set_gauge(
            "api_realtime_stream_overflows_total",
            count,
            labels={"kind": kind},
        )
    services.metrics.set_gauge("api_realtime_stream_reader_failures_total", status.reader_failures)
    services.metrics.set_gauge("api_realtime_stream_healthy", float(status.healthy))


def _record_database_pool_metrics(
    services: ApiServices,
    *,
    client: str,
    status: DatabasePoolStatus | None,
) -> None:
    if status is None:
        return
    labels = {"backend": "database", "client": client}
    for state, value in (
        ("in_use", status.checked_out),
        ("available", status.available),
        ("capacity", status.capacity),
    ):
        services.metrics.set_gauge(
            "api_io_pool_connections",
            value,
            labels=labels | {"state": state},
        )
    services.metrics.set_gauge(
        "api_io_pool_exhausted",
        float(status.exhausted),
        labels=labels,
    )
    services.metrics.set_gauge(
        "api_io_pool_exhaustions_total",
        status.exhaustions_total,
        labels=labels,
    )


def _record_redis_pool_metrics(
    services: ApiServices,
    *,
    client: str,
    status: RedisPoolStatus | None,
) -> None:
    if status is None:
        return
    labels = {"backend": "redis", "client": client}
    for state, value in (
        ("in_use", status.in_use),
        ("idle", status.idle),
        ("capacity", status.capacity),
    ):
        services.metrics.set_gauge(
            "api_io_pool_connections",
            value,
            labels=labels | {"state": state},
        )
    services.metrics.set_gauge(
        "api_io_pool_exhausted",
        float(status.exhausted),
        labels=labels,
    )
    services.metrics.set_gauge(
        "api_io_pool_exhaustions_total",
        status.exhaustions_total,
        labels=labels,
    )


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

    async def emit_async(
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
        return await self.services_provider.current().events.emit_async(
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

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> object:
        return self.services_provider.current().metrics.set_gauge(name, value, labels=labels)


@dataclass(frozen=True, slots=True)
class _CurrentWorkspaceResolver:
    services_provider: _FastApiServicesProvider

    async def __call__(self, scope: Scope) -> str:
        services = self.services_provider.current()
        authorization = authorization_header_from_scope(scope)
        if not authorization:
            return ""
        try:
            async_io = services.require_async_io()
            token = await services.auth.authenticate_header_async(
                async_io.database,
                async_io.auth_invalidation,
                authorization,
            )
        except AuthError:
            return ""
        return token.workspace_id


async def _capture_cleanup_failure(
    failures: list[Exception],
    cleanup: Callable[[], Awaitable[None]],
) -> None:
    try:
        await cleanup()
    except Exception as exc:
        failures.append(exc)


async def _cancel_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


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
        timeout_graceful_shutdown=20,
    )
