from __future__ import annotations

import asyncio
import json
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from control.service import ControlPlaneService, StubKind, StubRecord
from database.records.endpoint_dispatch import (
    EndpointDispatchStateRecord,
)
from database.repositories.apps import StubRepository
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.endpoint_dispatch import EndpointDispatchRepository
from database.repositories.orchestration import ContainerRepository
from database.repositories.previews import PreviewSessionRepository
from database.types import DatabaseSession
from foundation.ids import try_uuid
from pydantic import JsonValue
from shared.app_identity import ENDPOINT_IMAGE
from shared.container_requests import (
    CONTAINER_INNER_PORT,
    WORKER_USER_CODE_VOLUME,
    WorkerStartupKind,
)
from shared.containers import ContainerStatus
from shared.env import (
    APP_ID_ENV,
    CHECKPOINT_ENABLED_ENV,
    ENDPOINT_WORKERS_ENV,
    HOT_RELOAD_DIR_ENV,
    HOT_RELOAD_ENV,
    LIFECYCLE_HOOKS_ENV,
    STUB_ID_ENV,
    STUB_TYPE_ENV,
)
from shared.errors import (
    CapacityLimitReachedError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    PaymentRequiredError,
    UpstreamUnavailableError,
)
from shared.events import EventLevel
from shared.http.endpoint_forwarding import HeaderMap, error_response
from shared.http.endpoints import (
    EndpointForwardRequest,
    EndpointForwardResponse,
    EndpointWarmupRequest,
    EndpointWarmupResponse,
)
from shared.http.previews import CreatePreviewRequest, PreviewSessionResponse, PreviewSessionStatus
from shared.http.task_payload import serialize_http_task_payload
from shared.http.workspace_changes import WorkspaceChangeType
from shared.tasks import Task, TaskStatus
from shared.timestamps import utc_now

from database import AsyncDatabaseClient
from execution.checkpoints import latest_available_checkpoint
from execution.containers.planning import ContainerSchedulingOptions
from execution.containers.service import PendingContainerReservation
from execution.endpoints.config import EndpointStubConfig
from execution.endpoints.dispatch import (
    DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY,
    TERMINAL_ENDPOINT_DISPATCH_STATUSES,
    AsyncEndpointRequestDispatcher,
    AsyncEndpointResponseStream,
    EndpointBackendUnreachable,
    EndpointDispatchError,
    EndpointDispatchRecord,
    EndpointDispatchStatus,
    EndpointDispatchTarget,
    EndpointDispatchUnavailable,
)
from execution.endpoints.previews import PreviewSessionService
from execution.mounts import (
    container_resource_mounts,
    container_resource_mounts_require_workspace_storage,
)
from execution.services import EndpointExecutionServices

ENDPOINT_DISPATCH_POLL_INTERVAL_SECONDS = 0.05
ENDPOINT_HEALTH_PROBE_TIMEOUT_SECONDS = 10.0
ENDPOINT_BACKPRESSURE_STATUS_CODE = 429
ENDPOINT_CANCELLED_STATUS_CODE = 499
ENDPOINT_REQUEST_BUFFER_FULL_MESSAGE = "endpoint request buffer is full"
ENDPOINT_REQUEST_BUFFER_FULL_REASON = "request_buffer_full"


def _add_task_headers(headers: HeaderMap, task_id: str) -> None:
    headers["X-Task-Id"] = [task_id]
    headers["Access-Control-Expose-Headers"] = ["X-Task-Id"]


def _task_result(response: EndpointForwardResponse) -> dict[str, JsonValue]:
    return {
        "status_code": response.status_code,
        "body_size_bytes": len(response.body),
    }


@dataclass(frozen=True, slots=True)
class EndpointIngressDispatchSession:
    task_id: str
    stub_id: str
    workspace_id: str
    target: EndpointDispatchTarget
    headers: dict[str, list[str]]
    wait_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class EndpointDispatchAdmission:
    stub: StubRecord
    task: Task
    record: EndpointDispatchRecord
    settings: EndpointDispatchSettings


@dataclass(slots=True)
class _CapacityWait:
    """One request's wait for a container, carried across every target it tries.

    A warmup is asked for once per request, not once per stale target, so the
    flag lives here rather than in the loop that selects targets.
    """

    deadline: float
    warmup_attempted: bool = False

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def poll_delay(self) -> float:
        return min(ENDPOINT_DISPATCH_POLL_INTERVAL_SECONDS, max(self.remaining(), 0.0))


