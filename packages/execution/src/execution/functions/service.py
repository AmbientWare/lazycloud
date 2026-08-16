from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from control.service import ControlPlaneService, StubKind
from database.repositories.execution import (
    TaskAttemptRepository,
    TaskDependencyRepository,
    TaskRepository,
)
from database.repositories.orchestration import ContainerRepository
from pydantic import JsonValue
from shared.app_identity import FUNCTION_IMAGE
from shared.container_requests import (
    WORKER_USER_CODE_VOLUME,
    WorkerStartupKind,
)
from shared.containers import ContainerRecord, ContainerStatus
from shared.env import GATEWAY_HTTP_URL_ENV, no_gateway_origin
from shared.errors import (
    CapacityLimitReachedError,
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    PaymentRequiredError,
)
from shared.events import EventLevel
from shared.function_payloads import (
    FunctionDependencyBinding,
    FunctionInvocationPayload,
    FunctionJsonInvocation,
    FunctionResultPayload,
    function_result_payload_size,
    validate_function_dependency_bindings,
)
from shared.http.functions import (
    FunctionCallGraphNode,
    FunctionCallGraphResponse,
    FunctionCronRequest,
    FunctionCronResponse,
    FunctionGetArgsRequest,
    FunctionGetArgsResponse,
    FunctionInvokeBody,
    FunctionInvokeResponse,
    FunctionMonitorRequest,
    FunctionMonitorResponse,
    FunctionSetResultBody,
    FunctionSetResultResponse,
)
from shared.http.workspace_changes import WorkspaceChangeType
from shared.tasks import Task, TaskDependency, TaskStatus, is_terminal_task_status

from execution.config import env_sequence_mapping
from execution.containers.planning import ContainerSchedulingOptions
from execution.containers.service import PendingContainerReservation
from execution.functions import planning
from execution.functions.config import FunctionStubConfig
from execution.functions.planning import (
    FunctionContainerStartRequest,
    FunctionTaskCancellationReason,
    function_cancellation_decision,
    plan_function_container_start,
    plan_function_cron,
    plan_function_invoke,
    plan_function_monitor,
)
from execution.mounts import (
    container_resource_mounts,
    container_resource_mounts_require_workspace_storage,
)
from execution.services import ExecutionServices, SchedulerSubmissionResult

LOGGER = logging.getLogger(__name__)

FUNCTION_LIKE_STUB_KINDS = {StubKind.Function, StubKind.CronJob}


