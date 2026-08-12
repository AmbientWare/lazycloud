from __future__ import annotations

import json
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from control.service import ControlPlaneService, StubKind, StubRecord
from database.repositories.orchestration import ContainerRepository
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
    ENDPOINT_INSTANCE_LOCK_ENV,
    ENDPOINT_SERVE_LOCK_ENV,
    ENDPOINT_WORKERS_ENV,
    GATEWAY_HTTP_URL_ENV,
    HOT_RELOAD_DIR_ENV,
    HOT_RELOAD_ENV,
    LIFECYCLE_HOOKS_ENV,
    STUB_ID_ENV,
    STUB_TYPE_ENV,
    no_gateway_origin,
)
from shared.errors import (
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
    StartEndpointServeRequest,
    StartEndpointServeResponse,
)
from shared.http.task_payload import serialize_http_task_payload
from shared.http.workspace_changes import WorkspaceChangeType
from shared.tasks import Task, TaskStatus
from shared.timestamps import utc_now

from execution.checkpoints import latest_available_checkpoint
from execution.containers.planning import ContainerSchedulingOptions
from execution.containers.service import PendingContainerReservation
from execution.endpoints.config import EndpointStubConfig
from execution.endpoints.dispatch import (
    ACTIVE_ENDPOINT_DISPATCH_STATUSES,
    DEFAULT_ENDPOINT_CONTAINER_CONCURRENCY,
    ENDPOINT_DISPATCH_TASK_KEY,
    TERMINAL_ENDPOINT_DISPATCH_STATUSES,
    EndpointBackendUnreachable,
    EndpointDispatchError,
    EndpointDispatchRecord,
    EndpointDispatchStatus,
    EndpointDispatchTarget,
    EndpointDispatchUnavailable,
    EndpointRequestDispatcher,
    EndpointResponseStream,
)
from execution.endpoints.keys import DEFAULT_ENDPOINT_SERVE_TIMEOUT_SECONDS
from execution.endpoints.serve import EndpointServeRequest, plan_endpoint_serve
from execution.mounts import (
    container_resource_mounts,
    container_resource_mounts_require_workspace_storage,
)
from execution.services import ExecutionServices

ENDPOINT_DISPATCH_POLL_INTERVAL_SECONDS = 0.05
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


