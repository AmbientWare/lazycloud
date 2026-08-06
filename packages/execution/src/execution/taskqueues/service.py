from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from control.service import ControlPlaneService, StubKind
from coordination.redis_client import RedisClient, redis_text
from database.repositories.execution import QueueRepository
from database.repositories.orchestration import ContainerRepository
from foundation.payloads import json_or_base64_payload
from pydantic import JsonValue
from shared.container_requests import WORKER_USER_CODE_VOLUME, WorkerStartupKind
from shared.containers import ContainerRecord, ContainerStatus
from shared.env import (
    APP_ID_ENV,
    CHECKPOINT_ENABLED_ENV,
    GATEWAY_HTTP_URL_ENV,
    HOT_RELOAD_DIR_ENV,
    HOT_RELOAD_ENV,
    LIFECYCLE_HOOKS_ENV,
    STUB_ID_ENV,
    TASK_QUEUE_RETRY_FOR_ENV,
    TASK_QUEUE_SERVE_LOCK_ENV,
    TASK_QUEUE_WORKERS_ENV,
    no_gateway_origin,
)
from shared.errors import ConflictError, InvalidInputError, NotFoundError, UpstreamUnavailableError
from shared.events import EventLevel
from shared.http.taskqueues import (
    StartTaskQueueServeRequest,
    StartTaskQueueServeResponse,
    TaskQueueCompleteBody,
    TaskQueueCompleteResponse,
    TaskQueueMonitorRequest,
    TaskQueueMonitorResponse,
    TaskQueuePopRequest,
    TaskQueuePopResponse,
    TaskQueuePutResponse,
    TaskQueueSerializedInvocation,
    TaskQueueStateResponse,
    TaskQueueTaskMessage,
)
from shared.http.workspace_changes import WorkspaceChangeType
from shared.queue_messages import QueueMessage
from shared.tasks import Task, TaskStatus, is_terminal_task_status
from shared.timestamps import utc_now
from shared.workload_keys import (
    task_queue_keep_warm_lock_key,
    task_queue_processing_lock_key,
    task_queue_running_lock_index_key,
    task_queue_running_lock_key,
)

from execution.checkpoints import latest_available_checkpoint
from execution.containers.planning import ContainerSchedulingOptions
from execution.containers.service import PendingContainerReservation
from execution.mounts import (
    container_resource_mounts,
    container_resource_mounts_require_workspace_storage,
)
from execution.services import ExecutionServices
from execution.taskqueues import planning
from execution.taskqueues.config import TaskQueueStubConfig
from execution.taskqueues.planning import (
    TaskQueueCompletePlan,
    TaskQueueMonitorPlan,
    TaskQueuePopPlan,
    TaskQueueServeRequest,
    plan_task_queue_complete,
    plan_task_queue_monitor,
    plan_task_queue_pop,
    plan_task_queue_put,
    plan_task_queue_serve,
)


@dataclass(frozen=True, slots=True)
class TaskQueuePreemptedResult:
    task_id: str
    container_id: str
    status: TaskStatus
    changed: bool
    retry_scheduled: bool
    terminal: bool
    stale_attempt: bool
    message_released: bool
    message_acknowledged: bool
    locks_cleared: bool
    attempt_number: int
    max_attempts: int


