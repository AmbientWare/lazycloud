from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from anyio import CancelScope
from control.apps import DatabaseAppExecutionAdmission
from control.stubs import StubService
from database.records.apps import StubKind, StubRecord
from database.repositories.apps import DeploymentRepository
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.execution import (
    LogPage,
    LogPageCursor,
    TaskAttemptRepository,
    TaskDependencyRepository,
    TaskRepository,
)
from database.repositories.orchestration import ContainerRepository
from database.types import DatabaseSession
from observability.events import EventService
from observability.stream_state import AsyncTaskChangeReader
from pydantic import JsonValue
from shared.app_identity import FUNCTION_IMAGE
from shared.autoscaler_state import AutoscalerTargetKind
from shared.autoscaling import function_container_ceiling
from shared.container_requests import (
    WORKER_USER_CODE_VOLUME,
    StopContainerReason,
    WorkerStartupKind,
)
from shared.containers import ContainerRecord, ContainerStatus
from shared.env import HOT_RELOAD_DIR_ENV, HOT_RELOAD_ENV, parse_environment
from shared.errors import (
    CapacityLimitReachedError,
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
)
from shared.events import Event, EventLevel
from shared.function_payloads import (
    FunctionDependencyBinding,
    FunctionInvocationPayload,
    FunctionJsonInvocation,
    FunctionPayloadEncoding,
    FunctionResultPayload,
    function_result_payload_size,
    validate_function_dependency_bindings,
)
from shared.http.execution_entry import ExecutionEntryEvidence
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
    FunctionRetireRequest,
    FunctionRetireResponse,
    FunctionServeRequest,
    FunctionServeResponse,
    FunctionSetResultBody,
    FunctionSetResultResponse,
)
from shared.http.task_progress import TaskPendingProgress
from shared.http.workspace_changes import WorkspaceChangeType
from shared.placement import Placement, ProductRegion
from shared.tasks import (
    Task,
    TaskDependency,
    TaskProgressSnapshot,
    TaskResultSnapshot,
    TaskStatus,
    is_terminal_task_status,
)
from shared.timestamps import utc_now

from database import AsyncDatabaseClient
from execution.batching import RequestBatch
from execution.checkpoints import latest_available_checkpoint
from execution.containers.planning import ContainerSchedulingOptions
from execution.containers.service import PendingContainerReservation
from execution.functions import planning
from execution.functions.config import FunctionStubConfig
from execution.functions.planning import (
    FunctionContainerStartAuthority,
    FunctionContainerStartRequest,
    FunctionTaskCancellationReason,
    function_container_start_allowed,
    function_status_for_cancellation,
    plan_function_container_start,
    plan_function_invoke,
    plan_function_monitor,
)
from execution.mounts import (
    container_resource_mounts,
    container_resource_mounts_require_workspace_storage,
)
from execution.placement import workload_placement
from execution.services import ExecutionServices, SchedulerSubmissionResult
from execution.task_claims import TaskClaimReleaseService
from execution.tasks import TaskClaimOutcome, TaskFinishOutcome

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _InvocationResult:
    response: FunctionInvokeResponse
    headless: bool