@dataclass(slots=True)
class EndpointControlService:
    services: ExecutionServices
    dispatcher: EndpointRequestDispatcher | None = None
    gateway_http_url: Callable[[], str] = no_gateway_origin
    control_plane: ControlPlaneService = field(init=False)

    def __post_init__(self) -> None:
        self.control_plane = ControlPlaneService(self.services.context)

    def start_endpoint_serve(
        self,
        request: StartEndpointServeRequest,
    ) -> StartEndpointServeResponse:
        stub = self.control_plane.get_stub(request.stub_id)
        if stub.kind not in {StubKind.Endpoint, StubKind.Asgi}:
            raise InvalidInputError(f"stub is not an endpoint: {stub.id}")
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        config = EndpointStubConfig.model_validate(stub.config, from_attributes=True)
        timeout_seconds = request.timeout or DEFAULT_ENDPOINT_SERVE_TIMEOUT_SECONDS
        plan = plan_endpoint_serve(
            EndpointServeRequest(
                stub_id=stub.id,
                workspace_name=workspace.name,
                workspace_id=workspace.id,
                timeout_seconds=timeout_seconds,
                python_executable=config.image.python_executable,
            )
        )
        if not plan.authorized:
            raise InvalidInputError("Unauthorized")
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
            ENDPOINT_SERVE_LOCK_ENV: plan.serve_lock_key,
            ENDPOINT_INSTANCE_LOCK_ENV: plan.instance_lock_key,
            ENDPOINT_WORKERS_ENV: str(config.workers),
            LIFECYCLE_HOOKS_ENV: config.lifecycle_hooks.model_dump_json(),
            HOT_RELOAD_ENV: "true",
            HOT_RELOAD_DIR_ENV: WORKER_USER_CODE_VOLUME,
        }
        gateway_http_url = self.gateway_http_url()
        if gateway_http_url:
            env[GATEWAY_HTTP_URL_ENV] = gateway_http_url
        with self.services.context.database.session() as session:
            container = self.services.containers.reserve_pending(
                session,
                PendingContainerReservation(
                    name=f"endpoint-{stub.name}",
                    image=image_id,
                    command=list(plan.entrypoint),
                    workspace_id=stub.workspace_id,
                    stub_id=stub.id,
                    app_id=stub.app_id,
                    env=env,
                ),
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
        checkpoint = (
            latest_available_checkpoint(
                self.services.context,
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
            )
            if config.runtime.checkpoint_enabled
            else None
        )
        scheduled = self.services.containers.submit_scheduler_request(
            container,
            ContainerSchedulingOptions(
                workspace_name=workspace.name,
                stub_type=stub.kind.value,
                startup_kind=startup_kind,
                entrypoint=plan.entrypoint,
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
                memory_mib=config.runtime.requested_memory_mib,
                disk_mib=config.runtime.requested_disk_mib,
                gpu_type=config.runtime.requested_gpu_type,
                gpu_count=config.runtime.gpu_count,
                pool_selector=config.effective_pool_selector,
                runtime=config.runtime.runtime,
                runtime_class=config.runtime.runtime_class or "",
                docker_enabled=config.runtime.docker_enabled,
                preemptible=config.runtime.preemptible,
                gpu_limit=config.runtime.gpu_limit,
                cpu_limit_millicores=config.runtime.cpu_limit_millicores,
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
            with self.services.context.database.session() as session:
                container.status = ContainerStatus.Failed
                container.finished_at = utc_now()
                ContainerRepository(session).records.upsert(
                    container,
                    workspace_id=container.workspace_id,
                    name=container.name,
                    status=container.status.value,
                )
            self.services.containers.publish_lifecycle_change(
                container,
                WorkspaceChangeType.Updated,
            )
            raise UpstreamUnavailableError(
                scheduled.reason or "endpoint container scheduling failed"
            )
        self.services.events.emit(
            "endpoint.serve.started",
            level=EventLevel.Info,
            resource_type="container",
            resource_id=container.id,
            message=f"started endpoint serve container for {stub.name}",
            data={
                "stub_id": stub.id,
                "serve_lock_key": plan.serve_lock_key,
                "instance_lock_key": plan.instance_lock_key,
                "timeout_seconds": plan.wait_timeout_seconds,
            },
            workspace_id=stub.workspace_id,
        )
        return StartEndpointServeResponse(container_id=container.id)

    def forward_endpoint_request(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse:
        try:
            stub = self.control_plane.get_stub(request.stub_id)
            config = EndpointStubConfig.model_validate(stub.config, from_attributes=True)
            if stub.kind is StubKind.Endpoint:
                return self._forward_function_endpoint(stub, config, request)
            if stub.kind is StubKind.Asgi:
                return self._forward_asgi_endpoint(stub, config, request)
            return error_response(404, f"stub is not an endpoint: {stub.id}")
        except NotFoundError:
            return error_response(404, "endpoint not found")
        except Exception as exc:
            return error_response(500, str(exc))

    def prepare_asgi_http(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointIngressDispatchSession:
        return self._prepare_asgi_ingress(request, websocket=False)

    def prepare_asgi_websocket(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointIngressDispatchSession:
        return self._prepare_asgi_ingress(request, websocket=True)

    def _prepare_asgi_ingress(
        self,
        request: EndpointForwardRequest,
        *,
        websocket: bool,
    ) -> EndpointIngressDispatchSession:
        stub = self.control_plane.get_stub(request.stub_id)
        if stub.kind is not StubKind.Asgi:
            msg = f"stub is not an ASGI endpoint: {stub.id}"
            raise EndpointDispatchUnavailable(msg)
        repository = EndpointDispatchStateRepository(self.services)
        config = EndpointStubConfig.model_validate(stub.config, from_attributes=True)
        settings = _dispatch_settings(config)
        if not _endpoint_request_capacity_available(repository, stub, settings):
            self._record_request_rejected(stub)
            raise EndpointWebSocketDispatchRejected(
                ENDPOINT_REQUEST_BUFFER_FULL_MESSAGE,
                status_code=ENDPOINT_BACKPRESSURE_STATUS_CODE,
            )
        task = self.services.tasks.create(
            f"asgi-{stub.name}",
            workspace_id=stub.workspace_id,
            app_id=stub.app_id,
            stub_id=stub.id,
            deployment_id=stub.deployment_id,
            handler=stub.handler,
            kwargs={
                "method": request.method,
                "path": request.path,
                "websocket": websocket,
            },
        )
        self._record_endpoint_request_usage(stub, task)
        forwarded = request.model_copy(
            update={"headers": _headers_with_task_id(request.headers, task.id)}
        )
        record = repository.attach(
            task,
            stub=stub,
            request=forwarded,
            wait_timeout_seconds=settings.wait_timeout_seconds,
            max_pending_requests=settings.max_pending_requests,
            max_inflight_per_container=settings.max_inflight_per_container,
        )
        self._emit_dispatch_lifecycle(stub, record)

        dispatcher = self.dispatcher
        if dispatcher is None:
            record = repository.transition(
                task,
                EndpointDispatchStatus.Failed,
                error="endpoint dispatcher is not configured",
            )
            self._emit_dispatch_lifecycle(stub, record)
            self.services.tasks.transition(
                task,
                TaskStatus.Failed,
                error="endpoint dispatcher is not configured",
                exit_code=1,
            )
            raise EndpointDispatchUnavailable("endpoint dispatcher is not configured")

        try:
            target = self._wait_for_websocket_target(
                dispatcher,
                repository,
                stub,
                task,
                deadline=time.monotonic() + settings.wait_timeout_seconds,
                max_inflight_per_container=settings.max_inflight_per_container,
            )
        except EndpointDispatchUnavailable as exc:
            record = repository.transition(task, EndpointDispatchStatus.Failed, error=str(exc))
            self._emit_dispatch_lifecycle(stub, record)
            self.services.tasks.transition(task, TaskStatus.Failed, error=str(exc), exit_code=1)
            raise
        except EndpointDispatchCancelled as exc:
            record = repository.transition(task, EndpointDispatchStatus.Cancelled, error=str(exc))
            self._emit_dispatch_lifecycle(stub, record)
            self.services.tasks.transition(task, TaskStatus.Cancelled, error=str(exc), exit_code=1)
            raise
        except EndpointDispatchTimedOut as exc:
            record = repository.transition(task, EndpointDispatchStatus.Timeout, error=str(exc))
            self._emit_dispatch_lifecycle(stub, record)
            self.services.tasks.transition(task, TaskStatus.Timeout, error=str(exc), exit_code=1)
            raise
        except Exception as exc:
            record = repository.transition(task, EndpointDispatchStatus.Failed, error=str(exc))
            self._emit_dispatch_lifecycle(stub, record)
            self.services.tasks.transition(task, TaskStatus.Failed, error=str(exc), exit_code=1)
            raise

        return EndpointIngressDispatchSession(
            task_id=task.id,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            target=target,
            headers=forwarded.headers,
            wait_timeout_seconds=settings.wait_timeout_seconds,
        )

    def heartbeat_asgi_websocket(self, task_id: str) -> None:
        task = self.services.tasks.get(task_id)
        record = EndpointDispatchStateRepository(self.services).heartbeat(task)
        try:
            stub = self.control_plane.get_stub(record.stub_id)
        except NotFoundError:
            return
        self._emit_dispatch_lifecycle(stub, record, emit_event=False)

    def open_asgi_websocket_socket(
        self,
        session: EndpointIngressDispatchSession,
    ) -> socket.socket | None:
        dispatcher = self.dispatcher
        if dispatcher is None:
            raise EndpointDispatchUnavailable("endpoint dispatcher is not configured")
        return dispatcher.open_backend_socket(session.target)

    def open_asgi_http_stream(
        self,
        session: EndpointIngressDispatchSession,
        request: EndpointForwardRequest,
    ) -> EndpointResponseStream:
        dispatcher = self.dispatcher
        if dispatcher is None:
            raise EndpointDispatchUnavailable("endpoint dispatcher is not configured")
        return dispatcher.open_http_stream(
            session.target,
            request.model_copy(update={"headers": session.headers}),
            timeout_seconds=session.wait_timeout_seconds,
        )

    def finish_asgi_http(
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
        self._finish_asgi_ingress(
            task_id,
            result={
                "status_code": status_code,
                "body_size_bytes": body_size_bytes,
            },
            cancelled=cancelled,
            error=error,
        )

    def finish_asgi_websocket(
        self,
        task_id: str,
        *,
        cancelled: bool = False,
        error: str | None = None,
    ) -> None:
        self._finish_asgi_ingress(
            task_id,
            result={"websocket": True},
            cancelled=cancelled,
            error=error,
        )

    def _finish_asgi_ingress(
        self,
        task_id: str,
        *,
        result: dict[str, JsonValue],
        cancelled: bool,
        error: str | None,
    ) -> None:
        try:
            task = self.services.tasks.get(task_id)
        except NotFoundError:
            return
        try:
            record = EndpointDispatchStateRepository(self.services).transition(
                task,
                _websocket_terminal_status(cancelled=cancelled, error=error),
                error=error,
            )
            stub = self.control_plane.get_stub(record.stub_id)
        except NotFoundError:
            return
        self._emit_dispatch_lifecycle(stub, record)
        task_status = TaskStatus.Complete
        if cancelled:
            task_status = TaskStatus.Cancelled
        elif error:
            task_status = TaskStatus.Failed
        self.services.tasks.transition(
            task,
            task_status,
            result=result,
            error=error,
            exit_code=1 if error else 0,
        )

    def _forward_function_endpoint(
        self,
        stub: StubRecord,
        config: EndpointStubConfig,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse:
        settings = _dispatch_settings(config)
        if not self._request_capacity_available(stub, settings):
            self._record_request_rejected(stub)
            return error_response(
                ENDPOINT_BACKPRESSURE_STATUS_CODE,
                ENDPOINT_REQUEST_BUFFER_FULL_MESSAGE,
            )
        try:
            payload = serialize_http_task_payload(request.body, query_params=request.query_params)
        except ValueError as exc:
            return error_response(400, str(exc))
        task = self.services.tasks.create(
            f"endpoint-{stub.name}",
            workspace_id=stub.workspace_id,
            app_id=stub.app_id,
            stub_id=stub.id,
            deployment_id=stub.deployment_id,
            handler=stub.handler,
            args=payload.args or [],
            kwargs=payload.kwargs,
            retry_policy=config.effective_retry_policy,
        )
        self._record_endpoint_request_usage(stub, task)
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
            response = self._dispatch_task(stub, task, forwarded, settings)
            task = self.services.tasks.get(task.id)
            if _forward_response_succeeded(response) or task.status is not TaskStatus.Retry:
                return response
            retry_at = task.next_retry_at
            if retry_at is not None:
                delay_seconds = max((retry_at - utc_now()).total_seconds(), 0.0)
                if delay_seconds > 0:
                    time.sleep(delay_seconds)

    def _forward_asgi_endpoint(
        self,
        stub: StubRecord,
        config: EndpointStubConfig,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse:
        settings = _dispatch_settings(config)
        if not self._request_capacity_available(stub, settings):
            self._record_request_rejected(stub)
            return error_response(
                ENDPOINT_BACKPRESSURE_STATUS_CODE,
                ENDPOINT_REQUEST_BUFFER_FULL_MESSAGE,
            )
        task = self.services.tasks.create(
            f"asgi-{stub.name}",
            workspace_id=stub.workspace_id,
            app_id=stub.app_id,
            stub_id=stub.id,
            deployment_id=stub.deployment_id,
            handler=stub.handler,
            kwargs={
                "method": request.method,
                "path": request.path,
            },
        )
        self._record_endpoint_request_usage(stub, task)
        forwarded = request.model_copy(
            update={"headers": _headers_with_task_id(request.headers, task.id)}
        )
        return self._dispatch_task(stub, task, forwarded, settings)

    def _request_capacity_available(
        self,
        stub: StubRecord,
        settings: EndpointDispatchSettings,
    ) -> bool:
        repository = EndpointDispatchStateRepository(self.services)
        return _endpoint_request_capacity_available(repository, stub, settings)

    def _record_request_rejected(self, stub: StubRecord) -> None:
        self.services.metrics.increment(
            "endpoint_admission_rejected_total",
            labels={
                "stub_id": stub.id,
                "kind": stub.kind.value,
                "reason": ENDPOINT_REQUEST_BUFFER_FULL_REASON,
            },
        )

    def _dispatch_task(
        self,
        stub: StubRecord,
        task: Task,
        request: EndpointForwardRequest,
        settings: EndpointDispatchSettings,
    ) -> EndpointForwardResponse:
        dispatcher = self.dispatcher
        if dispatcher is None:
            return self._fail_task(
                task,
                error="endpoint dispatcher is not configured",
                status_code=503,
            )
        repository = EndpointDispatchStateRepository(self.services)
        record = repository.attach(
            task,
            stub=stub,
            request=request,
            wait_timeout_seconds=settings.wait_timeout_seconds,
            max_pending_requests=settings.max_pending_requests,
            max_inflight_per_container=settings.max_inflight_per_container,
        )
        self._emit_dispatch_lifecycle(stub, record)
        try:
            response = self._wait_for_dispatch(
                dispatcher,
                repository,
                stub,
                task,
                request,
                deadline=time.monotonic() + settings.wait_timeout_seconds,
                max_inflight_per_container=settings.max_inflight_per_container,
            )
        except EndpointDispatchUnavailable as exc:
            record = repository.transition(task, EndpointDispatchStatus.Failed, error=str(exc))
            self._emit_dispatch_lifecycle(stub, record)
            return self._fail_task(task, error=str(exc), status_code=503)
        except EndpointDispatchError as exc:
            record = repository.transition(task, EndpointDispatchStatus.Failed, error=str(exc))
            self._emit_dispatch_lifecycle(stub, record)
            return self._fail_task(task, error=str(exc), status_code=502)
        except EndpointDispatchCancelled as exc:
            record = repository.transition(task, EndpointDispatchStatus.Cancelled, error=str(exc))
            self._emit_dispatch_lifecycle(stub, record)
            return self._finish_cancelled_task(task, error=str(exc))
        except EndpointDispatchTimedOut as exc:
            record = repository.transition(task, EndpointDispatchStatus.Timeout, error=str(exc))
            self._emit_dispatch_lifecycle(stub, record)
            return self._finish_timed_out_task(task, error=str(exc))
        except Exception as exc:
            record = repository.transition(task, EndpointDispatchStatus.Failed, error=str(exc))
            self._emit_dispatch_lifecycle(stub, record)
            return self._fail_task(task, error=str(exc), status_code=500)

        _add_task_headers(response.headers, task.id)
        record_status = (
            EndpointDispatchStatus.Complete
            if _forward_response_succeeded(response)
            else EndpointDispatchStatus.Failed
        )
        forward_error = _forward_response_error(response)
        record = repository.transition(task, record_status, error=forward_error)
        self._emit_dispatch_lifecycle(stub, record)
        if _forward_response_succeeded(response):
            self.services.tasks.transition(
                task,
                TaskStatus.Complete,
                result=_task_result(response),
                error=None,
                exit_code=0,
            )
        else:
            self.services.tasks.finish_with_retry(
                task.id,
                TaskStatus.Failed,
                result=_task_result(response),
                error=forward_error,
                exit_code=1,
            )
        return response

    def _raise_if_capacity_is_dead(
        self,
        dispatcher: EndpointRequestDispatcher,
        stub: StubRecord,
    ) -> None:
        """Stop waiting when every container that could serve this stub has died.

        Read after a warmup has already been asked for, so a cold start still gets its
        time: what this catches is capacity that will never arrive, such as a handler
        that fails on import and takes every replacement down with it.
        """

        containers = self.services.containers.list(
            workspace_id=stub.workspace_id,
            statuses=(ContainerStatus.Pending, ContainerStatus.Running, ContainerStatus.Failed),
            stub_ids=(stub.id,),
        )
        if not containers or any(item.status is not ContainerStatus.Failed for item in containers):
            return
        # The listing is newest first, so the most recent failure is the one to name.
        latest = containers[0]
        reason = self._scheduling_failure_reason(dispatcher, stub, latest.id)
        if reason:
            raise EndpointDispatchUnavailable(
                f"no container could start for this endpoint: {reason}"
            )
        exit_code = latest.exit_code
        detail = f" (exit code {exit_code})" if exit_code is not None else ""
        raise EndpointDispatchUnavailable(
            f"no container could start for this endpoint{detail}; "
            f"check the container logs for {latest.id}"
        )

    @staticmethod
    def _scheduling_failure_reason(
        dispatcher: EndpointRequestDispatcher,
        stub: StubRecord,
        container_id: str,
    ) -> str:
        """The scheduler's account of a container that never reached a worker.

        A container the fleet could not place has no logs to read and an exit code this
        service invented, so pointing the caller at either sends them looking in the
        wrong place. The scheduler holds the only real account of why.
        """

        for state in dispatcher.container_states(stub.id):
            if state.container_id == container_id:
                return state.failure_reason
        return ""

    def _forward_failed(self, container_id: str, exc: Exception) -> EndpointDispatchError:
        """Name the container that dropped the request rather than the socket that noticed.

        The container is read back because a handler dying mid-request looks, from the
        transport, exactly like a network fault, and only its exit code tells the two
        apart. Its status may not have been written yet, which is why the transport
        error stays in the message instead of being replaced by a guess.
        """

        detail = f"container {container_id}"
        try:
            container = self.services.containers.get(container_id)
        except (NotFoundError, DomainError):
            container = None
        if container is not None and container.exit_code is not None:
            detail = f"{detail} exited with code {container.exit_code}"
        return EndpointDispatchError(
            f"the request reached {detail} and it stopped before answering ({exc}); "
            f"check the container logs"
        )

    def _request_capacity(self, stub: StubRecord, task: Task, *, warmup_attempted: bool) -> bool:
        """Ask for capacity once, and report that the ask has been made."""

        if warmup_attempted:
            return True
        try:
            warmup = self.start_endpoint_serve(StartEndpointServeRequest(stub_id=stub.id))
        except PaymentRequiredError:
            # Not converted. Every other reason capacity cannot be had is a
            # transient shortage the caller retries into; this one is the
            # platform declining, and telling them 503 sends them to look for an
            # outage that is not there.
            raise
        except DomainError as exc:
            self._emit_warmup_failure(stub, task, str(exc))
            raise EndpointDispatchUnavailable(str(exc)) from exc
        self._emit_warmup_result(stub, task, warmup)
        return True

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

    def _wait_for_dispatch(
        self,
        dispatcher: EndpointRequestDispatcher,
        repository: EndpointDispatchStateRepository,
        stub: StubRecord,
        task: Task,
        request: EndpointForwardRequest,
        *,
        deadline: float,
        max_inflight_per_container: int,
    ) -> EndpointForwardResponse:
        warmup_attempted = False
        while True:
            self._raise_if_cancelled(task.id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = "Timed out waiting for a backend container"
                raise EndpointDispatchTimedOut(msg)
            if warmup_attempted:
                self._raise_if_capacity_is_dead(dispatcher, stub)

            record = repository.transition(task, EndpointDispatchStatus.WaitingCapacity)
            self._emit_dispatch_lifecycle(stub, record, emit_event=False)
            loads = repository.inflight_counts(stub.id)
            target = dispatcher.select_target(
                stub.id,
                container_loads=loads,
                max_inflight_per_container=max_inflight_per_container,
            )
            if target is None:
                warmup_attempted = self._request_capacity(
                    stub, task, warmup_attempted=warmup_attempted
                )
                time.sleep(min(ENDPOINT_DISPATCH_POLL_INTERVAL_SECONDS, max(remaining, 0)))
                continue

            record = repository.transition(
                task,
                EndpointDispatchStatus.Inflight,
                container_id=target.container_id,
            )
            self._emit_dispatch_lifecycle(stub, record)
            # Bind the task to the container that will serve it, so its record
            # carries the same attribution every other workload kind has.
            task = self.services.tasks.assign(task, container_id=target.container_id)
            started = time.monotonic()
            try:
                response = dispatcher.forward_target(
                    target,
                    request,
                    timeout_seconds=max(remaining, 0.01),
                )
            except EndpointBackendUnreachable:
                # Nothing was written, so this says the target was stale rather than
                # that the endpoint is broken: a container can register a route and
                # then die before anyone dials it. Selecting again is free of replay,
                # and the next pass decides whether any capacity can still arrive.
                time.sleep(min(ENDPOINT_DISPATCH_POLL_INTERVAL_SECONDS, max(remaining, 0)))
                continue
            except Exception as exc:
                # The request left this process, so this attempt is final whatever
                # went wrong and whatever the application already did with it.
                self._observe_dispatch_latencies(stub, record, started=started)
                raise self._forward_failed(target.container_id, exc) from exc
            self._observe_dispatch_latencies(stub, record, started=started)
            return response

    def _wait_for_websocket_target(
        self,
        dispatcher: EndpointRequestDispatcher,
        repository: EndpointDispatchStateRepository,
        stub: StubRecord,
        task: Task,
        *,
        deadline: float,
        max_inflight_per_container: int,
    ) -> EndpointDispatchTarget:
        warmup_attempted = False
        while True:
            self._raise_if_cancelled(task.id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = "Timed out waiting for a backend container"
                raise EndpointDispatchTimedOut(msg)
            if warmup_attempted:
                self._raise_if_capacity_is_dead(dispatcher, stub)

            record = repository.transition(task, EndpointDispatchStatus.WaitingCapacity)
            self._emit_dispatch_lifecycle(stub, record, emit_event=False)
            loads = repository.inflight_counts(stub.id)
            target = dispatcher.select_target(
                stub.id,
                container_loads=loads,
                max_inflight_per_container=max_inflight_per_container,
            )
            if target is None:
                warmup_attempted = self._request_capacity(
                    stub, task, warmup_attempted=warmup_attempted
                )
                time.sleep(min(ENDPOINT_DISPATCH_POLL_INTERVAL_SECONDS, max(remaining, 0)))
                continue

            record = repository.transition(
                task,
                EndpointDispatchStatus.Inflight,
                container_id=target.container_id,
            )
            self._emit_dispatch_lifecycle(stub, record)
            # Bind the task to the container that will serve it, so its record
            # carries the same attribution every other workload kind has.
            task = self.services.tasks.assign(task, container_id=target.container_id)
            self._observe_dispatch_latencies(stub, record)
            return target

    def _raise_if_cancelled(self, task_id: str) -> None:
        try:
            task = self.services.tasks.get(task_id)
        except NotFoundError:
            return
        if task.status is TaskStatus.Cancelled:
            raise EndpointDispatchCancelled("endpoint request cancelled")

    def _fail_task(
        self,
        task: Task,
        *,
        error: str,
        status_code: int,
    ) -> EndpointForwardResponse:
        self.services.tasks.transition(task, TaskStatus.Failed, error=error, exit_code=1)
        response = error_response(status_code, error)
        _add_task_headers(response.headers, task.id)
        return response

    def _finish_timed_out_task(
        self,
        task: Task,
        *,
        error: str,
    ) -> EndpointForwardResponse:
        self.services.tasks.transition(task, TaskStatus.Timeout, error=error, exit_code=1)
        response = error_response(504, error)
        _add_task_headers(response.headers, task.id)
        return response

    def _finish_cancelled_task(
        self,
        task: Task,
        *,
        error: str,
    ) -> EndpointForwardResponse:
        self.services.tasks.transition(task, TaskStatus.Cancelled, error=error, exit_code=1)
        response = error_response(ENDPOINT_CANCELLED_STATUS_CODE, error)
        _add_task_headers(response.headers, task.id)
        return response

    def _emit_dispatch_lifecycle(
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
        level = EventLevel.Info
        if record.status in {
            EndpointDispatchStatus.Failed,
            EndpointDispatchStatus.Timeout,
        }:
            level = EventLevel.Error
        self.services.events.emit(
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

    def _emit_warmup_result(
        self,
        stub: StubRecord,
        task: Task,
        warmup: StartEndpointServeResponse,
    ) -> None:
        self.services.events.emit(
            "endpoint.dispatch.warmup",
            level=EventLevel.Info,
            resource_type="task",
            resource_id=task.id,
            message="endpoint warmup scheduled",
            data={
                "stub_id": stub.id,
                "container_id": warmup.container_id,
            },
            workspace_id=stub.workspace_id,
        )

    def _emit_warmup_failure(
        self,
        stub: StubRecord,
        task: Task,
        error: str,
    ) -> None:
        self.services.events.emit(
            "endpoint.dispatch.warmup",
            level=EventLevel.Error,
            resource_type="task",
            resource_id=task.id,
            message="endpoint warmup failed",
            data={
                "stub_id": stub.id,
                "error": error,
            },
            workspace_id=stub.workspace_id,
        )

    def _record_endpoint_request_usage(
        self,
        stub: StubRecord,
        task: Task,
    ) -> None:
        self.services.usage.record_task_count(
            workspace_id=stub.workspace_id,
            resource_type="endpoint",
            resource_id=stub.id,
            task_id=task.id,
            kind=stub.kind.value,
            app_id=stub.app_id or "",
            deployment_id=stub.deployment_id or "",
        )


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
class EndpointDispatchStateRepository:
    services: ExecutionServices

    def attach(
        self,
        task: Task,
        *,
        stub: StubRecord,
        request: EndpointForwardRequest,
        wait_timeout_seconds: float,
        max_pending_requests: int,
        max_inflight_per_container: int,
    ) -> EndpointDispatchRecord:
        record = EndpointDispatchRecord(
            task_id=task.id,
            stub_id=stub.id,
            workspace_id=stub.workspace_id,
            method=request.method,
            path=request.path,
            wait_timeout_seconds=wait_timeout_seconds,
            max_pending_requests=max_pending_requests,
            max_inflight_per_container=max_inflight_per_container,
            heartbeat_at=utc_now(),
        )
        return self.save(task, record)

    def save(self, task: Task, record: EndpointDispatchRecord) -> EndpointDispatchRecord:
        payload = record.model_dump(mode="json")
        task.kwargs[ENDPOINT_DISPATCH_TASK_KEY] = payload
        self.services.tasks.merge_kwargs(task.id, ENDPOINT_DISPATCH_TASK_KEY, payload)
        return record

    def transition(
        self,
        task: Task,
        status: EndpointDispatchStatus,
        *,
        container_id: str | None = None,
        error: str | None = None,
    ) -> EndpointDispatchRecord:
        record = self.for_task(task)
        if status is EndpointDispatchStatus.Inflight:
            record.attempts += 1
        record.transition(status, container_id=container_id, error=error)
        return self.save(task, record)

    def heartbeat(self, task: Task) -> EndpointDispatchRecord:
        record = self.for_task(task)
        record.transition(record.status)
        return self.save(task, record)

    def for_task(self, task: Task) -> EndpointDispatchRecord:
        try:
            raw = task.kwargs[ENDPOINT_DISPATCH_TASK_KEY]
        except KeyError as exc:
            msg = f"endpoint dispatch state is missing for task {task.id}"
            raise NotFoundError(msg) from exc
        return EndpointDispatchRecord.model_validate(raw)

    def active_count(self, stub_id: str, *, exclude_task_id: str | None = None) -> int:
        return sum(
            1
            for record in self.list_by_stub(stub_id)
            if record.task_id != exclude_task_id
            and record.status in ACTIVE_ENDPOINT_DISPATCH_STATUSES
            and not _dispatch_record_expired(record)
        )

    def inflight_counts(self, stub_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.list_by_stub(stub_id):
            if (
                record.status is EndpointDispatchStatus.Inflight
                and record.container_id is not None
                and not _dispatch_record_expired(record)
            ):
                counts[record.container_id] = counts.get(record.container_id, 0) + 1
        return counts

    def list_by_stub(self, stub_id: str) -> list[EndpointDispatchRecord]:
        records: list[EndpointDispatchRecord] = []
        for task in self.services.tasks.list():
            if ENDPOINT_DISPATCH_TASK_KEY not in task.kwargs:
                continue
            try:
                record = EndpointDispatchRecord.model_validate(
                    task.kwargs[ENDPOINT_DISPATCH_TASK_KEY]
                )
            except ValueError:
                continue
            if record.stub_id == stub_id:
                records.append(record)
        return records


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


def _dispatch_record_expired(record: EndpointDispatchRecord) -> bool:
    if record.status in TERMINAL_ENDPOINT_DISPATCH_STATUSES:
        return False
    deadline = record.heartbeat_at or record.enqueued_at
    return (utc_now() - deadline).total_seconds() > max(record.wait_timeout_seconds, 1.0)


def _endpoint_request_capacity_available(
    repository: EndpointDispatchStateRepository,
    stub: StubRecord,
    settings: EndpointDispatchSettings,
) -> bool:
    return repository.active_count(stub.id) < settings.max_pending_requests


def _forward_response_succeeded(response: EndpointForwardResponse) -> bool:
    return 200 <= response.status_code < 400


def _forward_response_error(response: EndpointForwardResponse) -> str | None:
    if _forward_response_succeeded(response):
        return None
    if response.body:
        return response.body.decode("utf-8", errors="replace")
    return f"HTTP {response.status_code}"


__all__ = ["EndpointControlService"]
