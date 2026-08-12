"""Scheduler-facing orchestration for worker-hosted image builds."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol

from images.building import (
    ImageBuildStreamEventPlan,
    plan_image_build_failure_event,
    plan_image_build_log_event,
    registry_auth_file_entry,
    unmarshal_registry_credentials,
)
from images.execution import (
    ImageBuildExecutionRequest,
    ImageBuildExecutionResult,
    ImageBuildExecutorKind,
    emit_image_build_event,
)
from images.lifecycle import ImageBuildContainerStateStore
from images.scheduling import (
    DEFAULT_IMAGE_BUILD_CONTAINER_ADDRESS_POLL_SECONDS,
    DEFAULT_IMAGE_BUILD_CONTAINER_ADDRESS_WAIT_SECONDS,
    DEFAULT_IMAGE_BUILD_CONTAINER_CPU_MILLICORES,
    DEFAULT_IMAGE_BUILD_CONTAINER_MEMORY_MIB,
    IMAGE_BUILD_REQUEST_KIND,
    plan_image_build_container_request,
)
from shared.errors import DomainError
from shared.image_building.records import BuildStatus
from shared.scheduling import SchedulerContainerSubmitResult, SchedulerWorkerRequest
from worker.repository_payloads import ImageBuildPrivateInputs, ImageBuildRegistryAuth

from worker_repository.image_build_container_execution import (
    ImageBuildContainerExecutorFactoryResult,
    ImageBuildContainerExecutorFactoryStatus,
)
from worker_repository.image_build_credentials import (
    ImageBuildCredentialLease,
    ImageBuildCredentialStore,
)


class ImageBuildContainerRequestScheduler(Protocol):
    def submit(self, request: SchedulerWorkerRequest) -> SchedulerContainerSubmitResult: ...


class ImageBuildSolvency(Protocol):
    """Whether this workspace's account may start a build.

    A build is compute the platform pays for like any other, and it reaches the
    worker without ever creating a container record — so the gate every other
    workload passes through is one it would otherwise walk around.
    """

    def assert_solvent(self, *, workspace_id: str) -> None: ...


class ImageBuildContainerExecutorFactory(Protocol):
    def create(self, container_id: str) -> ImageBuildContainerExecutorFactoryResult: ...


type ImageBuildSleep = Callable[[float], None]


@dataclass(slots=True)
class SchedulerImageBuildExecutor:
    cache_markers: ClassVar[frozenset[str]] = frozenset(
        {ImageBuildExecutorKind.BuildContainer.value, "container-client"}
    )
    requires_archive_publication: ClassVar[bool] = True

    scheduler: ImageBuildContainerRequestScheduler
    executor_factory: ImageBuildContainerExecutorFactory
    pending_container_state: ImageBuildContainerStateStore
    workspace_id: str = ""
    stub_id: str = IMAGE_BUILD_REQUEST_KIND
    pool_selector: str = ""
    cpu_millicores: int = DEFAULT_IMAGE_BUILD_CONTAINER_CPU_MILLICORES
    memory_mib: int = DEFAULT_IMAGE_BUILD_CONTAINER_MEMORY_MIB
    address_wait_timeout_seconds: float = DEFAULT_IMAGE_BUILD_CONTAINER_ADDRESS_WAIT_SECONDS
    address_poll_interval_seconds: float = DEFAULT_IMAGE_BUILD_CONTAINER_ADDRESS_POLL_SECONDS
    sleep: ImageBuildSleep = time.sleep
    credential_cache: ImageBuildCredentialStore | None = None
    solvency: ImageBuildSolvency | None = None
    """Absent only where nothing composes one. A deployment that bills has it, and
    a build then costs the account nothing it has not agreed to pay for."""

    def execute(self, request: ImageBuildExecutionRequest) -> ImageBuildExecutionResult:
        events = [
            _log_event(request, "submitting build container request"),
        ]
        workspace_id = self.workspace_id or request.workspace_id
        if not workspace_id:
            return _failure_result(
                request,
                events,
                "image build workspace id is required",
            )

        plan = plan_image_build_container_request(
            request,
            workspace_id=workspace_id,
            stub_id=self.stub_id,
            pool_selector=self.pool_selector,
            cpu_millicores=self.cpu_millicores,
            memory_mib=self.memory_mib,
        )
        credential_cache_key = plan.credential_metadata.cache_key
        try:
            private_inputs = _private_inputs(request)
        except Exception as exc:
            return self._failure_before_connection(
                request,
                events,
                f"image build private input validation failed ({type(exc).__name__})",
            )
        credentials_staged = False
        if not private_inputs.empty:
            if self.credential_cache is None:
                return self._failure_before_connection(
                    request,
                    events,
                    "image build source credentials require a credential cache",
                )
            try:
                self.credential_cache.put(
                    credential_cache_key,
                    ImageBuildCredentialLease(
                        workspace_id=workspace_id,
                        build_id=request.build_id,
                        container_id=request.session.container_id,
                        registry=plan.credential_metadata.registry,
                        private_inputs=private_inputs,
                    ),
                )
                credentials_staged = True
            except Exception as exc:
                return self._failure_before_connection(
                    request,
                    events,
                    f"image build credential staging failed ({type(exc).__name__})",
                )

        if self.solvency is not None:
            try:
                self.solvency.assert_solvent(workspace_id=workspace_id)
            except DomainError as exc:
                # Before the container request, so a refused build reserves no
                # worker and leaves no pending state behind it.
                return self._failure_before_connection(request, events, str(exc))
        try:
            try:
                scheduled = self.scheduler.submit(plan.scheduler_request)
            except Exception as exc:
                return self._failure_before_connection(
                    request,
                    events,
                    f"image build container request submission failed ({type(exc).__name__})",
                )
            events.append(_log_event(request, scheduled.reason))
            if not scheduled.accepted:
                return self._failure_before_connection(
                    request,
                    events,
                    scheduled.reason or "image build container request failed",
                )
            try:
                created = self._wait_for_executor(request.session.container_id)
            except Exception as exc:
                return self._failure_before_connection(
                    request,
                    events,
                    f"image build container address resolution failed: {exc}",
                )
            if not created.ready or created.executor is None:
                return self._failure_before_connection(
                    request,
                    events,
                    created.reason or "image build container address was not ready",
                )

            result = created.executor.execute(request)
            return result.model_copy(
                update={
                    "events": [*events, *result.events],
                    "cache_metadata": {
                        "scheduler_submit_status": scheduled.status.value,
                        "container_service_url": created.service_url,
                        **result.cache_metadata,
                    },
                }
            )
        finally:
            if credentials_staged and self.credential_cache is not None:
                self.credential_cache.delete(credential_cache_key)

    def _failure_before_connection(
        self,
        request: ImageBuildExecutionRequest,
        events: list[ImageBuildStreamEventPlan],
        reason: str,
    ) -> ImageBuildExecutionResult:
        try:
            cancelled = self.pending_container_state.delete_pending_build_container(
                request.session.container_id
            )
        except Exception as exc:
            cancellation_status = "error"
            cancellation_reason = f"{type(exc).__name__}: {exc}"
        else:
            cancellation_status = "complete" if cancelled else "not-found"
            cancellation_reason = ""
        result = _failure_result(request, events, reason)
        return result.model_copy(
            update={
                "cache_metadata": {
                    **result.cache_metadata,
                    "scheduler_cancel_status": cancellation_status,
                    "scheduler_cancel_reason": cancellation_reason,
                }
            }
        )

    def _wait_for_executor(self, container_id: str) -> ImageBuildContainerExecutorFactoryResult:
        deadline = _deadline(self.address_wait_timeout_seconds)
        result = self.executor_factory.create(container_id)
        while (
            result.status is ImageBuildContainerExecutorFactoryStatus.MissingAddress
            and not _deadline_reached(deadline)
        ):
            self.sleep(max(self.address_poll_interval_seconds, 0.0))
            result = self.executor_factory.create(container_id)
        return result


def _private_inputs(request: ImageBuildExecutionRequest) -> ImageBuildPrivateInputs:
    registry_auth: ImageBuildRegistryAuth | None = None
    if request.registry_credential_payload:
        payload = unmarshal_registry_credentials(request.registry_credential_payload)
        entry = registry_auth_file_entry(payload)
        registry_auth = ImageBuildRegistryAuth(
            registry=payload.registry,
            auth=entry.get("auth", ""),
            identity_token=entry.get("identitytoken", ""),
        )
    return ImageBuildPrivateInputs(
        registry_auth=registry_auth,
        build_args=request.build_args,
    )


def _deadline(timeout_seconds: float) -> float:
    return time.monotonic() + max(timeout_seconds, 0.0)


def _deadline_reached(deadline: float) -> bool:
    return time.monotonic() >= deadline


def _failure_result(
    request: ImageBuildExecutionRequest,
    events: list[ImageBuildStreamEventPlan],
    reason: str,
) -> ImageBuildExecutionResult:
    message = reason.strip() or "image build container execution failed"
    return ImageBuildExecutionResult(
        status=BuildStatus.Failed,
        events=[
            *events,
            plan_image_build_failure_event(
                message,
                image_id=request.image_id,
                build_id=request.build_id,
                python_version=request.plan.spec.python_version,
            ),
        ],
        reason=message,
    )


def _log_event(
    request: ImageBuildExecutionRequest,
    message: str,
) -> ImageBuildStreamEventPlan:
    return emit_image_build_event(
        request,
        plan_image_build_log_event(
            message.strip(),
            image_id=request.image_id,
            build_id=request.build_id,
            python_version=request.plan.spec.python_version,
        ),
    )