@dataclass(slots=True)
class EndpointControlService:
    services: EndpointExecutionServices
    async_database: AsyncDatabaseClient | None = None
    async_dispatcher: AsyncEndpointRequestDispatcher | None = None
    control_plane: ControlPlaneService = field(init=False)

    def __post_init__(self) -> None:
        self.control_plane = ControlPlaneService(self.services.context)

    def warm_endpoint(
        self,
        request: EndpointWarmupRequest,
    ) -> EndpointWarmupResponse:
        stub = self.control_plane.get_stub(request.stub_id)
        if PreviewSessionService(self.services).for_stub(stub.id) is not None:
            raise NotFoundError("preview capacity belongs to its session")
        return self._start_endpoint_container(stub)

    def create_preview(
        self, request: CreatePreviewRequest, *, workspace_id: str
    ) -> PreviewSessionResponse:
        previews = PreviewSessionService(self.services)
        record = previews.create(request, workspace_id=workspace_id)
        try:
            stub = previews.execution_stub(record.id)
            self._start_endpoint_container(stub, preview_id=record.id)
        except Exception:
            previews.stop(record.id, workspace_id=workspace_id)
            raise
        return previews.get(record.id, workspace_id=workspace_id)

    def get_preview(
        self, preview_id: str, *, workspace_id: str | None = None, public: bool = False
    ) -> PreviewSessionResponse:
        previews = PreviewSessionService(self.services)
        record = previews.get(preview_id, workspace_id=workspace_id, public=public)
        if record.status is PreviewSessionStatus.Active:
            try:
                previews.require_active(record)
            except NotFoundError:
                return previews.get(preview_id, workspace_id=workspace_id, public=public)
        return record

    def renew_preview(self, preview_id: str, *, workspace_id: str) -> PreviewSessionResponse:
        return PreviewSessionService(self.services).renew(preview_id, workspace_id=workspace_id)

    def stop_preview(self, preview_id: str, *, workspace_id: str) -> None:
        PreviewSessionService(self.services).stop(preview_id, workspace_id=workspace_id)

    def expire_previews(self, *, now: datetime | None = None, limit: int = 100) -> int:
        return PreviewSessionService(self.services).reconcile(now=now, limit=limit)

    def _start_endpoint_container(
        self, stub: StubRecord, *, preview_id: str | None = None
    ) -> EndpointWarmupResponse:
        if stub.kind not in {StubKind.Endpoint, StubKind.Asgi}:
            raise InvalidInputError(f"stub is not an endpoint: {stub.id}")
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        config = EndpointStubConfig.model_validate(stub.config, from_attributes=True)
        entrypoint = [config.image.python_executable, "-m", "runner.serve"]
        image_id = config.effective_image_id or ENDPOINT_IMAGE
        startup_kind = (
            WorkerStartupKind.Asgi if stub.kind is StubKind.Asgi else WorkerStartupKind.Endpoint
        )
        env = {
            **config.env,
            APP_ID_ENV: stub.app_id or "",
            CHECKPOINT_ENABLED_ENV: str(config.runtime.checkpoint_enabled).lower(),
            STUB_ID_ENV: stub.id,
            STUB_TYPE_ENV: stub.kind.value,
            "HANDLER": stub.handler or "",
            ENDPOINT_WORKERS_ENV: str(config.workers),
            LIFECYCLE_HOOKS_ENV: config.lifecycle_hooks.model_dump_json(),
            HOT_RELOAD_ENV: "true" if preview_id else "false",
            HOT_RELOAD_DIR_ENV: WORKER_USER_CODE_VOLUME,
        }
        with self.services.context.database.session() as session:
            preview = (
                PreviewSessionRepository(session).get(preview_id, lock=True) if preview_id else None
            )
            if preview_id and (
                preview is None
                or preview.status != PreviewSessionStatus.Active.value
                or preview.container_id is not None
            ):
                raise NotFoundError("preview session is not available for startup")
            if preview is not None and (
                (preview.expires_at is not None and preview.expires_at <= utc_now())
                or not self.services.redis_client.exists(
                    PreviewSessionService(self.services).lease_key(preview.id)
                )
            ):
                raise NotFoundError("preview session has expired")
            container = self.services.containers.reserve_pending(
                session,
                PendingContainerReservation(
                    name=f"endpoint-{stub.name}",
                    image=image_id,
                    command=entrypoint,
                    workspace_id=stub.workspace_id,
                    stub_id=stub.id,
                    app_id=stub.app_id,
                    env=env,
                    gpu=list(config.runtime.gpu),
                    gpu_count=config.runtime.gpu_count,
                    region=config.runtime.region,
                    availability_zone=config.runtime.availability_zone,
                ),
            )
            if preview is not None:
                preview.container_id = container.id
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
        checkpoint = (
            latest_available_checkpoint(
                self.services.context,
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
            )
            if config.runtime.checkpoint_enabled
            else None
        )
        if preview_id is not None:
            with self.services.context.database.session() as session:
                if not ContainerRolloutRepository(session).accepting_work(
                    container.id, stub_id=stub.id
                ):
                    raise NotFoundError("preview container admission is closed")
                self._validate_preview_scope(
                    session, EndpointForwardRequest(stub_id=stub.id, preview_session_id=preview_id)
                )
        scheduled = self.services.containers.submit_scheduler_request(
            container,
            ContainerSchedulingOptions(
                workspace_name=workspace.name,
                stub_type=stub.kind.value,
                startup_kind=startup_kind,
                entrypoint=entrypoint,
                cwd=WORKER_USER_CODE_VOLUME,
                env_list=[f"{key}={value}" for key, value in env.items()],
                image_id=image_id,
                app_id=stub.app_id or "",
                deployment_id=stub.deployment_id or "",
                ports=[CONTAINER_INNER_PORT],
                requested_ports=[CONTAINER_INNER_PORT],
                checkpoint_exposed_ports=(
                    checkpoint.exposed_ports if checkpoint is not None else []
                ),
                checkpoint_id=checkpoint.checkpoint_id if checkpoint is not None else "",
                checkpoint_enabled=config.runtime.checkpoint_enabled,
                cpu_millicores=config.runtime.requested_cpu_millicores,
                cpu_limit_millicores=config.runtime.limit_cpu_millicores,
                memory_mib=config.runtime.requested_memory_mib,
                memory_limit_mib=config.runtime.limit_memory_mib,
                disk_mib=config.runtime.requested_disk_mib,
                gpu=list(container.gpu),
                gpu_count=container.gpu_count,
                pool_selector=config.effective_pool_selector,
                region=config.runtime.region,
                availability_zone=config.runtime.availability_zone,
                runtime=config.runtime.runtime,
                runtime_class=config.runtime.runtime_class or "",
                docker_enabled=config.runtime.docker_enabled,
                preemptible=config.runtime.preemptible,
                workspace_gpu_quota=config.runtime.workspace_gpu_quota,
                workspace_cpu_quota_millicores=config.runtime.workspace_cpu_quota_millicores,
                secret_names=config.secrets,
                workspace_storage_required=(
                    container_resource_mounts_require_workspace_storage(
                        context=self.services.context,
                        workspace_id=stub.workspace_id,
                        mounts=resource_mounts,
                    )
                ),
                mounts=resource_mounts,
                gateway_token_required=True,
            ),
        )
        if not scheduled.accepted:
            raise UpstreamUnavailableError(
                scheduled.reason or "endpoint container scheduling failed"
            )
        self.services.events.emit(
            "endpoint.container.started",
            level=EventLevel.Info,
            resource_type="container",
            resource_id=container.id,
            message=f"started endpoint container for {stub.name}",
            data={
                "stub_id": stub.id,
                "preview_session_id": preview_id,
            },
            workspace_id=stub.workspace_id,
        )
        return EndpointWarmupResponse(container_id=container.id)

    async def forward_endpoint_request(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse:
        try:
            stub = await self._async_database().run_transaction(
                lambda session: self.control_plane.get_stub_in_session(
                    session,
                    request.stub_id,
                )
            )
            if stub.kind is StubKind.Endpoint:
                return await self._forward_function_endpoint(stub, request)
            if stub.kind is StubKind.Asgi:
                return await self._forward_asgi_endpoint(stub, request)
            return error_response(404, f"stub is not an endpoint: {stub.id}")
        except NotFoundError:
            return error_response(404, "endpoint not found")
        except Exception as exc:
            return error_response(500, str(exc))

    async def forward_endpoint_health(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse:
        """Answer a health probe without opening an invocation.

        Every other path here creates a task and meters it, which is right for a
        request the workload runs and wrong for one asking whether it could. An
        uptime check left pointing at this path would otherwise accrue task rows
        and billable usage for work nobody asked for. Admission control still
        applies: the request reaches the workload either way, and the public URL
        that carries it needs no token.

        No ready container is reported as unavailable rather than waited out: the
        caller asked for the current answer, and a probe that blocks until
        capacity arrives has stopped being a probe.

        The forward here is itself the probe, so selection deliberately skips the
        readiness filter: running it would send every container the same
        `GET /health` this request is about to send one of them, and an external
        uptime check polls far slower than the verdict is cached, so that second
        request is never the free one.
        """

        try:
            await self._async_database().run_transaction(
                lambda session: self._validate_preview_scope(session, request)
            )
            stub = await self._async_database().run_transaction(
                lambda session: self.control_plane.get_stub_in_session(
                    session,
                    request.stub_id,
                )
            )
            if stub.kind is not StubKind.Asgi:
                return error_response(404, f"stub is not an ASGI endpoint: {stub.id}")
            config = EndpointStubConfig.model_validate(stub.config, from_attributes=True)
            settings = _dispatch_settings(config)
            active_count = await AsyncEndpointDispatchStateRepository(
                self._async_database()
            ).active_count(stub.id)
            if active_count >= settings.max_pending_requests:
                self._record_request_rejected(stub)
                return error_response(
                    ENDPOINT_BACKPRESSURE_STATUS_CODE,
                    ENDPOINT_REQUEST_BUFFER_FULL_MESSAGE,
                )
            dispatcher = self.async_dispatcher
            if dispatcher is None:
                return error_response(503, "endpoint dispatcher is not configured")
            target = await dispatcher.unprobed_target(stub.id)
            if target is None:
                return error_response(503, "no running endpoint containers")
            stream = await dispatcher.open_http_stream(
                target, request, timeout_seconds=ENDPOINT_HEALTH_PROBE_TIMEOUT_SECONDS
            )
            return await _read_forward_response(stream)
        except NotFoundError:
            return error_response(404, "endpoint not found")
        except Exception as exc:
            return error_response(502, str(exc))

    async def prepare_asgi_http(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointIngressDispatchSession:
        return await self._prepare_asgi_ingress(request, websocket=False)

    async def prepare_asgi_websocket(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointIngressDispatchSession:
        return await self._prepare_asgi_ingress(request, websocket=True)

    async def _prepare_asgi_ingress(
        self,
        request: EndpointForwardRequest,
        *,
        websocket: bool,
    ) -> EndpointIngressDispatchSession:
        stub = await self._async_database().run_transaction(
            lambda session: self.control_plane.get_stub_in_session(session, request.stub_id)
        )
        if stub.kind is not StubKind.Asgi:
            msg = f"stub is not an ASGI endpoint: {stub.id}"
            raise EndpointDispatchUnavailable(msg)
        admission = await self._admit_dispatch_task(
            stub,
            request,
            task_kwargs={
                "method": request.method,
                "path": request.path,
                "websocket": websocket,
            },
        )
        if admission is None:
            self._record_request_rejected(stub)
            raise EndpointWebSocketDispatchRejected(
                ENDPOINT_REQUEST_BUFFER_FULL_MESSAGE,
                status_code=ENDPOINT_BACKPRESSURE_STATUS_CODE,
            )
        stub = admission.stub
        task = admission.task
        settings = admission.settings
        repository = AsyncEndpointDispatchStateRepository(self._async_database())
        await self._record_endpoint_request_usage(stub, task)
        forwarded = request.model_copy(
            update={"headers": _headers_with_task_id(request.headers, task.id)}
        )
        await self._emit_dispatch_lifecycle(stub, admission.record)

        wait = _CapacityWait(deadline=time.monotonic() + settings.wait_timeout_seconds)
        try:
            target, record = await self._claim_target(
                self._dispatcher(),
                repository,
                stub,
                task,
                wait,
                max_inflight_per_container=settings.max_inflight_per_container,
            )
            self._observe_dispatch_latencies(stub, record)
        except Exception as exc:
            await self._record_dispatch_failure(repository, stub, task, exc)
            raise

        return EndpointIngressDispatchSession(
            task_id=task.id,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            target=target,
            headers=forwarded.headers,
            wait_timeout_seconds=settings.wait_timeout_seconds,
        )

    async def heartbeat_asgi_websocket(self, task_id: str) -> None:
        task = await self.services.tasks.get_async(task_id)
        record = await AsyncEndpointDispatchStateRepository(self._async_database()).heartbeat(task)
        try:
            stub = await self._async_database().run_transaction(
                lambda session: self.control_plane.get_stub_in_session(session, record.stub_id)
            )
        except NotFoundError:
            return
        await self._emit_dispatch_lifecycle(stub, record, emit_event=False)

    async def open_asgi_websocket_socket(
        self,
        session: EndpointIngressDispatchSession,
    ) -> socket.socket | None:
        return await self._dispatcher().open_backend_socket(session.target)

    async def open_asgi_http_stream(
        self,
        session: EndpointIngressDispatchSession,
        request: EndpointForwardRequest,
    ) -> AsyncEndpointResponseStream:
        return await self._dispatcher().open_http_stream(
            session.target,
            request.model_copy(update={"headers": session.headers}),
            timeout_seconds=session.wait_timeout_seconds,
        )

    async def finish_asgi_http(
        self,
        task_id: str,
        *,
        status_code: int | None = None,
        body_size_bytes: int = 0,
        cancelled: bool = False,
        error: str | None = None,
    ) -> None:
        if error is None and status_code is not None and not 200 <= status_code < 400:
            error = f"HTTP {status_code}"
        await self._finish_asgi_ingress(
            task_id,
            result={
                "status_code": status_code,
                "body_size_bytes": body_size_bytes,
            },
            cancelled=cancelled,
            error=error,
        )

    async def finish_asgi_websocket(
        self,
        task_id: str,
        *,
        cancelled: bool = False,
        error: str | None = None,
    ) -> None:
        await self._finish_asgi_ingress(
            task_id,
            result={"websocket": True},
            cancelled=cancelled,
            error=error,
        )

    async def _finish_asgi_ingress(
        self,
        task_id: str,
        *,
        result: dict[str, JsonValue],
        cancelled: bool,
        error: str | None,
    ) -> None:
        try:
            task = await self.services.tasks.get_async(task_id)
        except NotFoundError:
            return
        try:
            record = await AsyncEndpointDispatchStateRepository(self._async_database()).transition(
                task,
                _websocket_terminal_status(cancelled=cancelled, error=error),
                error=error,
            )
            stub = await self._async_database().run_transaction(
                lambda session: self.control_plane.get_stub_in_session(session, record.stub_id)
            )
        except NotFoundError:
            return
        await self._emit_dispatch_lifecycle(stub, record)
        task_status = TaskStatus.Complete
        if cancelled:
            task_status = TaskStatus.Cancelled
        elif error:
            task_status = TaskStatus.Failed
        await self.services.tasks.transition_async(
            task,
            task_status,
            result=result,
            error=error,
            exit_code=1 if error else 0,
        )

    async def _forward_function_endpoint(
        self,
        stub: StubRecord,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse:
        try:
            payload = serialize_http_task_payload(request.body, query_params=request.query_params)
        except ValueError as exc:
            return error_response(400, str(exc))
        admission = await self._admit_dispatch_task(
            stub,
            request,
            task_args=payload.args or [],
            task_kwargs=payload.kwargs,
            retry=True,
        )
        if admission is None:
            self._record_request_rejected(stub)
            return error_response(
                ENDPOINT_BACKPRESSURE_STATUS_CODE,
                ENDPOINT_REQUEST_BUFFER_FULL_MESSAGE,
            )
        stub = admission.stub
        task = admission.task
        settings = admission.settings
        await self._record_endpoint_request_usage(stub, task)
        await self._emit_dispatch_lifecycle(stub, admission.record)
        forwarded = request.model_copy(
            update={
                "body": _endpoint_payload_body(payload.args or [], payload.kwargs),
                "query_params": {},
                "headers": _headers_with_task_id(
                    request.headers,
                    task.id,
                    content_type="application/json",
                ),
            }
        )
        while True:
            response = await self._dispatch_task(stub, task, forwarded, settings)
            task = await self.services.tasks.get_async(task.id)
            if _forward_response_succeeded(response) or task.status is not TaskStatus.Retry:
                return response
            retry_at = task.next_retry_at
            if retry_at is not None:
                delay_seconds = max((retry_at - utc_now()).total_seconds(), 0.0)
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)

    async def _forward_asgi_endpoint(
        self,
        stub: StubRecord,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse:
        admission = await self._admit_dispatch_task(
            stub,
            request,
            task_kwargs={
                "method": request.method,
                "path": request.path,
            },
        )
        if admission is None:
            self._record_request_rejected(stub)
            return error_response(
                ENDPOINT_BACKPRESSURE_STATUS_CODE,
                ENDPOINT_REQUEST_BUFFER_FULL_MESSAGE,
            )
        stub = admission.stub
        task = admission.task
        settings = admission.settings
        await self._record_endpoint_request_usage(stub, task)
        await self._emit_dispatch_lifecycle(stub, admission.record)
        forwarded = request.model_copy(
            update={"headers": _headers_with_task_id(request.headers, task.id)}
        )
        return await self._dispatch_task(stub, task, forwarded, settings)

    def _validate_preview_scope(
        self, session: DatabaseSession, request: EndpointForwardRequest
    ) -> None:
        preview = PreviewSessionRepository(session).for_stub(request.stub_id, lock=True)
        if preview is None:
            if request.preview_session_id is not None:
                raise NotFoundError("preview session not found")
            return
        if (
            preview.id != try_uuid(request.preview_session_id)
            or preview.status != PreviewSessionStatus.Active.value
            or (preview.expires_at is not None and preview.expires_at <= utc_now())
            or not self.services.redis_client.exists(
                PreviewSessionService(self.services).lease_key(preview.id)
            )
        ):
            raise NotFoundError("preview session has ended")

    async def _admit_dispatch_task(
        self,
        stub: StubRecord,
        request: EndpointForwardRequest,
        *,
        task_args: list[JsonValue] | None = None,
        task_kwargs: dict[str, JsonValue] | None = None,
        retry: bool = False,
    ) -> EndpointDispatchAdmission | None:
        admission = await self._async_database().run_transaction(
            lambda session: self._admit_dispatch_task_in_session(
                session,
                stub,
                request,
                task_args=task_args,
                task_kwargs=task_kwargs,
                retry=retry,
            )
        )
        if admission is not None:
            await self.services.tasks.publish_created_async(admission.task)
        return admission

    def _admit_dispatch_task_in_session(
        self,
        session: DatabaseSession,
        stub: StubRecord,
        request: EndpointForwardRequest,
        *,
        task_args: list[JsonValue] | None,
        task_kwargs: dict[str, JsonValue] | None,
        retry: bool,
    ) -> EndpointDispatchAdmission | None:
        self._validate_preview_scope(session, request)
        locked_stub = StubRepository(session).get_for_update(
            stub.id,
            workspace_id=stub.workspace_id,
        )
        if locked_stub is None:
            raise NotFoundError(f"stub not found: {stub.id}")
        config = EndpointStubConfig.model_validate(locked_stub.config, from_attributes=True)
        settings = _dispatch_settings(config)
        now = utc_now()
        dispatches = EndpointDispatchRepository(session)
        if dispatches.active_count(locked_stub.id, at=now) >= settings.max_pending_requests:
            return None
        task = self.services.tasks.create_in_transaction(
            session,
            f"{locked_stub.kind.value}-{locked_stub.name}",
            workspace_id=locked_stub.workspace_id,
            app_id=locked_stub.app_id,
            stub_id=locked_stub.id,
            deployment_id=locked_stub.deployment_id,
            handler=locked_stub.handler,
            args=task_args,
            kwargs=task_kwargs,
            retry_policy=config.effective_retry_policy if retry else None,
        )
        record = EndpointDispatchRecord(
            task_id=task.id,
            stub_id=locked_stub.id,
            workspace_id=locked_stub.workspace_id,
            method=request.method,
            path=request.path,
            wait_timeout_seconds=settings.wait_timeout_seconds,
            max_pending_requests=settings.max_pending_requests,
            max_inflight_per_container=settings.max_inflight_per_container,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=max(settings.wait_timeout_seconds, 1.0)),
        )
        persisted = _dispatch_record(dispatches.create(_dispatch_state(record)))
        return EndpointDispatchAdmission(
            stub=locked_stub,
            task=task,
            record=persisted,
            settings=settings,
        )

    def _record_request_rejected(self, stub: StubRecord) -> None:
        self.services.metrics.increment(
            "endpoint_admission_rejected_total",
            labels={
                "stub_id": stub.id,
                "kind": stub.kind.value,
                "reason": ENDPOINT_REQUEST_BUFFER_FULL_REASON,
            },
        )

    async def _dispatch_task(
        self,
        stub: StubRecord,
        task: Task,
        request: EndpointForwardRequest,
        settings: EndpointDispatchSettings,
    ) -> EndpointForwardResponse:
        repository = AsyncEndpointDispatchStateRepository(self._async_database())
        try:
            response = await self._wait_for_dispatch(
                self._dispatcher(),
                repository,
                stub,
                task,
                request,
                timeout_seconds=settings.wait_timeout_seconds,
                max_inflight_per_container=settings.max_inflight_per_container,
            )
        except Exception as exc:
            status_code = await self._record_dispatch_failure(repository, stub, task, exc)
            response = error_response(status_code, str(exc))
            _add_task_headers(response.headers, task.id)
            return response

        _add_task_headers(response.headers, task.id)
        forward_error = _forward_response_error(response)
        if _forward_response_succeeded(response):
            record = await repository.transition(task, EndpointDispatchStatus.Complete)
            await self._emit_dispatch_lifecycle(stub, record)
            await self.services.tasks.transition_async(
                task,
                TaskStatus.Complete,
                result=_task_result(response),
                error=None,
                exit_code=0,
            )
        else:
            outcome = await self.services.tasks.finish_with_retry_async(
                task.id,
                TaskStatus.Failed,
                result=_task_result(response),
                error=forward_error,
                exit_code=1,
            )
            record = (
                await repository.requeue(outcome.task)
                if outcome.retry_decision.should_retry
                else await repository.transition(
                    task,
                    EndpointDispatchStatus.Failed,
                    error=forward_error,
                )
            )
            await self._emit_dispatch_lifecycle(stub, record)
        return response

    async def _wait_for_dispatch(
        self,
        dispatcher: AsyncEndpointRequestDispatcher,
        repository: AsyncEndpointDispatchStateRepository,
        stub: StubRecord,
        task: Task,
        request: EndpointForwardRequest,
        *,
        timeout_seconds: float,
        max_inflight_per_container: int,
    ) -> EndpointForwardResponse:
        wait = _CapacityWait(deadline=time.monotonic() + timeout_seconds)
        while True:
            target, record = await self._claim_target(
                dispatcher,
                repository,
                stub,
                task,
                wait,
                max_inflight_per_container=max_inflight_per_container,
            )
            started = time.monotonic()
            try:
                stream = await dispatcher.open_http_stream(
                    target,
                    request,
                    timeout_seconds=max(wait.remaining(), 0.01),
                )
            except EndpointBackendUnreachable:
                # Nothing was written, so the target was stale rather than the
                # endpoint broken: a container can register a route and die before
                # anyone dials it. Selecting again replays nothing, and the next
                # pass decides whether capacity can still arrive.
                await asyncio.sleep(wait.poll_delay())
                continue
            try:
                response = await _read_forward_response(stream)
            except Exception as exc:
                # The request left this process, so this attempt is final whatever
                # went wrong and whatever the application already did with it.
                self._observe_dispatch_latencies(stub, record, started=started)
                raise await self._forward_failed(target.container_id, exc) from exc
            self._observe_dispatch_latencies(stub, record, started=started)
            return response

    async def _claim_target(
        self,
        dispatcher: AsyncEndpointRequestDispatcher,
        repository: AsyncEndpointDispatchStateRepository,
        stub: StubRecord,
        task: Task,
        wait: _CapacityWait,
        *,
        max_inflight_per_container: int,
    ) -> tuple[EndpointDispatchTarget, EndpointDispatchRecord]:
        outdated_containers: set[str] = set()
        while True:
            await self._raise_if_cancelled(task.id)
            previews = PreviewSessionService(self.services)
            preview = await asyncio.to_thread(previews.for_stub, stub.id)
            if preview is not None:
                await asyncio.to_thread(previews.require_active, preview)
            if wait.warmup_attempted:
                await self._raise_if_capacity_is_dead(dispatcher, stub)
            if wait.remaining() <= 0:
                raise EndpointDispatchTimedOut("Timed out waiting for a backend container")

            record = await repository.transition(task, EndpointDispatchStatus.WaitingCapacity)
            await self._emit_dispatch_lifecycle(stub, record, emit_event=False)
            loads = await repository.inflight_counts(stub.id)
            target = await dispatcher.select_target(
                stub.id,
                container_loads=loads,
                max_inflight_per_container=max_inflight_per_container,
                excluded_container_ids=(await repository.closed_containers(stub.id))
                | outdated_containers,
            )
            if target is None:
                if not wait.warmup_attempted:
                    await self._request_capacity(stub, task)
                    wait.warmup_attempted = True
                await asyncio.sleep(wait.poll_delay())
                continue

            if not await asyncio.to_thread(
                self.services.containers.accepting_work, target.container_id
            ):
                outdated_containers.add(target.container_id)
                continue
            record = await self._claim_dispatch_task(
                task, container_id=target.container_id, preview_id=preview.id if preview else None
            )
            if record is None:
                await asyncio.sleep(wait.poll_delay())
                continue
            await self._emit_dispatch_lifecycle(stub, record)
            # Bind the task to the container that will serve it, so its record
            # carries the same attribution every other workload kind has.
            await self.services.tasks.assign_async(task, container_id=target.container_id)
            return target, record

    async def _claim_dispatch_task(
        self, task: Task, *, container_id: str, preview_id: str | None
    ) -> EndpointDispatchRecord | None:
        def claim_in_session(session: DatabaseSession) -> EndpointDispatchRecord | None:
            if not ContainerRolloutRepository(session).accepting_work(
                container_id, stub_id=task.stub_id or ""
            ):
                return None
            self._validate_preview_scope(
                session,
                EndpointForwardRequest(stub_id=task.stub_id or "", preview_session_id=preview_id),
            )
            return _transition_dispatch_in_session(
                session,
                task,
                EndpointDispatchStatus.Inflight,
                container_id=container_id,
                error=None,
            )

        return await self._async_database().run_transaction(claim_in_session)

    async def _forward_failed(
        self,
        container_id: str,
        exc: Exception,
    ) -> EndpointDispatchError:
        """Name the container that dropped the request rather than the socket that noticed.

        A handler dying mid-request looks, from the transport, exactly like a network
        fault, and only the container's exit code tells the two apart. Its status may
        not have been written yet, which is why the transport error stays in the
        message instead of being replaced by a guess.
        """

        detail = f"container {container_id}"
        container = await self._async_database().run_transaction(
            lambda session: ContainerRepository(session).get_across_workspaces(container_id)
        )
        if container is not None and container.exit_code is not None:
            detail = f"{detail} exited with code {container.exit_code}"
        return EndpointDispatchError(
            f"the request reached {detail} and it stopped before answering ({exc}); "
            f"check the container logs"
        )

    def _observe_dispatch_latencies(
        self,
        stub: StubRecord,
        record: EndpointDispatchRecord,
        *,
        started: float | None = None,
    ) -> None:
        labels = {"stub_id": stub.id, "kind": stub.kind.value}
        wait_seconds = max(
            ((record.started_at or utc_now()) - record.enqueued_at).total_seconds(),
            0.0,
        )
        self.services.metrics.observe_histogram(
            "endpoint_dispatch_queue_wait_seconds", wait_seconds, labels=labels
        )
        if started is not None:
            self.services.metrics.observe_histogram(
                "endpoint_dispatch_inflight_seconds", time.monotonic() - started, labels=labels
            )

    async def _raise_if_cancelled(self, task_id: str) -> None:
        try:
            task = await self.services.tasks.get_async(task_id)
        except NotFoundError:
            return
        if task.status is TaskStatus.Cancelled:
            raise EndpointDispatchCancelled("endpoint request cancelled")

    async def _raise_if_capacity_is_dead(
        self,
        dispatcher: AsyncEndpointRequestDispatcher,
        stub: StubRecord,
    ) -> None:
        """Stop waiting when every container that could serve this stub has died.

        Read only after a warmup has been asked for, so a cold start still gets its
        time. What this catches is capacity that will never arrive, such as a handler
        that fails on import and takes every replacement down with it.
        """

        containers = await self._async_database().run_transaction(
            lambda session: ContainerRepository(session).list(
                workspace_id=stub.workspace_id,
                statuses=(
                    ContainerStatus.Pending.value,
                    ContainerStatus.Running.value,
                    ContainerStatus.Failed.value,
                ),
                stub_ids=(stub.id,),
            )
        )
        if not containers or any(item.status is not ContainerStatus.Failed for item in containers):
            return
        # The listing is newest first, so the most recent failure is the one to name.
        latest = containers[0]
        # A container the fleet could not place has no logs to read and an exit code
        # this service invented; the scheduler holds the only real account of why.
        reason = next(
            (
                state.failure_reason
                for state in await dispatcher.container_states(stub.id)
                if state.container_id == latest.id and state.failure_reason
            ),
            "",
        )
        if reason:
            raise EndpointDispatchUnavailable(
                f"no container could start for this endpoint: {reason}"
            )
        detail = f" (exit code {latest.exit_code})" if latest.exit_code is not None else ""
        raise EndpointDispatchUnavailable(
            f"no container could start for this endpoint{detail}; "
            f"check the container logs for {latest.id}"
        )

    async def _request_capacity(self, stub: StubRecord, task: Task) -> None:
        previews = PreviewSessionService(self.services)
        preview = await asyncio.to_thread(previews.for_stub, stub.id)
        if preview is not None:
            await asyncio.to_thread(previews.require_active, preview)
            container_id = preview.container_id
            if container_id is not None:
                container = await self._async_database().run_transaction(
                    lambda session: ContainerRepository(session).get_across_workspaces(container_id)
                )
                if container is not None and container.status in {
                    ContainerStatus.Pending,
                    ContainerStatus.Running,
                }:
                    return
            raise EndpointDispatchUnavailable(
                "preview container is unavailable; start a new preview"
            )
        try:
            warmup = await asyncio.to_thread(
                self.warm_endpoint,
                EndpointWarmupRequest(stub_id=stub.id),
            )
        except (PaymentRequiredError, CapacityLimitReachedError):
            # Not converted to 503. Every other reason capacity cannot be had is a
            # transient shortage the caller retries into; these are the platform
            # declining, and the limit refusal names a ceiling the account can act
            # on that 503 would hide.
            raise
        except DomainError as exc:
            await self._emit_warmup_failure(stub, task, str(exc))
            raise EndpointDispatchUnavailable(str(exc)) from exc
        await self._emit_warmup_result(stub, task, warmup)

    async def _record_dispatch_failure(
        self,
        repository: AsyncEndpointDispatchStateRepository,
        stub: StubRecord,
        task: Task,
        exc: Exception,
    ) -> int:
        """Settle both records for a dispatch that raised; returns the HTTP status."""
        dispatch_status, task_status, status_code = _dispatch_failure(exc)
        record = await repository.transition(task, dispatch_status, error=str(exc))
        await self._emit_dispatch_lifecycle(stub, record)
        await self.services.tasks.transition_async(
            task,
            task_status,
            error=str(exc),
            exit_code=1,
        )
        return status_code

    async def _emit_dispatch_lifecycle(
        self,
        stub: StubRecord,
        record: EndpointDispatchRecord,
        *,
        emit_event: bool = True,
    ) -> None:
        labels = {
            "stub_id": stub.id,
            "kind": stub.kind.value,
            "status": record.status.value,
        }
        self.services.metrics.increment("endpoint_dispatch_lifecycle_total", labels=labels)
        self.services.metrics.set_gauge(
            "endpoint_dispatch_active_requests",
            0 if record.status in TERMINAL_ENDPOINT_DISPATCH_STATUSES else 1,
            labels={"stub_id": stub.id, "kind": stub.kind.value, "task_id": record.task_id},
        )
        if not emit_event:
            return
        level = (
            EventLevel.Error
            if record.status in {EndpointDispatchStatus.Failed, EndpointDispatchStatus.Timeout}
            else EventLevel.Info
        )
        await self.services.events.emit_async(
            f"endpoint.dispatch.{record.status.value}",
            level=level,
            resource_type="task",
            resource_id=record.task_id,
            message=f"endpoint request {record.status.value}",
            data={
                "stub_id": record.stub_id,
                "container_id": record.container_id,
                "attempts": record.attempts,
                "error": record.error,
            },
            workspace_id=record.workspace_id,
        )

    async def _emit_warmup_result(
        self,
        stub: StubRecord,
        task: Task,
        warmup: EndpointWarmupResponse,
    ) -> None:
        await self.services.events.emit_async(
            "endpoint.dispatch.warmup",
            level=EventLevel.Info,
            resource_type="task",
            resource_id=task.id,
            message="endpoint warmup scheduled",
            data={"stub_id": stub.id, "container_id": warmup.container_id},
            workspace_id=stub.workspace_id,
        )

    async def _emit_warmup_failure(
        self,
        stub: StubRecord,
        task: Task,
        error: str,
    ) -> None:
        await self.services.events.emit_async(
            "endpoint.dispatch.warmup",
            level=EventLevel.Error,
            resource_type="task",
            resource_id=task.id,
            message="endpoint warmup failed",
            data={"stub_id": stub.id, "error": error},
            workspace_id=stub.workspace_id,
        )

    async def _record_endpoint_request_usage(
        self,
        stub: StubRecord,
        task: Task,
    ) -> None:
        await self.services.usage.record_task_count_async(
            workspace_id=stub.workspace_id,
            resource_type="endpoint",
            resource_id=stub.id,
            task_id=task.id,
            kind=stub.kind.value,
            app_id=stub.app_id or "",
            deployment_id=stub.deployment_id or "",
        )

    def _async_database(self) -> AsyncDatabaseClient:
        if self.async_database is None:
            raise RuntimeError("endpoint asynchronous database is not configured")
        return self.async_database

    def _dispatcher(self) -> AsyncEndpointRequestDispatcher:
        if self.async_dispatcher is None:
            raise EndpointDispatchUnavailable("endpoint dispatcher is not configured")
        return self.async_dispatcher


def _dispatch_failure(exc: Exception) -> tuple[EndpointDispatchStatus, TaskStatus, int]:
    if isinstance(exc, NotFoundError):
        return EndpointDispatchStatus.Failed, TaskStatus.Failed, 404
    if isinstance(exc, EndpointDispatchCancelled):
        return (
            EndpointDispatchStatus.Cancelled,
            TaskStatus.Cancelled,
            ENDPOINT_CANCELLED_STATUS_CODE,
        )
    if isinstance(exc, EndpointDispatchTimedOut):
        return EndpointDispatchStatus.Timeout, TaskStatus.Timeout, 504
    if isinstance(exc, EndpointDispatchUnavailable):
        return EndpointDispatchStatus.Failed, TaskStatus.Failed, 503
    if isinstance(exc, EndpointDispatchError):
        return EndpointDispatchStatus.Failed, TaskStatus.Failed, 502
    return EndpointDispatchStatus.Failed, TaskStatus.Failed, 500


def _endpoint_payload_body(args: list[JsonValue], kwargs: dict[str, JsonValue]) -> bytes:
    return json.dumps(
        {
            "args": args,
            "kwargs": kwargs,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _headers_with_task_id(
    headers: dict[str, list[str]],
    task_id: str,
    *,
    content_type: str | None = None,
) -> dict[str, list[str]]:
    forwarded = {key: list(values) for key, values in headers.items()}
    forwarded["X-Task-Id"] = [task_id]
    if content_type is not None:
        forwarded["Content-Type"] = [content_type]
    return forwarded


@dataclass(frozen=True, slots=True)
class EndpointDispatchSettings:
    wait_timeout_seconds: float
    max_pending_requests: int
    max_inflight_per_container: int


@dataclass(slots=True)
class AsyncEndpointDispatchStateRepository:
    database: AsyncDatabaseClient

    async def closed_containers(self, stub_id: str) -> set[str]:
        return await self.database.run_transaction(
            lambda session: ContainerRolloutRepository(session).closed_for_stub(stub_id)
        )

    async def transition(
        self,
        task: Task,
        status: EndpointDispatchStatus,
        *,
        container_id: str | None = None,
        error: str | None = None,
    ) -> EndpointDispatchRecord:
        return await self.database.run_transaction(
            lambda session: _transition_dispatch_in_session(
                session,
                task,
                status,
                container_id=container_id,
                error=error,
            )
        )

    async def heartbeat(self, task: Task) -> EndpointDispatchRecord:
        return await self.database.run_transaction(
            lambda session: _heartbeat_dispatch_in_session(session, task)
        )

    async def requeue(self, task: Task) -> EndpointDispatchRecord:
        return await self.database.run_transaction(
            lambda session: _requeue_dispatch_in_session(session, task)
        )

    async def active_count(
        self,
        stub_id: str,
        *,
        exclude_task_id: str | None = None,
    ) -> int:
        return await self.database.run_transaction(
            lambda session: EndpointDispatchRepository(session).active_count(
                stub_id,
                at=utc_now(),
                exclude_task_id=exclude_task_id,
            )
        )

    async def inflight_counts(self, stub_id: str) -> dict[str, int]:
        return await self.database.run_transaction(
            lambda session: EndpointDispatchRepository(session).inflight_counts(
                stub_id,
                at=utc_now(),
            )
        )


def _transition_dispatch_in_session(
    session: DatabaseSession,
    task: Task,
    status: EndpointDispatchStatus,
    *,
    container_id: str | None,
    error: str | None,
) -> EndpointDispatchRecord:
    repository = EndpointDispatchRepository(session)
    state = repository.get_for_update(task.id)
    if state is None:
        raise NotFoundError(f"endpoint dispatch state is missing for task {task.id}")
    record = _dispatch_record(state)
    if status is EndpointDispatchStatus.Inflight:
        record.attempts += 1
    record.transition(status, container_id=container_id, error=error)
    return _dispatch_record(repository.update(_dispatch_state(record)))


def _requeue_dispatch_in_session(
    session: DatabaseSession,
    task: Task,
) -> EndpointDispatchRecord:
    repository = EndpointDispatchRepository(session)
    state = repository.get_for_update(task.id)
    if state is None:
        raise NotFoundError(f"endpoint dispatch state is missing for task {task.id}")
    record = _dispatch_record(state).requeue()
    return _dispatch_record(repository.update(_dispatch_state(record)))


def _heartbeat_dispatch_in_session(
    session: DatabaseSession,
    task: Task,
) -> EndpointDispatchRecord:
    repository = EndpointDispatchRepository(session)
    state = repository.get_for_update(task.id)
    if state is None:
        raise NotFoundError(f"endpoint dispatch state is missing for task {task.id}")
    record = _dispatch_record(state)
    record.transition(record.status)
    return _dispatch_record(repository.update(_dispatch_state(record)))


def _dispatch_state(record: EndpointDispatchRecord) -> EndpointDispatchStateRecord:
    return EndpointDispatchStateRecord(
        task_id=record.task_id,
        workspace_id=record.workspace_id,
        stub_id=record.stub_id,
        container_id=record.container_id,
        method=record.method,
        path=record.path,
        status=record.status.value,
        wait_timeout_seconds=record.wait_timeout_seconds,
        max_pending_requests=record.max_pending_requests,
        max_inflight_per_container=record.max_inflight_per_container,
        attempts=record.attempts,
        enqueued_at=record.enqueued_at,
        started_at=record.started_at,
        heartbeat_at=record.heartbeat_at,
        expires_at=record.expires_at,
        finished_at=record.finished_at,
        error=record.error,
    )


def _dispatch_record(state: EndpointDispatchStateRecord) -> EndpointDispatchRecord:
    return EndpointDispatchRecord(
        task_id=state.task_id,
        workspace_id=state.workspace_id,
        stub_id=state.stub_id,
        container_id=state.container_id,
        method=state.method,
        path=state.path,
        status=EndpointDispatchStatus(state.status),
        wait_timeout_seconds=state.wait_timeout_seconds,
        max_pending_requests=state.max_pending_requests,
        max_inflight_per_container=state.max_inflight_per_container,
        attempts=state.attempts,
        enqueued_at=state.enqueued_at,
        started_at=state.started_at,
        heartbeat_at=state.heartbeat_at,
        expires_at=state.expires_at,
        finished_at=state.finished_at,
        error=state.error,
    )


class EndpointDispatchCancelled(RuntimeError):
    pass


class EndpointDispatchTimedOut(RuntimeError):
    pass


class EndpointWebSocketDispatchRejected(RuntimeError):
    def __init__(self, message: str, *, status_code: int, task_id: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.task_id = task_id


def _websocket_terminal_status(
    *,
    cancelled: bool,
    error: str | None,
) -> EndpointDispatchStatus:
    if cancelled:
        return EndpointDispatchStatus.Cancelled
    if error:
        return EndpointDispatchStatus.Failed
    return EndpointDispatchStatus.Complete


def _dispatch_settings(config: EndpointStubConfig) -> EndpointDispatchSettings:
    return EndpointDispatchSettings(
        wait_timeout_seconds=config.wait_timeout_seconds,
        max_pending_requests=config.effective_max_pending_tasks,
        max_inflight_per_container=max(
            config.container_concurrency,
            DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY,
        ),
    )


async def _read_forward_response(stream: AsyncEndpointResponseStream) -> EndpointForwardResponse:
    try:
        body = b"".join([chunk async for chunk in stream.iter_chunks()])
    finally:
        await stream.close()
    return EndpointForwardResponse(
        status_code=stream.status_code,
        headers=stream.headers,
        body=body,
    )


def _forward_response_succeeded(response: EndpointForwardResponse) -> bool:
    return 200 <= response.status_code < 400


def _forward_response_error(response: EndpointForwardResponse) -> str | None:
    if _forward_response_succeeded(response):
        return None
    if response.body:
        return response.body.decode("utf-8", errors="replace")
    return f"HTTP {response.status_code}"


__all__ = ["EndpointControlService"]