@dataclass(slots=True)
class TaskQueueControlService:
    services: ExecutionServices
    redis: RedisClient
    gateway_http_url: Callable[[], str] = no_gateway_origin
    control_plane: ControlPlaneService = field(init=False)

    def __post_init__(self) -> None:
        self.control_plane = ControlPlaneService(self.services.context)

    def task_queue_put(self, stub_id: str, payload: bytes) -> TaskQueuePutResponse:
        stub = self.control_plane.get_stub(stub_id)
        if stub.kind is not StubKind.TaskQueue:
            raise InvalidInputError(f"stub is not a task queue: {stub.id}")
        if stub.deployment_id:
            try:
                deployment = self.services.deployments.get(stub.deployment_id)
            except NotFoundError as exc:
                raise ConflictError("task queue deployment is deleted") from exc
            if not deployment.active or deployment.deleted_at is not None:
                raise ConflictError("task queue deployment is not active")
        try:
            serialized_invocation = TaskQueueSerializedInvocation.from_bytes(payload)
        except ValueError as exc:
            raise InvalidInputError("task queue invocation payload is empty") from exc
        config = TaskQueueStubConfig.model_validate(stub.config, from_attributes=True)
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        queue_name = self._queue_name(stub_id)
        put_plan = plan_task_queue_put(
            planning.TaskQueuePutRequest(
                body=payload,
                tasks_in_flight=self._queue_depth(queue_name, workspace.id),
                max_pending_tasks=config.effective_max_pending_tasks,
                ttl_seconds=config.task_policy.effective_ttl_seconds,
                parse_payload=False,
            )
        )
        if not put_plan.accepted:
            raise InvalidInputError(put_plan.reason)
        task = self.services.tasks.create(
            f"taskqueue-{stub_id}",
            workspace_id=stub.workspace_id,
            app_id=stub.app_id,
            stub_id=stub.id,
            deployment_id=stub.deployment_id,
            handler=stub.handler,
            retry_policy=config.effective_retry_policy,
        )
        self.services.usage.record_task_count(
            workspace_id=stub.workspace_id,
            resource_type="task_queue",
            resource_id=stub.id,
            task_id=task.id,
            kind=stub.kind.value,
            app_id=stub.app_id or "",
            deployment_id=stub.deployment_id or "",
        )
        message = TaskQueueTaskMessage(
            workspace_name=workspace.name,
            stub_id=stub_id,
            task_id=task.id,
            invocation=serialized_invocation,
        )
        self._publish_message(
            queue_name,
            message,
            workspace.id,
            expires_at=utc_now() + timedelta(seconds=put_plan.ttl_seconds),
        )
        return TaskQueuePutResponse(task_id=task.id)

    def task_queue_pop(self, request: TaskQueuePopRequest) -> TaskQueuePopResponse:
        stub = self.control_plane.get_stub(request.stub_id)
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        queue_name = self._queue_name(request.stub_id)
        while True:
            message = self._consume_message(queue_name, workspace.id)
            if message is None:
                task_message = None
                task = None
            else:
                try:
                    task_message = TaskQueueTaskMessage.model_validate(message.body)
                    task = self._task(task_message.task_id)
                except (KeyError, ValueError, NotFoundError):
                    self._ack_message(message.id, workspace_id=workspace.id)
                    continue
                if _message_expired(message) and task.status in {
                    TaskStatus.Pending,
                    TaskStatus.Retry,
                }:
                    self.services.tasks.transition(
                        task,
                        TaskStatus.Expired,
                        error="task queue item expired before execution",
                    )
                    self._ack_message(message.id, workspace_id=workspace.id)
                    continue
            pop_plan = plan_task_queue_pop(
                planning.TaskQueuePopRequest(
                    workspace_name=workspace.name,
                    stub_id=request.stub_id,
                    container_id=request.container_id,
                    queue_length=self._queue_depth(queue_name, workspace.id),
                    task_message=task_message,
                    task_status=task.status if task is not None else None,
                )
            )
            if pop_plan.cleanup_completed_task and message is not None:
                self._ack_message(message.id, workspace_id=workspace.id)
                continue
            if not pop_plan.claim_task or task_message is None or message is None:
                return TaskQueuePopResponse.from_bytes(b"")
            if task is not None:
                self.services.tasks.start(task.id, container_id=request.container_id)
            self._record_task_claim(pop_plan, container_id=request.container_id)
            return TaskQueuePopResponse.from_bytes(_message_bytes(task_message))

    def task_queue_state(self, stub_id: str) -> TaskQueueStateResponse:
        stub = self.control_plane.get_stub(stub_id)
        if stub.kind is not StubKind.TaskQueue:
            raise InvalidInputError(f"stub is not a task queue: {stub.id}")
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        queue_name = self._queue_name(stub.id)
        now = utc_now()
        with self.services.context.database.session() as session:
            repository = QueueRepository(session)
            queue_depth = repository.queue_depth(queue_name, workspace_id=workspace.id, now=now)
            oldest_pending_at = repository.oldest_pending_at(
                queue_name,
                workspace_id=workspace.id,
                now=now,
            )
        containers = [
            container
            for container in self.services.containers.list(
                workspace_id=workspace.id,
                stub_ids=(stub.id,),
            )
            if container.status is ContainerStatus.Running
        ]
        config = TaskQueueStubConfig.model_validate(stub.config, from_attributes=True)
        active_consumers = len(containers) * config.consumers_per_container
        busy_consumers = min(
            self._running_task_count(workspace.name, stub.id, containers),
            active_consumers,
        )
        return TaskQueueStateResponse(
            queue_depth=queue_depth,
            oldest_pending_at=oldest_pending_at,
            active_consumers=active_consumers,
            busy_consumers=busy_consumers,
            available_consumers=active_consumers - busy_consumers,
        )

    def expire_pending_tasks(self, stub_id: str, *, now: datetime | None = None) -> int:
        stub = self.control_plane.get_stub(stub_id)
        if stub.kind is not StubKind.TaskQueue:
            raise InvalidInputError(f"stub is not a task queue: {stub.id}")
        current_time = now or utc_now()
        queue_name = self._queue_name(stub.id)
        expired = 0
        while True:
            message = self._consume_expired_message(
                queue_name,
                stub.workspace_id,
                now=current_time,
            )
            if message is None:
                return expired
            try:
                task_message = TaskQueueTaskMessage.model_validate(message.body)
                task = self._task(task_message.task_id)
            except (KeyError, ValueError, NotFoundError):
                self._ack_message(message.id, workspace_id=stub.workspace_id)
                continue
            if task.status in {TaskStatus.Pending, TaskStatus.Retry}:
                self.services.tasks.transition(
                    task,
                    TaskStatus.Expired,
                    error="task queue item expired before execution",
                )
                self._ack_message(message.id, workspace_id=stub.workspace_id)
                expired += 1
            elif task.status is not TaskStatus.Running:
                self._ack_message(message.id, workspace_id=stub.workspace_id)

    def task_queue_monitor(self, request: TaskQueueMonitorRequest) -> TaskQueueMonitorResponse:
        task = self._task(request.task_id)
        stub = self.control_plane.get_stub(request.stub_id)
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        claimed = self._task_queue_message(request.stub_id, request.task_id, workspace.id)[0]
        cancellation_requested = task.status is TaskStatus.Cancelled
        timeout_elapsed = task.status is TaskStatus.Timeout
        monitor = plan_task_queue_monitor(
            planning.TaskQueueMonitorRequest(
                workspace_name=workspace.name,
                stub_id=request.stub_id,
                container_id=request.container_id,
                task_id=request.task_id,
                task_status=task.status,
                claimed=claimed is not None,
                cancellation_requested=cancellation_requested,
                timeout_elapsed=timeout_elapsed,
            )
        )
        if monitor.next_status is not None and task.status is not monitor.next_status:
            self.services.tasks.transition(task, monitor.next_status)
        if monitor.complete_dispatcher:
            self._ack_task_message(request.stub_id, request.task_id, workspace.id)
        if monitor.complete_dispatcher or monitor.complete:
            self._clear_running_task(
                monitor.running_lock_key,
                request.container_id,
                request.task_id,
                workspace.name,
                request.stub_id,
            )
        else:
            if claimed is not None:
                self._refresh_message_lease(claimed, workspace_id=workspace.id)
            self._refresh_task_locks(monitor, container_id=request.container_id)
        return TaskQueueMonitorResponse(
            cancelled=monitor.cancelled,
            complete=monitor.complete,
            timed_out=monitor.timed_out,
        )

    def task_queue_preempted(
        self,
        *,
        stub_id: str,
        task_id: str,
        container_id: str,
        exit_code: int | None = None,
        error: str = "task queue container was preempted",
    ) -> TaskQueuePreemptedResult:
        task = self._task(task_id)
        stub = self.control_plane.get_stub(stub_id)
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        if not self._owns_task_claim(
            workspace_name=workspace.name,
            stub_id=stub.id,
            task_id=task.id,
            container_id=container_id,
        ):
            return _task_queue_preempted_result(
                task,
                container_id=container_id,
                stale_attempt=task.status is TaskStatus.Running,
            )

        guard_key = self._redis_key(
            f"taskqueue:{workspace.name}:{stub.id}:task:preemption:{task.id}"
        )
        if not self.redis.set_single_use(guard_key, container_id, ttl_seconds=300):
            current = self._task(task_id)
            return _task_queue_preempted_result(current, container_id=container_id)
        try:
            if not self._owns_task_claim(
                workspace_name=workspace.name,
                stub_id=stub.id,
                task_id=task.id,
                container_id=container_id,
            ):
                current = self._task(task_id)
                return _task_queue_preempted_result(
                    current,
                    container_id=container_id,
                    stale_attempt=current.status is TaskStatus.Running,
                )

            message, task_message = self._task_queue_message(stub.id, task.id, workspace.id)
            if task.status is TaskStatus.Running and (message is None or task_message is None):
                raise NotFoundError(f"task queue message not found: {task.id}")

            outcome = self.services.tasks.finish_with_retry(
                task.id,
                TaskStatus.Failed,
                container_id=container_id,
                error=error,
                exit_code=exit_code,
            )
            released = False
            acknowledged = False
            locks_cleared = False
            if outcome.state_changed and outcome.retry_decision.should_retry:
                if message is None or task_message is None:
                    raise NotFoundError(f"task queue message not found: {task.id}")
                self._release_task_message_for_retry(
                    message,
                    task_message,
                    workspace_id=workspace.id,
                    delay_seconds=outcome.retry_decision.delay_seconds,
                )
                released = True
            elif outcome.task.status is TaskStatus.Retry:
                if message is not None and task_message is not None and message.leased_until:
                    delay_seconds = (
                        max((outcome.task.next_retry_at - utc_now()).total_seconds(), 0.0)
                        if outcome.task.next_retry_at is not None
                        else 0.0
                    )
                    self._release_task_message_for_retry(
                        message,
                        task_message,
                        workspace_id=workspace.id,
                        delay_seconds=delay_seconds,
                    )
                    released = True
            elif is_terminal_task_status(outcome.task.status) and message is not None:
                self._ack_message(message.id, workspace_id=workspace.id)
                acknowledged = True

            if (
                outcome.state_changed
                or outcome.task.status is TaskStatus.Retry
                or is_terminal_task_status(outcome.task.status)
            ):
                self._clear_running_task(
                    task_queue_running_lock_key(
                        workspace.name,
                        stub.id,
                        container_id,
                        task.id,
                    ),
                    container_id,
                    task.id,
                    workspace.name,
                    stub.id,
                )
                locks_cleared = True
            return _task_queue_preempted_result(
                outcome.task,
                container_id=container_id,
                changed=outcome.state_changed,
                retry_scheduled=outcome.task.status is TaskStatus.Retry,
                stale_attempt=(
                    not outcome.state_changed and outcome.task.status is TaskStatus.Running
                ),
                message_released=released,
                message_acknowledged=acknowledged,
                locks_cleared=locks_cleared,
            )
        finally:
            self.redis.delete(guard_key)

    def task_queue_complete(
        self,
        request: TaskQueueCompleteBody,
    ) -> TaskQueueCompleteResponse:
        task = self._task(request.task_id)
        stub = self.control_plane.get_stub(request.stub_id)
        config = TaskQueueStubConfig.model_validate(stub.config, from_attributes=True)
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        message, task_message = self._task_queue_message(
            request.stub_id,
            request.task_id,
            workspace.id,
        )
        task_result = request.bytes_value()
        retry_policy = task.retry_policy or config.effective_retry_policy
        retry_count = max(task.attempt_number - 1, 0)
        complete_plan = plan_task_queue_complete(
            planning.TaskQueueCompleteRequest(
                workspace_name=workspace.name,
                stub_id=request.stub_id,
                container_id=request.container_id,
                task_id=request.task_id,
                task_status=request.task_status,
                task_duration_ms=request.task_duration * 1000,
                keep_warm_seconds=int(request.keep_warm_seconds),
                result=task_result,
                error=request.error,
                retry_count=retry_count,
                retry_limit=retry_policy.retry_count,
                retry_delay_seconds=retry_policy.delay_seconds,
            )
        )
        result = (
            json_or_base64_payload(task_result) if task_result and complete_plan.terminal else None
        )
        error = complete_plan.retry_message or request.error or None
        updated = self.services.tasks.transition(
            task,
            complete_plan.final_status,
            result=result,
            error=error,
        )
        if complete_plan.retry and not complete_plan.retry_limit_exceeded:
            if message is None or task_message is None:
                raise NotFoundError(f"task queue message not found: {request.task_id}")
            self._release_task_message_for_retry(
                message,
                task_message,
                workspace_id=workspace.id,
                delay_seconds=complete_plan.retry_delay_seconds,
            )
        else:
            self._ack_task_message(request.stub_id, request.task_id, workspace.id)
        self._record_task_completion(complete_plan, int(request.keep_warm_seconds))
        return TaskQueueCompleteResponse(
            message=complete_plan.action.value,
            task_status=request.task_status,
            final_status=updated.status,
            retry_scheduled=updated.status is TaskStatus.Retry,
            attempt_number=updated.attempt_number,
            max_attempts=updated.max_attempts,
        )

    def start_task_queue_serve(
        self,
        request: StartTaskQueueServeRequest,
    ) -> StartTaskQueueServeResponse:
        stub = self.control_plane.get_stub(request.stub_id)
        if stub.kind is not StubKind.TaskQueue:
            raise InvalidInputError(f"stub is not a task queue: {stub.id}")
        config = TaskQueueStubConfig.model_validate(stub.config, from_attributes=True)
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        plan = plan_task_queue_serve(
            TaskQueueServeRequest(
                stub_id=stub.id,
                workspace_name=workspace.name,
                timeout_seconds=request.timeout,
                python_executable=config.image.python_executable,
            )
        )
        if not plan.authorized:
            raise InvalidInputError("Unauthorized")
        image_id = config.effective_image_id
        retry_for_json = json.dumps(list(config.effective_retry_policy.retry_for))
        env = {
            **config.env,
            APP_ID_ENV: stub.app_id or "",
            CHECKPOINT_ENABLED_ENV: str(config.runtime.checkpoint_enabled).lower(),
            STUB_ID_ENV: stub.id,
            "HANDLER": stub.handler or "",
            TASK_QUEUE_SERVE_LOCK_ENV: plan.serve_lock_key,
            TASK_QUEUE_RETRY_FOR_ENV: retry_for_json,
            LIFECYCLE_HOOKS_ENV: config.lifecycle_hooks.model_dump_json(),
            HOT_RELOAD_ENV: "true",
            HOT_RELOAD_DIR_ENV: WORKER_USER_CODE_VOLUME,
            "KEEP_WARM_SECONDS": str(config.effective_keep_warm_seconds),
            TASK_QUEUE_WORKERS_ENV: str(config.consumers_per_container),
        }
        _add_gateway_http_env(env, self.gateway_http_url())
        env_payload: dict[str, JsonValue] = {name: value for name, value in env.items()}
        secret_names = config.secrets
        with self.services.context.database.session() as session:
            container = self.services.containers.reserve_pending(
                session,
                PendingContainerReservation(
                    name=f"taskqueue-{stub.name}",
                    image=image_id,
                    command=list(plan.entrypoint),
                    workspace_id=stub.workspace_id,
                    stub_id=stub.id,
                    app_id=stub.app_id,
                    env={name: str(value) for name, value in env_payload.items()},
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
                workspace_name=plan.request.workspace_name,
                stub_type="taskqueue",
                startup_kind=WorkerStartupKind.TaskQueue,
                entrypoint=plan.entrypoint,
                cwd=WORKER_USER_CODE_VOLUME,
                env_list=[f"{name}={value}" for name, value in env.items()],
                image_id=image_id,
                app_id=stub.app_id or "",
                deployment_id=stub.deployment_id or "",
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
                pool_selector=config.runtime.pool_selector or "",
                runtime=config.runtime.runtime,
                runtime_class=config.runtime.runtime_class or "",
                docker_enabled=config.runtime.docker_enabled,
                preemptible=config.runtime.preemptible,
                gpu_limit=config.runtime.gpu_limit,
                cpu_limit_millicores=config.runtime.cpu_limit_millicores,
                secret_names=secret_names,
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
                scheduled.reason or "task queue container scheduling failed"
            )
        self._set_serve_lock(
            plan.serve_lock_key,
            container.id,
            plan.serve_lock_ttl_seconds,
        )
        self._set_keep_warm_lock(
            workspace.name,
            stub.id,
            container.id,
            config.effective_keep_warm_seconds,
        )
        self.services.events.emit(
            "taskqueue.serve.started",
            level=EventLevel.Info,
            resource_type="container",
            resource_id=container.id,
            message=f"started task queue serve container for {stub.name}",
            data={
                "stub_id": stub.id,
                "serve_lock_key": plan.serve_lock_key,
                "timeout_seconds": plan.wait_timeout_seconds,
            },
            workspace_id=stub.workspace_id,
        )
        return StartTaskQueueServeResponse(container_id=container.id)

    def _task(self, task_id: str) -> Task:
        return self.services.tasks.get(task_id)

    def _queue_name(self, stub_id: str) -> str:
        return f"taskqueue:{stub_id}"

    def _publish_message(
        self,
        queue_name: str,
        message: TaskQueueTaskMessage,
        workspace_id: str,
        *,
        expires_at: datetime | None = None,
    ) -> QueueMessage:
        with self.services.context.database.session() as session:
            repository = QueueRepository(session)
            return repository.messages.create(
                {
                    "queue": queue_name,
                    "body": message.model_dump(mode="json"),
                    "attempts": 0,
                    "available_at": utc_now().isoformat(),
                    "expires_at": expires_at,
                },
                workspace_id=workspace_id,
            )

    def _consume_message(
        self,
        queue_name: str,
        workspace_id: str,
        *,
        lease_seconds: int = 60,
    ) -> QueueMessage | None:
        now = utc_now()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self.services.context.database.session() as session:
            return QueueRepository(session).claim_available_message(
                queue_name,
                workspace_id=workspace_id,
                now=now,
                lease_until=lease_until,
            )

    def _consume_expired_message(
        self,
        queue_name: str,
        workspace_id: str,
        *,
        now: datetime,
        lease_seconds: int = 60,
    ) -> QueueMessage | None:
        lease_until = now + timedelta(seconds=lease_seconds)
        with self.services.context.database.session() as session:
            return QueueRepository(session).claim_expired_message(
                queue_name,
                workspace_id=workspace_id,
                now=now,
                lease_until=lease_until,
            )

    def _queue_depth(self, queue_name: str, workspace_id: str) -> int:
        with self.services.context.database.session() as session:
            return QueueRepository(session).queue_depth(
                queue_name,
                workspace_id=workspace_id,
                now=utc_now(),
            )

    def _running_task_count(
        self,
        workspace_name: str,
        stub_id: str,
        containers: list[ContainerRecord],
    ) -> int:
        count = 0
        for container in containers:
            index_key = self._redis_key(
                task_queue_running_lock_index_key(workspace_name, stub_id, container.id)
            )
            for task_id_value in self.redis.set_members(index_key):
                task_id = redis_text(task_id_value)
                lock_key = self._redis_key(
                    task_queue_running_lock_key(
                        workspace_name,
                        stub_id,
                        container.id,
                        task_id,
                    )
                )
                if int(self.redis.exists(lock_key) or 0) > 0:
                    count += 1
        return count

    def _ack_message(self, message_id: str, *, workspace_id: str) -> None:
        with self.services.context.database.session() as session:
            QueueRepository(session).messages.delete(message_id, workspace_id=workspace_id)

    def _ack_task_message(self, stub_id: str, task_id: str, workspace_id: str) -> None:
        message, _task_message = self._task_queue_message(stub_id, task_id, workspace_id)
        if message is not None:
            self._ack_message(message.id, workspace_id=workspace_id)

    def _release_task_message_for_retry(
        self,
        message: QueueMessage,
        task_message: TaskQueueTaskMessage,
        *,
        workspace_id: str,
        delay_seconds: float,
    ) -> None:
        message.body = task_message.model_dump(mode="json")
        message.leased_until = None
        message.available_at = utc_now() + timedelta(seconds=delay_seconds)
        with self.services.context.database.session() as session:
            QueueRepository(session).upsert_message(message, workspace_id=workspace_id)

    def _refresh_message_lease(
        self,
        message: QueueMessage,
        *,
        workspace_id: str,
        lease_seconds: int = 60,
    ) -> None:
        with self.services.context.database.session() as session:
            QueueRepository(session).renew_message_lease(
                message.id,
                workspace_id=workspace_id,
                lease_until=utc_now() + timedelta(seconds=lease_seconds),
            )

    def _task_queue_message(
        self,
        stub_id: str,
        task_id: str,
        workspace_id: str,
    ) -> tuple[QueueMessage | None, TaskQueueTaskMessage | None]:
        queue_name = self._queue_name(stub_id)
        with self.services.context.database.session() as session:
            repository = QueueRepository(session)
            messages = repository.messages.list(workspace_id=workspace_id)
            for message in messages:
                if message.queue != queue_name:
                    continue
                try:
                    body = TaskQueueTaskMessage.model_validate(message.body)
                except ValueError:
                    continue
                if body.task_id == task_id:
                    return message, body
        return None, None

    def _record_task_claim(self, pop_plan: TaskQueuePopPlan, *, container_id: str) -> None:
        if (
            pop_plan.running_lock_index_key is None
            or pop_plan.running_lock_key is None
            or pop_plan.heartbeat_key is None
            or pop_plan.task_message is None
        ):
            return
        self.redis.set(
            self._redis_key(pop_plan.processing_lock_key),
            pop_plan.task_message.task_id,
            ex=pop_plan.running_lock_ttl_seconds,
        )
        index_key = self._redis_key(pop_plan.running_lock_index_key)
        self.redis.set_add(index_key, pop_plan.task_message.task_id)
        self.redis.expire(index_key, pop_plan.running_lock_ttl_seconds * 2)
        self.redis.set(
            self._redis_key(pop_plan.running_lock_key),
            "1",
            ex=pop_plan.running_lock_ttl_seconds,
        )
        self.redis.set(
            self._redis_key(pop_plan.heartbeat_key),
            container_id,
            ex=pop_plan.heartbeat_ttl_seconds,
        )

    def _refresh_task_locks(
        self,
        monitor: TaskQueueMonitorPlan,
        *,
        container_id: str,
    ) -> None:
        self.redis.set(
            self._redis_key(monitor.heartbeat_key),
            container_id,
            ex=monitor.heartbeat_ttl_seconds,
        )
        self.redis.set(
            self._redis_key(monitor.running_lock_key),
            "1",
            ex=monitor.running_lock_ttl_seconds,
        )

    def _record_task_completion(
        self,
        complete_plan: TaskQueueCompletePlan,
        keep_warm_seconds: int,
    ) -> None:
        self.redis.delete(self._redis_key(complete_plan.running_lock_key))
        self.redis.set_remove(
            self._redis_key(complete_plan.running_lock_index_key),
            _task_id_from_running_lock_key(complete_plan.running_lock_key),
        )
        processing_lock_key = _processing_lock_key_from_running_lock_key(
            complete_plan.running_lock_key
        )
        if processing_lock_key:
            self.redis.delete(self._redis_key(processing_lock_key))
        if complete_plan.keep_warm_lock_key is not None and keep_warm_seconds > 0:
            self.redis.set(
                self._redis_key(complete_plan.keep_warm_lock_key),
                "1",
                ex=keep_warm_seconds,
            )

    def _clear_running_task(
        self,
        running_lock_key: str,
        container_id: str,
        task_id: str,
        workspace_name: str,
        stub_id: str,
    ) -> None:
        self.redis.delete(self._redis_key(running_lock_key))
        self.redis.set_remove(
            self._redis_key(
                task_queue_running_lock_index_key(workspace_name, stub_id, container_id)
            ),
            task_id,
        )
        self.redis.delete(
            self._redis_key(task_queue_processing_lock_key(workspace_name, stub_id, container_id))
        )
        self.redis.delete(
            self._redis_key(
                planning.task_queue_task_heartbeat_key(workspace_name, stub_id, task_id)
            )
        )

    def _owns_task_claim(
        self,
        *,
        workspace_name: str,
        stub_id: str,
        task_id: str,
        container_id: str,
    ) -> bool:
        heartbeat = self.redis.get(
            self._redis_key(
                planning.task_queue_task_heartbeat_key(workspace_name, stub_id, task_id)
            )
        )
        processing = self.redis.get(
            self._redis_key(task_queue_processing_lock_key(workspace_name, stub_id, container_id))
        )
        running_lock = self._redis_key(
            task_queue_running_lock_key(workspace_name, stub_id, container_id, task_id)
        )
        return (
            heartbeat is not None
            and redis_text(heartbeat) == container_id
            and processing is not None
            and redis_text(processing) == task_id
            and self.redis.exists(running_lock)
        )

    def _set_keep_warm_lock(
        self,
        workspace_name: str,
        stub_id: str,
        container_id: str,
        keep_warm_seconds: int,
    ) -> None:
        if keep_warm_seconds <= 0:
            return
        self.redis.set(
            self._redis_key(task_queue_keep_warm_lock_key(workspace_name, stub_id, container_id)),
            "1",
            ex=keep_warm_seconds,
        )

    def _set_serve_lock(
        self,
        lock_key: str,
        container_id: str,
        ttl_seconds: int,
    ) -> None:
        self.redis.set(self._redis_key(lock_key), container_id, ex=max(1, ttl_seconds))

    def _redis_key(self, logical_key: str) -> str:
        return self.redis.key(logical_key)


def _message_bytes(message: TaskQueueTaskMessage) -> bytes:
    return message.model_dump_json().encode("utf-8")


def _add_gateway_http_env(env: dict[str, str], gateway_http_url: str) -> None:
    if env.get(GATEWAY_HTTP_URL_ENV):
        return
    if gateway_http_url:
        env[GATEWAY_HTTP_URL_ENV] = gateway_http_url


def _message_expired(message: QueueMessage, *, now: datetime | None = None) -> bool:
    return message.expires_at is not None and message.expires_at <= (now or utc_now())


def _task_id_from_running_lock_key(running_lock_key: str) -> str:
    parts = running_lock_key.split(":")
    return parts[5] if len(parts) >= 6 else ""


def _processing_lock_key_from_running_lock_key(running_lock_key: str) -> str:
    parts = running_lock_key.split(":")
    if len(parts) < 6 or parts[0] != "taskqueue" or parts[3] != "task_running":
        return ""
    workspace_name, stub_id, container_id = parts[1], parts[2], parts[4]
    return task_queue_processing_lock_key(workspace_name, stub_id, container_id)


def _task_queue_preempted_result(
    task: Task,
    *,
    container_id: str,
    changed: bool = False,
    retry_scheduled: bool | None = None,
    stale_attempt: bool = False,
    message_released: bool = False,
    message_acknowledged: bool = False,
    locks_cleared: bool = False,
) -> TaskQueuePreemptedResult:
    return TaskQueuePreemptedResult(
        task_id=task.id,
        container_id=container_id,
        status=task.status,
        changed=changed,
        retry_scheduled=(
            task.status is TaskStatus.Retry if retry_scheduled is None else retry_scheduled
        ),
        terminal=is_terminal_task_status(task.status),
        stale_attempt=stale_attempt,
        message_released=message_released,
        message_acknowledged=message_acknowledged,
        locks_cleared=locks_cleared,
        attempt_number=task.attempt_number,
        max_attempts=task.max_attempts,
    )


__all__ = ["TaskQueueControlService", "TaskQueuePreemptedResult"]