@dataclass(slots=True)
class FunctionControlService:
    services: ExecutionServices
    async_database: AsyncDatabaseClient | None = None
    task_changes: AsyncTaskChangeReader | None = None
    stubs: StubService = field(init=False)
    _admissions: dict[tuple[str, str], RequestBatch[FunctionInvokeBody, _InvocationResult]] = field(
        default_factory=dict, init=False
    )

    def __post_init__(self) -> None:
        self.stubs = StubService(self.services.context)

    def function_invoke(
        self, request: FunctionInvokeBody, *, stub: StubRecord
    ) -> FunctionInvokeResponse:
        result = self._invoke_batch([request], stub=stub)[0]
        if isinstance(result, Exception):
            raise result
        return result.response

    async def function_invoke_async(
        self,
        request: FunctionInvokeBody,
        *,
        workspace_id: str,
        stub: StubRecord | None = None,
    ) -> FunctionInvokeResponse:
        if stub is None:
            stub = await self._async_database().run_transaction(
                lambda session: self.stubs.get_stub_in_session(
                    session, request.stub_id, workspace=workspace_id
                )
            )
        if stub.workspace_id != workspace_id or stub.kind is not StubKind.Function:
            raise NotFoundError("function not found")
        # Graph validation can fail independently of admission. Keep those calls
        # isolated so a missing dependency cannot roll back another caller.
        graph = bool(request.dependencies or request.parent_task_id or request.root_task_id)
        key = (stub.id, str(uuid4()) if graph else "")
        batch = self._admissions.get(key)
        if batch is None:
            admitted_stub = stub

            async def execute(
                requests: Sequence[FunctionInvokeBody],
            ) -> Sequence[_InvocationResult | Exception]:
                return await asyncio.to_thread(self._invoke_batch, requests, stub=admitted_stub)

            async def abandon(result: _InvocationResult) -> None:
                if result.response.task_id and not result.headless:
                    await asyncio.to_thread(self.cancel_task, result.response.task_id)

            def on_idle() -> None:
                self._admissions.pop(key, None)

            batch = RequestBatch(
                execute=execute,
                abandon=abandon,
                on_idle=on_idle,
            )
            self._admissions[key] = batch
        return (await batch.submit(request)).response

    def _invoke_batch(
        self, requests: Sequence[FunctionInvokeBody], *, stub: StubRecord
    ) -> list[_InvocationResult | Exception]:
        try:
            return self._admit_invocations(requests, stub=stub)
        except DomainError as exc:
            return [exc for _ in requests]
        except Exception as exc:
            return [
                _InvocationResult(
                    FunctionInvokeResponse.from_result(
                        task_id="", output=str(exc), done=True, exit_code=1
                    ),
                    request.headless,
                )
                for request in requests
            ]

    def _admit_invocations(
        self, requests: Sequence[FunctionInvokeBody], *, stub: StubRecord
    ) -> list[_InvocationResult | Exception]:
        if stub.kind is not StubKind.Function:
            raise InvalidInputError(f"stub is not a function: {stub.id}")
        config = FunctionStubConfig.model_validate(stub.config, from_attributes=True)
        retry_policy = config.effective_retry_policy
        prepared: list[tuple[int, FunctionInvokeBody, Task]] = []
        results: list[_InvocationResult | Exception] = [
            RuntimeError("function admission omitted its result") for _ in requests
        ]
        for index, request in enumerate(requests):
            try:
                if (
                    isinstance(request.invocation, FunctionJsonInvocation)
                    and request.invocation.result_encoding is FunctionPayloadEncoding.Json
                    and stub.config.client_contract is not None
                ):
                    python_type = stub.config.client_contract.operation.return_python_type
                    if python_type:
                        raise InvalidInputError(
                            f"function returns {python_type}; call it through the Python SDK"
                        )
                invoke_plan = plan_function_invoke(
                    planning.FunctionInvokeRequest(
                        invocation=request.invocation,
                        headless=request.headless,
                        configured_retry_count=retry_policy.retry_count,
                    )
                )
                parent_task_id, root_task_id = self._task_graph_context(request, stub.workspace_id)
                task_args, task_kwargs = _persisted_invocation_arguments(invoke_plan.invocation)
                task = Task(
                    id=str(uuid4()),
                    name=f"function-{stub.name}",
                    workspace_id=stub.workspace_id,
                    app_id=stub.app_id,
                    stub_id=stub.id,
                    deployment_id=stub.deployment_id,
                    parent_task_id=parent_task_id or None,
                    root_task_id=root_task_id or None,
                    handler=stub.handler,
                    args=task_args or [],
                    kwargs=task_kwargs or {},
                    invocation=invoke_plan.invocation,
                    retry_policy=retry_policy,
                    max_attempts=retry_policy.max_attempts,
                    claimable_at=utc_now() if not request.dependencies else None,
                )
                prepared.append((index, request, task))
            except DomainError as exc:
                results[index] = exc
        if not prepared:
            return results

        # Cold eligibility can lock billing accounts. Commit before acquiring
        # artifact and workspace fences in the task creation transaction.
        with self.services.context.database.session() as session:
            needs_container = ContainerRepository(session).count_live_for_stub(stub.id) == 0
            admit = (
                self.services.containers.admit_container_start
                if needs_container
                else self.services.payment_admission.assert_workload_eligible
            )
            admit(
                session,
                workspace_id=stub.workspace_id,
                gpu=config.runtime.gpu,
                gpu_count=config.runtime.gpu_count,
                region=config.runtime.region,
                availability_zone=config.runtime.availability_zone,
            )
            placement = workload_placement(
                session,
                resolver=self.services.placement_resolver,
                stub=stub,
                workspace=self.services.context.workspace(session, stub.workspace_id),
            )
            limit = config.effective_max_pending_tasks
            available = len(prepared)
            if limit > 0:
                # Concurrent processes can observe the same count, as with a
                # single invocation. A batch must still consume each local slot.
                in_flight = TaskRepository(session).count_inflight_for_stub(stub.id)
                available = max(0, limit - in_flight)
                for index, _, _ in prepared[available:]:
                    results[index] = CapacityLimitReachedError(
                        f"this function already has {limit} calls in flight, which is the most "
                        f"it accepts ({limit}); raise max_pending_tasks to queue more"
                    )
        prepared = prepared[:available]
        if not prepared:
            return results

        with self.services.context.database.session() as session:
            if stub.app_id:
                DatabaseAppExecutionAdmission().assert_active(
                    session, app_id=stub.app_id, workspace_id=stub.workspace_id
                )
            if stub.deployment_id:
                active = DeploymentRepository(session).lock_invocation_active(
                    stub.deployment_id, workspace_id=stub.workspace_id, stub_id=stub.id
                )
                if active is None:
                    raise NotFoundError(f"deployment not found for function: {stub.deployment_id}")
                if not active:
                    raise ConflictError(f"deployment is not active: {stub.deployment_id}")
            tasks = self.services.tasks.create_batch_in_transaction(
                session, [task for _, _, task in prepared]
            )
            usage = self.services.usage.record_task_counts_in_session(
                session,
                workspace_id=stub.workspace_id,
                resource_type="function",
                resource_id=stub.id,
                task_ids=[task.id for task in tasks],
                kind=stub.kind.value,
                app_id=stub.app_id or "",
                deployment_id=stub.deployment_id or "",
            )
            events: list[tuple[Event, str | None]] = []
            for (_, request, _), task in zip(prepared, tasks, strict=True):
                dependencies = self._create_task_dependencies(session, task, request)
                events.append(
                    (
                        Event(
                            id=str(uuid4()),
                            action="function.invoked",
                            level=EventLevel.Info,
                            resource_type="task",
                            resource_id=task.id,
                            message=f"invoked function {stub.name}",
                            data={
                                "stub_id": stub.id,
                                "parent_task_id": task.parent_task_id or "",
                                "root_task_id": task.root_task_id or task.id,
                                "dependency_count": len(dependencies),
                                "headless": request.headless,
                            },
                        ),
                        stub.workspace_id,
                    )
                )
            self.services.events.emit_many_in_session(session, events)
            # This is the only shared write across independent invocations.
            if any(task.claimable_at is not None for task in tasks):
                self.services.execution_demand.activate_in_transaction(
                    session,
                    stub_id=stub.id,
                    workspace_id=stub.workspace_id,
                    kind=AutoscalerTargetKind.Function,
                )
        self.services.execution_demand.notify()
        for (index, request, _), task, record in zip(prepared, tasks, usage, strict=True):
            response = FunctionInvokeResponse.from_result(task_id=task.id)
            try:
                self.services.tasks.publish_created(task)
                self.services.usage.publish_change(record)
                scheduled = None
                if task.claimable_at is None:
                    scheduled = self._try_schedule_waiting_task(task.id, placement=placement)
                elif needs_container:
                    scheduled = self._schedule_function_task(task, placement=placement)
                if scheduled is not None and not scheduled.accepted:
                    response = FunctionInvokeResponse.from_result(
                        task_id=task.id,
                        output=scheduled.reason or "function container scheduling failed",
                        done=True,
                        exit_code=1,
                    )
            except Exception as exc:
                LOGGER.exception("post-commit function admission failed for task %s", task.id)
                try:
                    self.cancel_task(task.id)
                except Exception:
                    LOGGER.exception("failed to cancel admitted task %s", task.id)
                response = FunctionInvokeResponse.from_result(
                    task_id=task.id, output=str(exc), done=True, exit_code=1
                )
            results[index] = _InvocationResult(response, request.headless)
        return results

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
        session: DatabaseSession,
        task: Task,
        request: FunctionInvokeBody,
    ) -> list[TaskDependency]:
        dependencies: list[TaskDependency] = []
        seen: set[str] = set()
        repository = TaskDependencyRepository(session)
        for dependency in request.dependencies:
            upstream_task_id = dependency.task_id.strip()
            if not upstream_task_id or upstream_task_id in seen:
                continue
            upstream = TaskRepository(session).get_across_workspaces(upstream_task_id)
            if upstream is None or upstream.workspace_id != task.workspace_id:
                raise NotFoundError(f"task not found: {upstream_task_id}")
            seen.add(upstream_task_id)
            dependencies.append(
                repository.create(
                    TaskDependency(
                        workspace_id=task.workspace_id,
                        task_id=task.id,
                        upstream_task_id=upstream.id,
                        parent_task_id=task.parent_task_id,
                        root_task_id=task.root_task_id,
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
        placement: Placement | None = None,
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
            if marked is not None and marked.stub_id and marked.workspace_id:
                self.services.execution_demand.activate_in_transaction(
                    session,
                    stub_id=marked.stub_id,
                    workspace_id=marked.workspace_id,
                    kind=AutoscalerTargetKind.Function,
                )
        self.services.execution_demand.notify()
        if marked is not None:
            task = marked
        self.services.tasks.publish_lifecycle_change(task, WorkspaceChangeType.Updated)
        return self._schedule_function_task(task, placement=placement)

    def task_demand_counts(self, stub_ids: Sequence[str]) -> dict[str, int]:
        with self.services.context.database.session() as session:
            return TaskRepository(session).count_demand_by_stub(stub_ids)

    def containers_holding_work(self, container_ids: Sequence[str]) -> set[str]:
        """Which of these containers is serving an invocation right now.

        Asked before a scale-down chooses what to stop. A stop settles what a
        container was holding by releasing it, so stopping a busy one loses no
        invocation — it loses the part of one that had already run, and a
        handler that is not idempotent runs that part again.
        """

        with self.services.context.database.session() as session:
            return TaskRepository(session).containers_with_inflight_work(container_ids)

    def start_function_serve(self, request: FunctionServeRequest) -> FunctionServeResponse:
        stub = self.stubs.get_stub(request.stub_id)
        if stub.kind is not StubKind.Function:
            raise InvalidInputError("serve requires a function")
        if stub.deployment_id is not None:
            raise InvalidInputError("serve requires a prepared preview, not a deployed function")
        result = next(
            self._launch_function_containers(
                stub,
                target_containers=1,
                task=None,
                eligible_at=None,
                authority=FunctionContainerStartAuthority.Preview,
                preview_timeout=request.timeout,
            ),
            None,
        )
        if result is None:
            raise CapacityLimitReachedError("this function already has a live container")
        if not result.accepted:
            raise CapacityLimitReachedError(result.reason or "function preview could not start")
        return FunctionServeResponse(container_id=result.container_id)

    def start_function_containers(self, stub_id: str, *, desired_count: int) -> Iterator[str]:
        if desired_count <= 0:
            return
        stub = self.stubs.get_stub(stub_id)
        if stub.kind is not StubKind.Function:
            return
        with self.services.context.database.session() as session:
            candidates = TaskRepository(session).list_unclaimed_claimable(
                limit=1,
                stub_id=stub_id,
            )
        pending = next(iter(candidates), None)
        if pending is not None and not self._scheduled_execution_allowed(
            pending, scheduled=bool(stub.config.cron)
        ):
            return
        for launched in self._launch_function_containers(
            stub,
            target_containers=desired_count,
            task=pending,
            eligible_at=None,
            authority=FunctionContainerStartAuthority.Autoscaler,
        ):
            if not launched.accepted:
                raise CapacityLimitReachedError(
                    launched.reason or "function container could not start"
                )
            yield launched.container_id

    def _schedule_function_task(
        self,
        task: Task,
        *,
        eligible_at: datetime | None = None,
        authority: FunctionContainerStartAuthority = (FunctionContainerStartAuthority.ColdStart),
        placement: Placement | None = None,
    ) -> SchedulerSubmissionResult | None:
        if not task.stub_id:
            self.services.tasks.transition(
                task,
                TaskStatus.Failed,
                error="function task is missing stub_id",
                exit_code=1,
            )
            return None
        stub = self.stubs.get_stub(task.stub_id)
        if not self._scheduled_execution_allowed(task, scheduled=bool(stub.config.cron)):
            return None
        return next(
            self._launch_function_containers(
                stub,
                target_containers=1,
                task=task,
                eligible_at=eligible_at,
                authority=authority,
                placement=placement,
            ),
            None,
        )

    def _launch_function_containers(
        self,
        stub: StubRecord,
        *,
        target_containers: int,
        task: Task | None,
        eligible_at: datetime | None,
        authority: FunctionContainerStartAuthority,
        preview_timeout: int | None = None,
        placement: Placement | None = None,
    ) -> Iterator[SchedulerSubmissionResult]:
        """Prepare inputs, then reserve containers under the admission fences.

        A task-triggered start rechecks that its task still needs capacity.
        A warm floor passes no task.
        """

        with self.services.context.database.session() as workspace_session:
            workspace = self.services.context.workspace(workspace_session, stub.workspace_id)
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
            return

        # An invoke resolved this before it wrote its task; every other start
        # (a schedule, a retry, a warm floor) resolves here.
        try:
            if placement is None:
                with self.services.context.database.session() as session:
                    placement = workload_placement(
                        session,
                        resolver=self.services.placement_resolver,
                        stub=stub,
                        workspace=workspace,
                    )
        except (InvalidInputError, NotFoundError) as error:
            if task is None:
                raise
            self.services.tasks.transition(task, TaskStatus.Failed, error=str(error), exit_code=1)
            return

        checkpoint = (
            latest_available_checkpoint(
                self.services.context,
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
            )
            if config.runtime.checkpoint_enabled
            else None
        )
        for _ in range(target_containers):
            container_id = str(uuid4())
            container_plan = plan_function_container_start(
                FunctionContainerStartRequest(
                    workspace_name=workspace.name,
                    workspace_id=stub.workspace_id,
                    app_id=stub.app_id or "",
                    stub_id=stub.id,
                    handler=stub.handler or "",
                    container_id=container_id,
                    keep_warm_seconds=-1
                    if preview_timeout is not None
                    else config.runtime.keep_warm,
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
                    gpu=list(config.runtime.gpu),
                    image_id=config.effective_image_id,
                    checkpoint_enabled=config.runtime.checkpoint_enabled,
                    env=[
                        *config.env_list,
                        *(
                            [
                                f"{HOT_RELOAD_ENV}=true",
                                f"{HOT_RELOAD_DIR_ENV}={WORKER_USER_CODE_VOLUME}",
                            ]
                            if preview_timeout is not None
                            else []
                        ),
                    ],
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
                region=config.runtime.region,
                availability_zone=config.runtime.availability_zone,
                eligible_at=eligible_at,
                preemptible=config.runtime.preemptible,
                authority=authority,
                max_containers=min(
                    target_containers,
                    function_container_ceiling(stub.config.autoscaler.max_containers),
                ),
                preview_timeout=preview_timeout,
            )
            if container is None:
                return
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
                    gpu=list(container.gpu),
                    gpu_count=container.gpu_count,
                    placement=placement,
                    region=config.runtime.region,
                    availability_zone=config.runtime.availability_zone,
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
                            workspace=workspace,
                            mounts=resource_mounts,
                        )
                    ),
                    mounts=resource_mounts,
                ),
            )
            if not scheduled.accepted:
                with self.services.context.database.session() as session:
                    container.status = ContainerStatus.Failed
                    ContainerRepository(session).upsert(container)
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
            yield scheduled

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
        workspace_id: str,
        container_id: str = "",
        result: JsonValue = None,
        error: str | None = None,
        exit_code: int | None = None,
        retry_allowed: bool = True,
        execution_entry: ExecutionEntryEvidence | None = None,
        claim_id: str | None = None,
    ) -> TaskFinishOutcome:
        outcome = self.services.tasks.finish_with_retry(
            task_id,
            status,
            container_id=container_id or None,
            result=result,
            error=error,
            exit_code=exit_code,
            retry_allowed=retry_allowed,
            execution_entry=execution_entry,
            claim_id=claim_id,
            function_workspace_id=workspace_id,
        )
        # A retry due immediately is not scheduled from here. Making it runnable
        # means releasing its claim, and that is `schedule_due_retries`, which
        # owns the decision for delayed retries too — one path rather than two
        # that must agree. Scheduling it here without releasing did nothing: a
        # task in `retry` is invisible to a claim and counts toward no capacity.
        if outcome.state_changed and is_terminal_task_status(outcome.task.status):
            self.release_dependents(outcome.task)
        return outcome

    def cancel_task(
        self,
        task_id: str,
        *,
        reason: FunctionTaskCancellationReason = (FunctionTaskCancellationReason.RequestCancelled),
    ) -> Task:
        outcome = self.services.tasks.cancel(
            task_id,
            status=function_status_for_cancellation(reason),
            error=reason.value,
            exit_code=1,
        )
        updated = outcome.task
        if not outcome.state_changed:
            return updated
        if updated.container_id:
            if not updated.stub_id:
                raise InvalidInputError("running function task has no stub")
            runtime = self.stubs.get_stub(updated.stub_id).config.runtime
            if runtime.in_process or runtime.concurrency <= 1:
                self.services.containers.stop(
                    updated.container_id,
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
                stub = self.stubs.get_stub(task.stub_id)
                if stub.kind is not StubKind.Function:
                    continue
            if task.next_retry_at is not None and current < task.next_retry_at:
                continue
            # A retry is work that is runnable again, so it goes back into the
            # population a claim reads: still ready, owned by nobody. Left in
            # `retry` holding the failed attempt's container it would be visible
            # to no claim and picked up by nothing.
            with self.services.context.database.session() as session:
                released = TaskClaimReleaseService(session).release(
                    task.id, container_id=task.container_id
                )
            if released is None:
                continue
            self.services.tasks.publish_lifecycle_change(released, WorkspaceChangeType.Updated)
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
        return scheduled

    def expire_timed_out_tasks(self, *, now: datetime, limit: int = 100) -> None:
        with self.services.context.database.session() as session:
            attempts = TaskAttemptRepository(session).expired_function_attempts(
                now=now, limit=limit
            )
        for attempt in attempts:
            try:
                outcome = self.services.tasks.finish_with_retry(
                    attempt.task_id,
                    TaskStatus.Timeout,
                    container_id=attempt.container_id,
                    attempt_number=attempt.attempt_number,
                    error="function execution timed out",
                    exit_code=124,
                )
                if outcome.state_changed and is_terminal_task_status(outcome.task.status):
                    self.release_dependents(outcome.task)
            except DomainError:
                LOGGER.exception("expiring function task %s failed", attempt.task_id)
        # Attempts retain the container after a retry releases its claim. Read
        # them again so a scheduler restart cannot lose the stop obligation.
        with self.services.context.database.session() as session:
            containers = TaskAttemptRepository(session).containers_with_timed_out_attempts(
                limit=limit
            )
        for container_id in containers:
            try:
                self.services.containers.stop(
                    container_id, reason=StopContainerReason.Scheduler, force=True
                )
            except DomainError:
                LOGGER.exception("stopping timed-out function container %s failed", container_id)

    def fail_unclaimed_tasks(
        self,
        stub_id: str,
        *,
        error: str,
        limit: int = 100,
    ) -> int:
        failed = self.services.tasks.fail_unclaimed_claimable_for_stub(
            stub_id,
            error=error,
            limit=limit,
        )
        for task in failed:
            self.release_dependents(task)
        return len(failed)

    def _reserve_function_container(
        self,
        task: Task | None,
        *,
        container_plan: planning.FunctionContainerStartPlan,
        stub_id: str,
        stub_name: str,
        stub_workspace_id: str,
        stub_app_id: str | None,
        region: ProductRegion | None,
        availability_zone: str,
        preemptible: bool,
        eligible_at: datetime | None,
        authority: FunctionContainerStartAuthority,
        max_containers: int,
        preview_timeout: int | None = None,
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
                        env=parse_environment(container_plan.env),
                        gpu=list(container_plan.gpu),
                        gpu_count=container_plan.gpu_count,
                        region=region,
                        availability_zone=availability_zone,
                        timeout_seconds=preview_timeout or 0,
                        expires_at=(
                            datetime.now(UTC) + timedelta(seconds=preview_timeout)
                            if preview_timeout
                            else None
                        ),
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
            container_id=task.container_id,
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
        keepalive_interval_seconds: float = 5.0,
    ) -> AsyncGenerator[FunctionInvokeResponse, None]:
        yield initial
        if initial.done or initial.exit_code != 0 or not initial.task_id or headless:
            return

        if self.task_changes is None:
            raise RuntimeError("function invocation notifications are not configured")
        task = await self._async_database().run_transaction(
            lambda session: TaskRepository(session).progress_snapshot(initial.task_id)
        )
        if task is None:
            yield FunctionInvokeResponse.from_result(
                task_id=initial.task_id,
                output=f"task not found: {initial.task_id}",
                done=True,
                exit_code=1,
            )
            return
        if not task.workspace_id:
            raise RuntimeError("function invocation has no workspace")
        # Subscribe before the snapshot: a commit during subscription is read from
        # PostgreSQL, and a later commit wakes the stream through Redis.
        async with (
            self.task_changes.follow(
                workspace_id=task.workspace_id, stub_id=task.stub_id or "", task_id=task.id
            ) as updates,
            aclosing(
                self._function_invoke_updates(
                    initial, updates, keepalive_interval_seconds=keepalive_interval_seconds
                )
            ) as responses,
        ):
            async for response in responses:
                yield response

    async def _function_invoke_updates(
        self,
        initial: FunctionInvokeResponse,
        updates: AsyncIterator[None],
        *,
        keepalive_interval_seconds: float,
    ) -> AsyncGenerator[FunctionInvokeResponse]:
        log_cursor: LogPageCursor | None = None
        last_status = ""
        last_progress: TaskPendingProgress | None = None
        last_progress_read = 0.0
        last_keepalive = time.monotonic()
        progress_read: asyncio.Task[dict[str, TaskPendingProgress | None]] | None = None
        progress_status: TaskStatus | None = None
        update_read: asyncio.Future[None] | None = None
        try:
            while True:
                try:
                    log_page, task, completed = await self._async_database().run_transaction(
                        lambda session, cursor=log_cursor: self._function_stream_page_in_session(
                            session, initial.task_id, cursor=cursor
                        )
                    )
                except NotFoundError as exc:
                    yield FunctionInvokeResponse.from_result(
                        task_id=initial.task_id, output=str(exc), done=True, exit_code=1
                    )
                    return
                for record in log_page.data:
                    log_cursor = record.cursor
                    last_keepalive = time.monotonic()
                    yield FunctionInvokeResponse.from_result(
                        task_id=initial.task_id,
                        output=_stream_log_output(record.entry.message),
                        stream=record.entry.stream,
                    )
                if log_page.next is not None:
                    continue
                progress = last_progress
                pending = task.status in {TaskStatus.Pending, TaskStatus.Retry}
                if progress_read is not None and progress_read.done():
                    try:
                        observed = progress_read.result()
                    except Exception:
                        LOGGER.exception("pending progress read failed for task %s", task.id)
                    else:
                        if progress_status is task.status:
                            progress = observed[task.id]
                    progress_read = None
                if not pending:
                    progress = None
                    if progress_read is not None:
                        progress_read.cancel()
                        progress_read = None
                if (
                    pending
                    and progress_read is None
                    and (
                        time.monotonic() - last_progress_read >= 1.0
                        or progress_status is not task.status
                    )
                ):
                    progress_status = task.status
                    last_progress_read = time.monotonic()
                    progress_read = asyncio.create_task(
                        asyncio.to_thread(self.services.tasks.progress.read, [task])
                    )
                # Progress is advisory. A queued read must never hold a committed
                # result behind an executor or database connection.
                if (not pending or progress_read is None or last_status) and (
                    task.status.value != last_status or progress != last_progress
                ):
                    last_status = task.status.value
                    last_progress = progress
                    last_keepalive = time.monotonic()
                    yield FunctionInvokeResponse.from_result(
                        task_id=initial.task_id,
                        status=last_status,
                        pending_progress=progress,
                    )
                if is_terminal_task_status(task.status):
                    if completed is None:
                        raise NotFoundError(f"task not found: {task.id}")
                    yield FunctionInvokeResponse.from_result(
                        task_id=task.id,
                        result=(
                            _function_result_payload(completed)
                            if task.status is TaskStatus.Complete
                            else None
                        ),
                        output=completed.error or "",
                        done=True,
                        exit_code=task.exit_code
                        if task.exit_code is not None
                        else 0
                        if task.status is TaskStatus.Complete
                        else 1,
                    )
                    return
                if time.monotonic() - last_keepalive >= max(keepalive_interval_seconds, 1.0):
                    last_keepalive = time.monotonic()
                    yield FunctionInvokeResponse.from_result(
                        task_id=initial.task_id,
                        status=last_status,
                        pending_progress=last_progress,
                    )
                if update_read is None:
                    update_read = asyncio.ensure_future(anext(updates))
                waiting: set[
                    asyncio.Future[None] | asyncio.Task[dict[str, TaskPendingProgress | None]]
                ] = {update_read}
                if progress_read is not None:
                    waiting.add(progress_read)
                await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if update_read.done():
                    update_read.result()
                    update_read = None
        finally:
            reads = [read for read in (progress_read, update_read) if read is not None]
            for read in reads:
                read.cancel()
            # Join the notification reader before its subscription closes, even
            # when the response's cancellation scope has already been cancelled.
            with CancelScope(shield=True):
                if progress_read is not None:
                    await asyncio.gather(progress_read, return_exceptions=True)
                if update_read is not None:
                    await asyncio.gather(update_read, return_exceptions=True)

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
    ) -> tuple[LogPage, TaskProgressSnapshot, TaskResultSnapshot | None]:
        tasks = self.services.tasks
        task = TaskRepository(session).progress_snapshot(task_id)
        if task is None:
            raise NotFoundError(f"task not found: {task_id}")
        log_page = tasks.log_page_in_session(session, task, limit=1_000, cursor=cursor, follow=True)
        completed = (
            TaskRepository(session).result_snapshot(task_id)
            if is_terminal_task_status(task.status) and log_page.next is None
            else None
        )
        return log_page, task, completed

    async def function_claim_wait(
        self, request: FunctionClaimRequest, *, workspace_id: str
    ) -> FunctionClaimResponse:
        if not request.wait_seconds:
            return await asyncio.to_thread(self.function_claim, request, workspace_id=workspace_id)
        if self.task_changes is None:
            raise RuntimeError("function claim notifications are not configured")
        # Subscribe before reading SQL so a commit between the read and wait
        # cannot lose its notification. SQL remains the claim authority.
        async with self.task_changes.follow_claims(
            workspace_id=workspace_id, stub_id=request.stub_id
        ) as updates:
            deadline = time.monotonic() + request.wait_seconds
            while True:
                response, outcome = await asyncio.to_thread(
                    self._function_claim_attempt, request, workspace_id=workspace_id
                )
                updates.completed(
                    claimed=outcome.task is not None, queue_checked=outcome.queue_checked
                )
                if response.task is not None:
                    return response
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return response
                try:
                    async with asyncio.timeout(remaining):
                        await anext(updates)
                except TimeoutError:
                    return FunctionClaimResponse()

    def function_claim(
        self, request: FunctionClaimRequest, *, workspace_id: str
    ) -> FunctionClaimResponse:
        response, _ = self._function_claim_attempt(request, workspace_id=workspace_id)
        return response

    def _function_claim_attempt(
        self, request: FunctionClaimRequest, *, workspace_id: str
    ) -> tuple[FunctionClaimResponse, TaskClaimOutcome]:
        outcome = self.services.tasks.claim_and_start(
            request.stub_id,
            workspace_id=workspace_id,
            container_id=request.container_id,
            claim_id=request.claim_id,
        )
        task = outcome.task
        if task is None:
            return FunctionClaimResponse(), outcome
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
            return FunctionClaimResponse(), outcome
        validate_function_dependency_bindings(task.dependency_bindings)
        return FunctionClaimResponse(
            task=FunctionClaimedTask(
                task_id=task.id,
                root_task_id=task.root_task_id or task.id,
                attempt_number=task.attempt_number,
                max_attempts=task.max_attempts,
                invocation=task.invocation,
                dependencies=task.dependency_bindings,
            )
        ), outcome

    def function_retire(
        self,
        request: FunctionRetireRequest,
        *,
        workspace_id: str,
    ) -> FunctionRetireResponse:
        stub = self.stubs.get_stub(request.stub_id, workspace=workspace_id)
        keep_warm = stub.config.runtime.keep_warm
        now = utc_now()
        with self.services.context.database.session() as session:
            containers = ContainerRepository(session)
            # Enqueue may reserve replacement capacity only after admission closes.
            containers.lock_stub_capacity(stub.id)
            drains = ContainerRolloutRepository(session)
            container = containers.get_across_workspaces(request.container_id)
            if (
                container is None
                or container.workspace_id != workspace_id
                or container.stub_id != stub.id
            ):
                raise NotFoundError("function container not found")
            if not drains.accepting_work(request.container_id, stub_id=stub.id):
                return FunctionRetireResponse(retired=True)
            if keep_warm < 0:
                return FunctionRetireResponse(retired=False)
            tasks = TaskRepository(session)
            if tasks.containers_with_inflight_work([container.id]):
                return FunctionRetireResponse(retired=False)
            if tasks.count_unclaimed_by_stub([stub.id]).get(stub.id, 0):
                return FunctionRetireResponse(retired=False)
            finished = TaskAttemptRepository(session).latest_finished_at_for_container(container.id)
            idle_since = finished or container.started_at or container.created_at
            if (now - idle_since).total_seconds() < keep_warm:
                return FunctionRetireResponse(retired=False)
            drains.prepare(container, serving_floor=0, now=now)
            return FunctionRetireResponse(retired=drains.close_admission(container.id, now=now))

    def function_set_result(
        self,
        request: FunctionSetResultBody,
        *,
        workspace_id: str,
    ) -> FunctionSetResultResponse:
        with self.services.context.database.session() as session:
            outcome = self.services.tasks.finish_in_transaction(
                session,
                request.task_id,
                TaskStatus.Complete,
                container_id=request.container_id,
                function_result=request.result,
                exit_code=0,
                execution_entry=request.execution_entry,
                claim_id=request.claim_id,
                function_workspace_id=workspace_id,
            )
            if outcome.state_changed:
                EventService.emit_in_session(
                    session,
                    "function.result.set",
                    level=EventLevel.Info,
                    resource_type="task",
                    resource_id=request.task_id,
                    message=f"stored function result for {request.task_id}",
                    data={"result_size_bytes": function_result_payload_size(request.result)},
                    workspace_id=workspace_id,
                )
        self.services.tasks.publish_finished(outcome)
        if not outcome.state_changed:
            return FunctionSetResultResponse(
                stored=False,
                status=outcome.task.status,
                claim_acknowledged=outcome.claim_acknowledged,
            )
        if is_terminal_task_status(outcome.task.status):
            self.release_dependents(outcome.task)
        return FunctionSetResultResponse(
            status=outcome.task.status, claim_acknowledged=outcome.claim_acknowledged
        )

    def function_monitor(
        self, request: FunctionMonitorRequest, *, workspace_id: str
    ) -> FunctionMonitorResponse:
        with self.services.context.database.session() as session:
            task = TaskRepository(session).function_state(
                request.task_id, workspace_id=workspace_id, stub_id=request.stub_id
            )
        if task is None:
            raise NotFoundError("function task not found")
        plan = plan_function_monitor(
            planning.FunctionMonitorRequest(
                workspace_id=workspace_id,
                stub_id=request.stub_id,
                container_id=request.container_id,
                task_id=request.task_id,
                task_status=task.status,
                claimed=task.container_id == request.container_id,
                cancellation_requested=task.status is TaskStatus.Cancelled,
                timeout_elapsed=task.status is TaskStatus.Timeout,
            )
        )
        return FunctionMonitorResponse(
            cancelled=plan.cancelled,
            complete=plan.complete,
            timed_out=plan.timed_out,
        )


def _function_result_payload(task: TaskResultSnapshot) -> FunctionResultPayload:
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
