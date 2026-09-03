from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from control.service import ControlPlaneService, StubKind, StubRecord
from database.repositories.execution import (
    LogPage,
    LogPageCursor,
    TaskDependencyRepository,
    TaskRepository,
)
from database.repositories.orchestration import ContainerRepository
from database.types import DatabaseSession
from pydantic import JsonValue
from shared.app_identity import FUNCTION_IMAGE
from shared.autoscaling import function_container_ceiling
from shared.container_requests import (
    WORKER_USER_CODE_VOLUME,
    StopContainerReason,
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
    FunctionClaimedTask,
    FunctionClaimRequest,
    FunctionClaimResponse,
    FunctionInvokeBody,
    FunctionInvokeResponse,
    FunctionMonitorRequest,
    FunctionMonitorResponse,
    FunctionSetResultBody,
    FunctionSetResultResponse,
)
from shared.http.workspace_changes import WorkspaceChangeType
from shared.tasks import Task, TaskDependency, TaskStatus, is_terminal_task_status
from shared.timestamps import utc_now

from database import AsyncDatabaseClient
from execution.checkpoints import latest_available_checkpoint
from execution.config import env_sequence_mapping
from execution.containers.planning import ContainerSchedulingOptions
from execution.containers.service import PendingContainerReservation
from execution.functions import planning
from execution.functions.config import FunctionStubConfig
from execution.functions.planning import (
    FunctionContainerStartAuthority,
    FunctionContainerStartRequest,
    FunctionTaskCancellationReason,
    function_cancellation_decision,
    function_container_start_allowed,
    plan_function_container_start,
    plan_function_invoke,
    plan_function_monitor,
)
from execution.mounts import (
    container_resource_mounts,
    container_resource_mounts_require_workspace_storage,
)
from execution.services import ExecutionServices, SchedulerSubmissionResult

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FunctionControlService:
    services: ExecutionServices
    gateway_http_url: Callable[[], str] = no_gateway_origin
    async_database: AsyncDatabaseClient | None = None
    control_plane: ControlPlaneService = field(init=False)

    def __post_init__(self) -> None:
        self.control_plane = ControlPlaneService(self.services.context)

    def function_invoke(self, request: FunctionInvokeBody) -> FunctionInvokeResponse:
        try:
            stub = self.control_plane.get_stub(request.stub_id)
            if stub.kind is not StubKind.Function:
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
            self._assert_within_pending_limit(stub.id, config)
            retry_policy = config.effective_retry_policy
            invoke_plan = plan_function_invoke(
                planning.FunctionInvokeRequest(
                    invocation=request.invocation,
                    headless=request.headless,
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
            # Readiness is established once. `claimable_at` is what says so, and
            # it outlives the arrangement where one container served one task —
            # `container_id` only stood in for it while those were the same fact.
            if task.claimable_at is not None or is_terminal_task_status(task.status):
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
        # Recorded before anything acts on it. A task marked claimable and never
        # scheduled is recoverable — the row says it is runnable and nothing owns
        # it. A task scheduled and never marked is not, because the only evidence
        # it was ready would be the container that failed to start.
        with self.services.context.database.session() as session:
            marked = TaskRepository(session).mark_claimable(task.id, at=utc_now())
        if marked is not None:
            task = marked
        return self._schedule_function_task(task)

    def unclaimed_task_counts(self, stub_ids: Sequence[str]) -> dict[str, int]:
        with self.services.context.database.session() as session:
            return TaskRepository(session).count_unclaimed_by_stub(stub_ids)

    def containers_holding_work(self, container_ids: Sequence[str]) -> set[str]:
        """Which of these containers is serving an invocation right now.

        Asked before a scale-down chooses what to stop. A stop settles what a
        container was holding by releasing it, so stopping a busy one loses no
        invocation — it loses the part of one that had already run, and a
        handler that is not idempotent runs that part again.
        """

        with self.services.context.database.session() as session:
            return TaskRepository(session).containers_with_inflight_work(container_ids)

    def start_function_container(self, stub_id: str) -> bool:
        """Start one more container for this stub, because the autoscaler said so.

        How deep the backlog warrants going has already been answered by the
        caller; the ceiling is not taken on trust and is checked again where the
        container is reserved, which is the only place it can be checked without
        racing.

        It takes the oldest unclaimed task if there is one — that is the
        invocation this container will most likely serve, and a task that has
        since finished withdraws the warrant. With nothing waiting, the warrant
        is the stub's own warm floor, which is a promise about containers rather
        than about work, so the container is reserved from the stub alone.
        """

        with self.services.context.database.session() as session:
            candidates = TaskRepository(session).list_unclaimed_claimable(
                limit=1,
                stub_id=stub_id,
            )
        pending = next(iter(candidates), None)
        if pending is not None:
            scheduled = self._schedule_function_task(
                pending,
                authority=FunctionContainerStartAuthority.Autoscaler,
            )
            return scheduled is not None
        stub = self.control_plane.get_stub(stub_id)
        if stub.kind is not StubKind.Function:
            return False
        launched = self._launch_function_container(
            stub,
            task=None,
            eligible_at=None,
            authority=FunctionContainerStartAuthority.Autoscaler,
        )
        return launched is not None

    def _schedule_function_task(
        self,
        task: Task,
        *,
        eligible_at: datetime | None = None,
        authority: FunctionContainerStartAuthority = (FunctionContainerStartAuthority.ColdStart),
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
        if not self._scheduled_execution_allowed(task, scheduled=bool(stub.config.cron)):
            return None
        return self._launch_function_container(
            stub,
            task=task,
            eligible_at=eligible_at,
            authority=authority,
        )

    def _launch_function_container(
        self,
        stub: StubRecord,
        *,
        task: Task | None,
        eligible_at: datetime | None,
        authority: FunctionContainerStartAuthority,
    ) -> SchedulerSubmissionResult | None:
        """Plan, reserve and submit one container for this stub.

        The container is the stub's, not the task's. A task is passed when one
        prompted the start, and it is read only to decide whether starting
        anything is warranted — a finished or not-yet-due task warrants nothing.
        A warm floor is its own warrant and passes none.
        """

        workspace = self.control_plane.get_workspace(stub.workspace_id)
        config = FunctionStubConfig.model_validate(stub.config, from_attributes=True)
        # A task with nothing to run cannot be served by any container, so it
        # fails here rather than after one has been started for it.
        if task is not None and task.invocation is None:
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
                handler=stub.handler or "",
                container_id=container_id,
                keep_warm_seconds=config.runtime.keep_warm,
                concurrency=config.runtime.concurrency,
                in_process=config.runtime.in_process,
                python_executable=config.image.python_executable,
                cpu_millicores=config.runtime.requested_cpu_millicores,
                cpu_limit_millicores=config.runtime.limit_cpu_millicores,
                memory_mib=config.runtime.requested_memory_mib,
                memory_limit_mib=config.runtime.limit_memory_mib,
                disk_mib=config.runtime.requested_disk_mib,
                requires_gpu=config.runtime.gpu_required,
                gpu_count=config.runtime.gpu_count,
                image_id=config.effective_image_id,
                checkpoint_enabled=config.runtime.checkpoint_enabled,
                env=_function_runtime_env(config.env_list, self.gateway_http_url()),
                secret_env=[],
                lifecycle_hooks=config.lifecycle_hooks,
            )
        )
        container = self._reserve_function_container(
            task,
            container_plan=container_plan,
            stub_id=stub.id,
            stub_name=stub.name,
            stub_workspace_id=stub.workspace_id,
            stub_app_id=stub.app_id,
            eligible_at=eligible_at,
            authority=authority,
            max_containers=function_container_ceiling(stub.config.autoscaler.max_containers),
        )
        if container is None:
            return None
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
                stub_type="function",
                startup_kind=WorkerStartupKind.Function,
                entrypoint=container_plan.entrypoint,
                cwd=WORKER_USER_CODE_VOLUME,
                env_list=container_plan.env,
                image_id=container_plan.image_id or FUNCTION_IMAGE,
                app_id=stub.app_id or "",
                deployment_id=stub.deployment_id or "",
                checkpoint_exposed_ports=(
                    checkpoint.exposed_ports if checkpoint is not None else []
                ),
                checkpoint_id=checkpoint.checkpoint_id if checkpoint is not None else "",
                checkpoint_enabled=config.runtime.checkpoint_enabled,
                cpu_millicores=container_plan.cpu_millicores,
                cpu_limit_millicores=container_plan.cpu_limit_millicores,
                memory_mib=container_plan.memory_mib,
                memory_limit_mib=container_plan.memory_limit_mib,
                disk_mib=container_plan.disk_mib,
                gpu=list(config.runtime.gpu),
                gpu_count=container_plan.gpu_count,
                pool_selector=config.runtime.pool_selector or "",
                runtime=config.runtime.runtime,
                runtime_class=config.runtime.runtime_class or "",
                docker_enabled=config.runtime.docker_enabled,
                preemptible=config.runtime.preemptible,
                workspace_gpu_quota=config.runtime.workspace_gpu_quota,
                workspace_cpu_quota_millicores=config.runtime.workspace_cpu_quota_millicores,
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
            if task is not None:
                updated = self.services.tasks.transition(
                    task,
                    TaskStatus.Failed,
                    error=scheduled.reason,
                    exit_code=1,
                )
                self.release_dependents(updated)
        return scheduled

    def assert_may_accept_invocation(self, stub_id: str) -> None:
        """The backpressure refusal, asked before a streaming response begins.

        A stream sends its status before the generator runs, so this raised from
        inside one cannot be a refusal — it reaches the caller as a response that
        breaks mid-flight, which is how a clear limit turns into a gateway error
        with nothing in it about the limit.
        """

        stub = self.control_plane.get_stub(stub_id)
        if stub.kind is not StubKind.Function:
            return
        config = FunctionStubConfig.model_validate(stub.config, from_attributes=True)
        self._assert_within_pending_limit(stub.id, config)

    def _assert_within_pending_limit(self, stub_id: str, config: FunctionStubConfig) -> None:
        """Refuse work this function has no prospect of getting to.

        A fan-out that outruns what the platform will start containers for
        otherwise queues without bound, and every queued call is billable work
        somebody is waiting on. Failing the call that crosses the line tells the
        caller immediately, where accepting it would report success and then be
        indistinguishable from a function that is merely slow.

        Deliberately approximate: two calls arriving together can both read the
        same count and both be admitted. The limit bounds a runaway rather than
        rationing the last slot, and a tighter one would mean serialising every
        invocation of every function behind a lock.
        """

        limit = config.effective_max_pending_tasks
        if limit <= 0:
            return
        with self.services.context.database.session() as session:
            in_flight = TaskRepository(session).count_inflight_for_stub(stub_id)
        if in_flight >= limit:
            raise CapacityLimitReachedError(
                f"this function already has {in_flight} calls in flight, which is the most "
                f"it accepts ({limit}); raise max_pending_tasks to queue more"
            )

    def _scheduled_execution_allowed(self, task: Task, *, scheduled: bool) -> bool:
        """A run that fired may outlive the deployment that scheduled it.

        Only a scheduled run can: every other invocation has a caller waiting,
        and a caller cannot invoke a deployment that is gone. This one was
        enqueued by a tick, so between the tick and the claim its deployment may
        have been stopped or deleted, and running it then is work nobody wants.
        """

        if not scheduled:
            return True
        if not task.deployment_id:
            updated = self.services.tasks.transition(
                task,
                TaskStatus.Failed,
                error="scheduled task is missing deployment_id",
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
        reason = "scheduled deployment is unavailable"
        if deployment is not None:
            reason = (
                "scheduled deployment is deleted"
                if deployment.deleted_at is not None
                else "scheduled deployment is inactive"
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
        # A retry due immediately is not scheduled from here. Making it runnable
        # means releasing its claim, and that is `schedule_due_retries`, which
        # owns the decision for delayed retries too — one path rather than two
        # that must agree. Scheduling it here without releasing did nothing: a
        # task in `retry` is invisible to a claim and counts toward no capacity.
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
            # Stopping the container is the only way to reach the handler, and a
            # pooled one is also serving calls nobody cancelled. `User` would
            # settle their claims the way it settles this one — cancelled, which
            # is terminal and carries no retry — so the reason says the platform
            # stopped it. Their claims are released instead and run again
            # elsewhere; the cancelled task is already terminal, and a released
            # claim never resurrects one.
            self.services.containers.stop(
                current.container_id,
                reason=StopContainerReason.Scheduler,
            )
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
                if stub.kind is not StubKind.Function:
                    continue
            if task.next_retry_at is not None and current < task.next_retry_at:
                continue
            # A retry is work that is runnable again, so it goes back into the
            # population a claim reads: still ready, owned by nobody. Left in
            # `retry` holding the failed attempt's container it would be visible
            # to no claim and picked up by nothing.
            with self.services.context.database.session() as session:
                released = TaskRepository(session).release_claim(task.id)
            if released is None:
                continue
            try:
                result = self._schedule_function_task(released, eligible_at=current)
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
        scheduled.extend(self._schedule_unservable_claimable_work(limit=limit, at=current))
        return scheduled

    def _schedule_unservable_claimable_work(
        self,
        *,
        limit: int,
        at: datetime,
    ) -> list[Task]:
        """Give a stub with runnable work and nothing alive somewhere to run it.

        Releasing a claim and having somewhere to run are separate events, and a
        container dying between them leaves a row that says runnable forever
        while its caller waits forever. Nothing else notices: the work is not
        failed, not claimed, and not attached to anything that will report it.

        Recovery only, one container per stub. How deep to go for a backlog that
        is already being served is the autoscaler's decision, taken against the
        whole queue on the same tick this runs on — a second opinion formed here
        from one task row would be the two-starter race again.
        """

        scheduled: list[Task] = []
        with self.services.context.database.session() as session:
            candidates = TaskRepository(session).list_unclaimed_claimable(limit=limit)
        served: set[str] = set()
        for task in candidates:
            if not task.stub_id or task.stub_id in served:
                continue
            served.add(task.stub_id)
            try:
                stub = self.control_plane.get_stub(task.stub_id)
                if stub.kind is not StubKind.Function:
                    continue
                result = self._schedule_function_task(task, eligible_at=at)
            except DomainError:
                LOGGER.exception("scheduling claimable task %s failed", task.id)
                continue
            if result is not None:
                scheduled.append(self.services.tasks.get(task.id))
        return scheduled

    def _reserve_function_container(
        self,
        task: Task | None,
        *,
        container_plan: planning.FunctionContainerStartPlan,
        stub_id: str,
        stub_name: str,
        stub_workspace_id: str,
        stub_app_id: str | None,
        eligible_at: datetime | None,
        authority: FunctionContainerStartAuthority,
        max_containers: int,
    ) -> ContainerRecord | None:
        """Reserve a container for this stub, prompted by `task` but not bound to it.

        The task is read to decide whether starting anything is warranted at all —
        a finished or not-yet-due task warrants nothing — but the container that
        results serves the stub. Which invocation it runs is settled later, by the
        claim, and may well be a different one that arrived while it was starting.

        The stub's ceiling is settled here and nowhere else. This is the only
        point at which the count of what is already running and the row that adds
        to it are the same transaction, so it is the only point at which refusing
        past the ceiling refuses anything — a caller that checked earlier checked
        a number another caller was in the middle of changing.
        """

        rejected: Task | None = None
        container: ContainerRecord | None = None
        current: Task | None = None
        with self.services.context.database.session() as session:
            containers = ContainerRepository(session)
            containers.lock_stub_capacity(stub_id)
            if not function_container_start_allowed(
                authority=authority,
                live_containers=containers.count_live_for_stub(stub_id),
                max_containers=max_containers,
            ):
                return None
            task_repository = TaskRepository(session)
            if task is not None:
                current = task_repository.get_for_update_across_workspaces(task.id)
                if current is None or is_terminal_task_status(current.status):
                    return None
                if current.status not in {TaskStatus.Pending, TaskStatus.Retry}:
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
                        stub_id=stub_id,
                        app_id=stub_app_id,
                        env=env_sequence_mapping(container_plan.env),
                    ),
                )
            except ConflictError:
                # The app was deleted between the plan and the reservation. A
                # task that prompted this start is cancelled with the reason; a
                # warm start has no caller to tell.
                if current is None:
                    return None
                current.status = TaskStatus.Cancelled
                current.error = "owning app is not active"
                current.finished_at = datetime.now(UTC)
                rejected = task_repository.upsert(current)
        if rejected is not None:
            self.services.tasks.publish_lifecycle_change(
                rejected,
                WorkspaceChangeType.Updated,
            )
            return None
        if container is None:
            raise RuntimeError("function container reservation did not produce a durable result")
        return container

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

    async def function_invoke_stream(
        self,
        initial: FunctionInvokeResponse,
        *,
        headless: bool = False,
        poll_interval_seconds: float = 0.25,
        keepalive_interval_seconds: float = 5.0,
    ) -> AsyncIterator[FunctionInvokeResponse]:
        yield initial
        if initial.done or initial.exit_code != 0 or not initial.task_id or headless:
            return

        log_cursor: LogPageCursor | None = None
        sleep_seconds = max(poll_interval_seconds, 0.05)
        last_status = ""
        last_keepalive = time.monotonic()
        while True:
            try:
                log_page, task = await self._async_database().run_transaction(
                    lambda session, cursor=log_cursor: self._function_stream_page_in_session(
                        session,
                        initial.task_id,
                        cursor=cursor,
                    )
                )
            except NotFoundError as exc:
                yield FunctionInvokeResponse.from_result(
                    task_id=initial.task_id,
                    output=str(exc),
                    done=True,
                    exit_code=1,
                )
                return
            for record in log_page.data:
                entry = record.entry
                log_cursor = record.cursor
                last_keepalive = time.monotonic()
                yield FunctionInvokeResponse.from_result(
                    task_id=initial.task_id,
                    output=_stream_log_output(entry.message),
                    stream=entry.stream,
                )
            if task.status.value != last_status:
                last_status = task.status.value
                last_keepalive = time.monotonic()
                yield FunctionInvokeResponse.from_result(
                    task_id=initial.task_id,
                    status=last_status,
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
            await asyncio.sleep(sleep_seconds)

    def _async_database(self) -> AsyncDatabaseClient:
        if self.async_database is None:
            raise RuntimeError("function streaming asynchronous database is not configured")
        return self.async_database

    def _function_stream_page_in_session(
        self,
        session: DatabaseSession,
        task_id: str,
        *,
        cursor: LogPageCursor | None,
    ) -> tuple[LogPage, Task]:
        tasks = self.services.tasks
        task = tasks.get_in_session(session, task_id)
        return tasks.log_page_in_session(session, task, limit=1_000, cursor=cursor), task

    def function_claim(self, request: FunctionClaimRequest) -> FunctionClaimResponse:
        """Give a container asking for work one invocation to run, if there is one.

        Empty is the ordinary answer, not a failure: a warm container asks
        repeatedly while nothing is arriving, and every one of those asks that
        finds nothing is the pooling working as intended.
        """

        with self.services.context.database.session() as session:
            claimed = TaskRepository(session).claim_for_stub(
                request.stub_id,
                container_id=request.container_id,
                limit=1,
            )
        if not claimed:
            return FunctionClaimResponse()
        task = claimed[0]
        if task.invocation is None:
            # Failed rather than raised. The claim already happened, so raising
            # would leave a task owned by a container that was told nothing about
            # it — invisible to the next claim and waited on forever by its caller.
            updated = self.services.tasks.transition(
                task,
                TaskStatus.Failed,
                error="function task is missing its invocation payload",
                exit_code=1,
            )
            self.release_dependents(updated)
            return FunctionClaimResponse()
        validate_function_dependency_bindings(task.dependency_bindings)
        self.services.tasks.publish_lifecycle_change(task, WorkspaceChangeType.Updated)
        return FunctionClaimResponse(
            task=FunctionClaimedTask(
                task_id=task.id,
                root_task_id=task.root_task_id or task.id,
                attempt_number=task.attempt_number,
                max_attempts=task.max_attempts,
                invocation=task.invocation,
                dependencies=task.dependency_bindings,
            )
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