@dataclass(slots=True)
class FunctionControlService:
    services: ExecutionServices
    gateway_http_url: Callable[[], str] = no_gateway_origin
    control_plane: ControlPlaneService = field(init=False)

    def __post_init__(self) -> None:
        self.control_plane = ControlPlaneService(self.services.context)

    def function_invoke(self, request: FunctionInvokeBody) -> FunctionInvokeResponse:
        try:
            stub = self.control_plane.get_stub(request.stub_id)
            if stub.kind not in FUNCTION_LIKE_STUB_KINDS:
                return FunctionInvokeResponse.from_result(
                    task_id="",
                    output=f"stub is not a function: {stub.id}",
                    done=True,
                    exit_code=1,
                )
            # Before the task row, because everything after this commits: a
            # refusal taken later leaves a task queued forever for an account
            # nothing will schedule.
            with self.services.context.database.session() as session:
                self.services.containers.assert_may_start_container(
                    session, workspace_id=stub.workspace_id
                )
            config = FunctionStubConfig.model_validate(stub.config, from_attributes=True)
            retry_policy = config.effective_retry_policy
            invoke_plan = plan_function_invoke(
                planning.FunctionInvokeRequest(
                    invocation=request.invocation,
                    headless=request.headless,
                    task_ttl_seconds=config.runtime.task_ttl_seconds,
                    configured_retry_count=retry_policy.retry_count,
                )
            )
            parent_task_id, root_task_id = self._task_graph_context(request, stub.workspace_id)
            task_args, task_kwargs = _persisted_invocation_arguments(invoke_plan.invocation)
            task = self.services.tasks.create(
                f"function-{stub.name}",
                workspace_id=stub.workspace_id,
                app_id=stub.app_id,
                stub_id=stub.id,
                deployment_id=stub.deployment_id,
                parent_task_id=parent_task_id or None,
                root_task_id=root_task_id or None,
                handler=stub.handler,
                args=task_args,
                kwargs=task_kwargs,
                invocation=invoke_plan.invocation,
                retry_policy=retry_policy,
            )
            if not task.root_task_id:
                task.root_task_id = task.id
                task = self.services.tasks.save(task)
                self.services.tasks.publish_lifecycle_change(
                    task,
                    WorkspaceChangeType.Updated,
                )
            self.services.usage.record_task_count(
                workspace_id=stub.workspace_id,
                resource_type="function",
                resource_id=stub.id,
                task_id=task.id,
                kind=stub.kind.value,
                app_id=stub.app_id or "",
                deployment_id=stub.deployment_id or "",
            )
            dependencies = self._create_task_dependencies(task, request)
            scheduled = self._try_schedule_waiting_task(task.id)
            if scheduled is not None and not scheduled.accepted:
                return FunctionInvokeResponse.from_result(
                    task_id=task.id,
                    output=scheduled.reason or "function container scheduling failed",
                    done=True,
                    exit_code=1,
                )
            self.services.events.emit(
                "function.invoked",
                level=EventLevel.Info,
                resource_type="task",
                resource_id=task.id,
                message=f"invoked function {stub.name}",
                data={
                    "stub_id": stub.id,
                    "parent_task_id": task.parent_task_id or "",
                    "root_task_id": task.root_task_id or task.id,
                    "dependency_count": len(dependencies),
                    "task_ttl_seconds": invoke_plan.task_ttl_seconds,
                    "headless": invoke_plan.headless,
                },
                workspace_id=stub.workspace_id,
            )
            return FunctionInvokeResponse.from_result(task_id=task.id)
        except (PaymentRequiredError, CapacityLimitReachedError):
            # Told to the caller as a refusal rather than folded into a result.
            # Everything else here is something that went wrong while running
            # their code, which a failed task describes; these are the platform
            # declining to run it at all, and there is no task to describe them.
            # Reported as a failed invocation, an account at its container limit
            # reads as a bug in the code it never ran.
            raise
        except Exception as exc:
            return FunctionInvokeResponse.from_result(
                task_id="",
                output=str(exc),
                done=True,
                exit_code=1,
            )

    def _task_graph_context(
        self,
        request: FunctionInvokeBody,
        workspace_id: str,
    ) -> tuple[str, str]:
        upstream_tasks = [
            self._workspace_task(dependency.task_id, workspace_id)
            for dependency in request.dependencies
        ]
        parent_task_id = request.parent_task_id.strip()
        parent_task = None
        if parent_task_id:
            parent_task = self._workspace_task(parent_task_id, workspace_id)
        elif upstream_tasks:
            parent_task = upstream_tasks[0]
            parent_task_id = parent_task.id

        root_task_id = request.root_task_id.strip()
        if root_task_id:
            self._workspace_task(root_task_id, workspace_id)
        elif parent_task is not None:
            root_task_id = parent_task.root_task_id or parent_task.id
        elif upstream_tasks:
            root_task_id = upstream_tasks[0].root_task_id or upstream_tasks[0].id
        return parent_task_id, root_task_id

    def _workspace_task(self, task_id: str, workspace_id: str) -> Task:
        task = self.services.tasks.get(task_id)
        if task.workspace_id != workspace_id:
            msg = f"task not found: {task_id}"
            raise NotFoundError(msg)
        return task

    def _create_task_dependencies(
        self,
        task: Task,
        request: FunctionInvokeBody,
    ) -> list[TaskDependency]:
        dependencies: list[TaskDependency] = []
        seen: set[str] = set()
        with self.services.context.database.session() as session:
            repository = TaskDependencyRepository(session)
            for dependency in request.dependencies:
                upstream_task_id = dependency.task_id.strip()
                if not upstream_task_id or upstream_task_id in seen:
                    continue
                upstream = self._workspace_task(upstream_task_id, task.workspace_id or "")
                seen.add(upstream_task_id)
                dependencies.append(
                    repository.create(
                        TaskDependency(
                            workspace_id=task.workspace_id,
                            task_id=task.id,
                            upstream_task_id=upstream.id,
                            parent_task_id=task.parent_task_id,
                            root_task_id=task.root_task_id or task.id,
                            edge_type=dependency.edge_type or "argument",
                        )
                    )
                )
        return dependencies

    def _try_schedule_waiting_task(
        self,
        task_id: str,
        *,
        seen: set[str] | None = None,
    ) -> SchedulerSubmissionResult | None:
        visited = seen or set()
        if task_id in visited:
            return None
        visited.add(task_id)
        failed_upstream: Task | None = None
        with self.services.context.database.session() as session:
            task_repository = TaskRepository(session)
            task = task_repository.get_for_update_across_workspaces(task_id)
            if task is None:
                raise NotFoundError(f"task not found: {task_id}")
            if task.container_id or is_terminal_task_status(task.status):
                return None
            dependencies = TaskDependencyRepository(session).list_for_task(task.id)
            upstream_tasks: list[Task] = []
            for dependency in dependencies:
                upstream = task_repository.get_across_workspaces(dependency.upstream_task_id)
                if upstream is None or upstream.workspace_id != task.workspace_id:
                    raise InvalidInputError(
                        f"function dependency task not found: {dependency.upstream_task_id}"
                    )
                upstream_tasks.append(upstream)
            failed_upstream = next(
                (
                    upstream
                    for upstream in upstream_tasks
                    if is_terminal_task_status(upstream.status)
                    and upstream.status is not TaskStatus.Complete
                ),
                None,
            )
            if failed_upstream is None and all(
                upstream.status is TaskStatus.Complete for upstream in upstream_tasks
            ):
                bindings: list[FunctionDependencyBinding] = []
                for dependency, upstream in zip(dependencies, upstream_tasks, strict=True):
                    if upstream.function_result is None:
                        raise InvalidInputError(
                            f"function dependency task {upstream.id} has no typed result"
                        )
                    bindings.append(
                        FunctionDependencyBinding(
                            task_id=dependency.upstream_task_id,
                            result=upstream.function_result,
                        )
                    )
                validate_function_dependency_bindings(bindings)
                if task.dependency_bindings and task.dependency_bindings != bindings:
                    raise ConflictError(
                        f"function dependency bindings are immutable for task {task.id}"
                    )
                if not task.dependency_bindings and bindings:
                    task.dependency_bindings = bindings
                    task = task_repository.upsert(task)

        if failed_upstream is not None:
            updated = self.services.tasks.transition(
                task,
                TaskStatus.Failed,
                error=(
                    f"upstream task {failed_upstream.id} finished with status "
                    f"{failed_upstream.status.value}"
                ),
                exit_code=1,
            )
            self.release_dependents(updated, seen=visited)
            return None
        if any(upstream.status is not TaskStatus.Complete for upstream in upstream_tasks):
            return None
        return self._schedule_function_task(task)

    def _schedule_function_task(
        self,
        task: Task,
        *,
        eligible_at: datetime | None = None,
    ) -> SchedulerSubmissionResult | None:
        if not task.stub_id:
            self.services.tasks.transition(
                task,
                TaskStatus.Failed,
                error="function task is missing stub_id",
                exit_code=1,
            )
            return None
        stub = self.control_plane.get_stub(task.stub_id)
        if not self._cron_execution_allowed(task, stub_kind=stub.kind):
            return None
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        config = FunctionStubConfig.model_validate(stub.config, from_attributes=True)
        if task.invocation is None:
            self.services.tasks.transition(
                task,
                TaskStatus.Failed,
                error="function task is missing its invocation payload",
                exit_code=1,
            )
            return None

        container_id = str(uuid4())
        container_plan = plan_function_container_start(
            FunctionContainerStartRequest(
                workspace_name=workspace.name,
                workspace_id=stub.workspace_id,
                app_id=stub.app_id or "",
                stub_id=stub.id,
                task_id=task.id,
                root_task_id=task.root_task_id or task.id,
                handler=stub.handler or "",
                container_id=container_id,
                python_executable=config.image.python_executable,
                cpu_millicores=config.runtime.requested_cpu_millicores,
                memory_mib=config.runtime.requested_memory_mib,
                disk_mib=config.runtime.requested_disk_mib,
                requires_gpu=config.runtime.gpu_required,
                gpu_count=config.runtime.gpu_count,
                image_id=config.effective_image_id,
                env=_function_runtime_env(config.env_list, self.gateway_http_url()),
                secret_env=[],
                lifecycle_hooks=config.lifecycle_hooks,
            )
        )
        reservation = self._reserve_function_container(
            task,
            container_plan=container_plan,
            stub_name=stub.name,
            stub_workspace_id=stub.workspace_id,
            stub_app_id=stub.app_id,
            eligible_at=eligible_at,
        )
        if reservation is None:
            return None
        task, container = reservation
        self.services.tasks.publish_lifecycle_change(
            task,
            WorkspaceChangeType.Updated,
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
        scheduled = self.services.containers.submit_scheduler_request(
            container,
            ContainerSchedulingOptions(
                workspace_name=workspace.name,
                stub_type="function",
                startup_kind=WorkerStartupKind.Function,
                entrypoint=container_plan.entrypoint,
                cwd=WORKER_USER_CODE_VOLUME,
                env_list=container_plan.env,
                image_id=container_plan.image_id or FUNCTION_IMAGE,
                app_id=stub.app_id or "",
                deployment_id=stub.deployment_id or "",
                cpu_millicores=container_plan.cpu_millicores,
                memory_mib=container_plan.memory_mib,
                disk_mib=container_plan.disk_mib,
                gpu_type=config.runtime.requested_gpu_type,
                gpu_request=container_plan.gpu_request,
                gpu_count=container_plan.gpu_count,
                pool_selector=config.runtime.pool_selector or "",
                runtime=config.runtime.runtime,
                runtime_class=config.runtime.runtime_class or "",
                docker_enabled=config.runtime.docker_enabled,
                preemptible=config.runtime.preemptible,
                gpu_limit=config.runtime.gpu_limit,
                cpu_limit_millicores=config.runtime.cpu_limit_millicores,
                secret_names=config.secrets,
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
            updated = self.services.tasks.transition(
                task,
                TaskStatus.Failed,
                error=scheduled.reason,
                exit_code=1,
            )
            self.release_dependents(updated)
        return scheduled

    def _cron_execution_allowed(self, task: Task, *, stub_kind: StubKind) -> bool:
        if stub_kind is not StubKind.CronJob:
            return True
        if not task.deployment_id:
            updated = self.services.tasks.transition(
                task,
                TaskStatus.Failed,
                error="cron task is missing deployment_id",
                exit_code=1,
            )
            self.release_dependents(updated)
            return False
        try:
            deployment = self.services.deployments.get(task.deployment_id)
        except NotFoundError:
            deployment = None
        if deployment is not None and deployment.active and deployment.deleted_at is None:
            return True
        reason = "cron deployment is unavailable"
        if deployment is not None:
            reason = (
                "cron deployment is deleted"
                if deployment.deleted_at is not None
                else "cron deployment is inactive"
            )
        updated = self.services.tasks.transition(
            task,
            TaskStatus.Cancelled,
            error=reason,
            exit_code=1,
        )
        self.release_dependents(updated)
        return False

    def finish_function_task(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        container_id: str = "",
        result: JsonValue = None,
        error: str | None = None,
        exit_code: int | None = None,
    ) -> Task:
        outcome = self.services.tasks.finish_with_retry(
            task_id,
            status,
            container_id=container_id or None,
            result=result,
            error=error,
            exit_code=exit_code,
        )
        if outcome.retry_scheduling_owned and outcome.retry_decision.delay_seconds <= 0:
            self._schedule_function_task(outcome.task)
        if outcome.state_changed and is_terminal_task_status(outcome.task.status):
            self.release_dependents(outcome.task)
        return outcome.task

    def cancel_task(
        self,
        task_id: str,
        *,
        reason: FunctionTaskCancellationReason = (FunctionTaskCancellationReason.RequestCancelled),
    ) -> Task:
        current = self.services.tasks.get(task_id)
        decision = function_cancellation_decision(
            current.status,
            reason,
            container_id=current.container_id or "",
        )
        if not decision.should_update:
            return current
        updated = self.services.tasks.transition(
            current,
            decision.next_status,
            error=reason.value,
            exit_code=1,
        )
        if decision.should_stop_container and current.container_id:
            self.services.containers.stop(current.container_id)
        self.release_dependents(updated)
        return updated

    def schedule_due_retries(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[Task]:
        scheduled: list[Task] = []
        current = now or datetime.now(UTC)
        for task in self.services.tasks.due_retry_tasks(now=current, limit=limit):
            if task.stub_id:
                stub = self.control_plane.get_stub(task.stub_id)
                if stub.kind not in FUNCTION_LIKE_STUB_KINDS:
                    continue
            try:
                result = self._schedule_function_task(task, eligible_at=current)
            except DomainError:
                # One task's refusal is its own. This sweep runs inside the
                # scheduler's pass, so an account that cannot be scheduled would
                # otherwise stop every later step of it — including the billing
                # that is the only thing able to make that account schedulable
                # again.
                LOGGER.exception("scheduling retry for task %s failed", task.id)
                continue
            if result is not None:
                scheduled.append(self.services.tasks.get(task.id))
        return scheduled

    def _reserve_function_container(
        self,
        task: Task,
        *,
        container_plan: planning.FunctionContainerStartPlan,
        stub_name: str,
        stub_workspace_id: str,
        stub_app_id: str | None,
        eligible_at: datetime | None,
    ) -> tuple[Task, ContainerRecord] | None:
        rejected: Task | None = None
        reserved: Task | None = None
        container: ContainerRecord | None = None
        with self.services.context.database.session() as session:
            task_repository = TaskRepository(session)
            current = task_repository.get_for_update_across_workspaces(task.id)
            if current is None or is_terminal_task_status(current.status):
                return None
            if current.status not in {TaskStatus.Pending, TaskStatus.Retry}:
                return None
            if current.container_id:
                return None
            if current.status is TaskStatus.Retry:
                latest_attempt = TaskAttemptRepository(session).latest_for_task(current.id)
                if latest_attempt is None or latest_attempt.container_id is None:
                    return None
                previous_container = ContainerRepository(session).get_across_workspaces(
                    latest_attempt.container_id
                )
                if previous_container is None or previous_container.status not in {
                    ContainerStatus.Exited,
                    ContainerStatus.Failed,
                    ContainerStatus.Stopped,
                }:
                    return None
            if (
                current.status is TaskStatus.Retry
                and current.next_retry_at is not None
                and (eligible_at or datetime.now(UTC)) < current.next_retry_at
            ):
                return None
            try:
                container = self.services.containers.reserve_pending(
                    session,
                    PendingContainerReservation(
                        id=container_plan.container_id,
                        name=f"function-{stub_name}",
                        image=container_plan.image_id or FUNCTION_IMAGE,
                        command=list(container_plan.entrypoint),
                        workspace_id=stub_workspace_id,
                        stub_id=current.stub_id,
                        app_id=stub_app_id,
                        task_id=current.id,
                        env=env_sequence_mapping(container_plan.env),
                    ),
                )
            except ConflictError:
                current.status = TaskStatus.Cancelled
                current.error = "owning app is not active"
                current.finished_at = datetime.now(UTC)
                rejected = task_repository.upsert(current)
            else:
                current.container_id = container.id
                current.next_retry_at = None
                reserved = task_repository.upsert(current)
        if rejected is not None:
            self.services.tasks.publish_lifecycle_change(
                rejected,
                WorkspaceChangeType.Updated,
            )
            return None
        if reserved is None or container is None:
            raise RuntimeError("function container reservation did not produce a durable result")
        return reserved, container

    def release_dependents(self, task: Task, *, seen: set[str] | None = None) -> None:
        if not is_terminal_task_status(task.status):
            return
        visited = seen or set()
        with self.services.context.database.session() as session:
            dependencies = TaskDependencyRepository(session).list_for_upstream(task.id)
        for dependency in dependencies:
            try:
                self._try_schedule_waiting_task(dependency.task_id, seen=visited)
            except DomainError:
                # Releasing what waited on this task is downstream of finishing
                # it, and the work that finished is already recorded. A refusal
                # here must not travel back up and fail the call that reported a
                # success — a runner would exit non-zero for a function that ran
                # perfectly well.
                LOGGER.exception(
                    "releasing dependent task %s of %s failed", dependency.task_id, task.id
                )

    def function_call_graph(
        self,
        task_id: str,
        *,
        workspace_id: str,
    ) -> FunctionCallGraphResponse:
        try:
            root_task = self._workspace_task(task_id, workspace_id)
            root_task_id = root_task.root_task_id or root_task.id
            tasks = self._root_tasks(root_task_id, workspace_id)
            with self.services.context.database.session() as session:
                dependencies = TaskDependencyRepository(session).list_for_root(
                    root_task_id,
                    workspace_id=workspace_id,
                )
            dependencies_by_task: dict[str, list[str]] = {}
            for dependency in dependencies:
                dependencies_by_task.setdefault(dependency.task_id, []).append(
                    dependency.upstream_task_id
                )
            children_by_parent: dict[str, list[Task]] = {}
            for task in tasks:
                children_by_parent.setdefault(task.parent_task_id or "", []).append(task)
            nodes = [
                self._task_graph_node(task, children_by_parent, dependencies_by_task)
                for task in sorted(
                    children_by_parent.get("", []),
                    key=lambda item: item.created_at,
                )
            ]
            if not nodes and root_task.id in {task.id for task in tasks}:
                nodes = [self._task_graph_node(root_task, children_by_parent, dependencies_by_task)]
            root = next((node for node in nodes if node.task_id == root_task_id), None)
            return FunctionCallGraphResponse(root_task_id=root_task_id, root=root, nodes=nodes)
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(str(exc)) from exc

    def _root_tasks(self, root_task_id: str, workspace_id: str) -> list[Task]:
        with self.services.context.database.session() as session:
            records = TaskRepository(session).list(workspace_id=workspace_id)
        return [
            task for task in records if task.id == root_task_id or task.root_task_id == root_task_id
        ]

    def _task_graph_node(
        self,
        task: Task,
        children_by_parent: dict[str, list[Task]],
        dependencies_by_task: dict[str, list[str]],
    ) -> FunctionCallGraphNode:
        return FunctionCallGraphNode(
            task_id=task.id,
            parent_task_id=task.parent_task_id or "",
            root_task_id=task.root_task_id or task.id,
            status=task.status,
            stub_id=task.stub_id or "",
            deployment_id=task.deployment_id or "",
            app_id=task.app_id or "",
            name=task.name,
            function_name=task.handler or task.name.removeprefix("function-"),
            created_at=task.created_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
            dependencies=dependencies_by_task.get(task.id, []),
            children=[
                self._task_graph_node(child, children_by_parent, dependencies_by_task)
                for child in sorted(
                    children_by_parent.get(task.id, []),
                    key=lambda item: item.created_at,
                )
            ],
        )

    def function_invoke_stream(
        self,
        request: FunctionInvokeBody,
        *,
        poll_interval_seconds: float = 0.25,
        keepalive_interval_seconds: float = 5.0,
    ) -> Iterable[FunctionInvokeResponse]:
        initial = self.function_invoke(request)
        yield initial
        if initial.done or initial.exit_code != 0 or not initial.task_id or request.headless:
            return

        seen_logs: set[str] = set()
        sleep_seconds = max(poll_interval_seconds, 0.05)
        last_status = ""
        last_keepalive = time.monotonic()
        while True:
            for entry in self.services.tasks.logs(initial.task_id):
                if entry.id in seen_logs:
                    continue
                seen_logs.add(entry.id)
                last_keepalive = time.monotonic()
                yield FunctionInvokeResponse.from_result(
                    task_id=initial.task_id,
                    output=_stream_log_output(entry.message),
                )
            try:
                task = self.services.tasks.get(initial.task_id)
            except NotFoundError as exc:
                yield FunctionInvokeResponse.from_result(
                    task_id=initial.task_id,
                    output=str(exc),
                    done=True,
                    exit_code=1,
                )
                return
            if task.status.value != last_status:
                last_status = task.status.value
                last_keepalive = time.monotonic()
                yield FunctionInvokeResponse.from_result(
                    task_id=initial.task_id,
                    output=f"Task <{initial.task_id}> {last_status}\n",
                )
            if is_terminal_task_status(task.status):
                yield FunctionInvokeResponse.from_result(
                    task_id=task.id,
                    result=(
                        _function_result_payload(task)
                        if task.status is TaskStatus.Complete
                        else None
                    ),
                    output=task.error or "",
                    done=True,
                    exit_code=task.exit_code
                    if task.exit_code is not None
                    else 0
                    if task.status is TaskStatus.Complete
                    else 1,
                )
                return
            if time.monotonic() - last_keepalive >= max(keepalive_interval_seconds, sleep_seconds):
                last_keepalive = time.monotonic()
                yield FunctionInvokeResponse.from_result(task_id=initial.task_id)
            time.sleep(sleep_seconds)

    def function_get_args(self, request: FunctionGetArgsRequest) -> FunctionGetArgsResponse:
        with self.services.context.database.session() as session:
            task = TaskRepository(session).get_for_update_across_workspaces(request.task_id)
            if task is None:
                raise NotFoundError(f"function arguments not found: {request.task_id}")
            assigned_container_id = task.container_id or ""
            if task.status is not TaskStatus.Running:
                raise ConflictError(
                    f"function arguments are unavailable while task {task.id} is "
                    f"{task.status.value}"
                )
            if not assigned_container_id or assigned_container_id != request.container_id:
                raise ConflictError(
                    f"container {request.container_id} does not own function task {task.id}"
                )
            if task.invocation is None:
                raise InvalidInputError(f"function task {task.id} has no invocation payload")
            validate_function_dependency_bindings(task.dependency_bindings)
            return FunctionGetArgsResponse(
                invocation=task.invocation,
                dependencies=task.dependency_bindings,
            )

    def function_set_result(
        self,
        request: FunctionSetResultBody,
    ) -> FunctionSetResultResponse:
        outcome = self.services.tasks.finish_with_retry(
            request.task_id,
            TaskStatus.Complete,
            container_id=request.container_id,
            function_result=request.result,
            exit_code=0,
        )
        if not outcome.state_changed:
            return FunctionSetResultResponse(stored=False, status=outcome.task.status)
        self.services.events.emit(
            "function.result.set",
            level=EventLevel.Info,
            resource_type="task",
            resource_id=request.task_id,
            message=f"stored function result for {request.task_id}",
            data={
                "result_size_bytes": function_result_payload_size(request.result),
            },
            workspace_id=outcome.task.workspace_id,
        )
        if is_terminal_task_status(outcome.task.status):
            self.release_dependents(outcome.task)
        return FunctionSetResultResponse(status=outcome.task.status)

    def function_monitor(self, request: FunctionMonitorRequest) -> FunctionMonitorResponse:
        task = self.services.tasks.get(request.task_id)
        workspace = self._task_workspace_id(task)
        plan = plan_function_monitor(
            planning.FunctionMonitorRequest(
                workspace_id=workspace,
                stub_id=request.stub_id,
                container_id=request.container_id,
                task_id=request.task_id,
                task_status=task.status,
                claimed=True,
                cancellation_requested=task.status is TaskStatus.Cancelled,
                timeout_elapsed=task.status is TaskStatus.Timeout,
            )
        )
        if plan.next_status is not None and task.status is not plan.next_status:
            self.services.tasks.transition(task, plan.next_status)
        return FunctionMonitorResponse(
            cancelled=plan.cancelled,
            complete=plan.complete,
            timed_out=plan.timed_out,
        )

    def function_cron(self, request: FunctionCronRequest) -> FunctionCronResponse:
        stub = self.control_plane.get_stub(request.stub_id)
        deployment = self.services.deployments.get(request.deployment_id)
        if (
            stub.kind not in FUNCTION_LIKE_STUB_KINDS
            or stub.deployment_id != deployment.id
            or deployment.stub_id != stub.id
        ):
            msg = "cron schedule must reference its function-like deployment stub"
            raise InvalidInputError(msg)
        workspace = self.control_plane.get_workspace(stub.workspace_id)
        plan = plan_function_cron(
            planning.FunctionCronRequest(
                workspace_name=workspace.name,
                stub_id=stub.id,
                deployment_id=deployment.id,
                deployment_name=deployment.name,
                cron=request.cron,
            )
        )
        record = self.services.cron_jobs.create(
            plan.job_name,
            request.cron,
            deployment.id,
            workspace=stub.workspace_id,
            payload=plan.payload,
        )
        return FunctionCronResponse(cron_job_id=record.name)

    def _task_workspace_id(self, task: Task) -> str:
        if not task.workspace_id:
            raise InvalidInputError(f"function task {task.id} is missing workspace ownership")
        return task.workspace_id


def _function_runtime_env(values: Iterable[str], gateway_http_url: str) -> list[str]:
    env = list(values)
    if gateway_http_url:
        env.append(f"{GATEWAY_HTTP_URL_ENV}={gateway_http_url}")
    return env


def _function_result_payload(task: Task) -> FunctionResultPayload:
    if task.function_result is None:
        raise InvalidInputError(f"function task {task.id} has no function result")
    return task.function_result


def _persisted_invocation_arguments(
    invocation: FunctionInvocationPayload,
) -> tuple[list[JsonValue] | None, dict[str, JsonValue] | None]:
    if isinstance(invocation, FunctionJsonInvocation):
        return list(invocation.args), dict(invocation.kwargs)
    if invocation.arguments is None:
        return None, None
    return list(invocation.arguments.args), dict(invocation.arguments.kwargs)


def _stream_log_output(message: str) -> str:
    if not message:
        return ""
    return message if message.endswith("\n") else f"{message}\n"


__all__ = ["FunctionControlService"]
