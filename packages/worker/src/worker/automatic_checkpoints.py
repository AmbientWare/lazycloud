from __future__ import annotations

import http.client
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from shared.checkpoints import AutomaticCheckpointCreationLease
from shared.container_requests import WorkerStartupKind

from worker.checkpoints import (
    DEFAULT_CHECKPOINT_SIGNAL_ROOT,
    CheckpointLifecycleAction,
    CheckpointSignalWaitAction,
    checkpoint_signal_dir,
    plan_auto_checkpoint,
    plan_checkpoint_signal_mount,
    plan_checkpoint_signal_wait,
)
from worker.container_execution import ContainerExecutionContext, ContainerMountSetupResult
from worker.container_service.protocols import (
    WorkerContainerCheckpointCreator,
    WorkerContainerInstanceStore,
)
from worker.runtime_config import RuntimeContainerStatus, runtime_capabilities

CHECKPOINT_RUNNER_KINDS = {
    WorkerStartupKind.Endpoint,
    WorkerStartupKind.Asgi,
    WorkerStartupKind.TaskQueue,
}
CHECKPOINT_HTTP_KINDS = {WorkerStartupKind.Pod, WorkerStartupKind.PodRun}
AUTOMATIC_CHECKPOINT_CREATION_GRACE_SECONDS = 30 * 60


class AutomaticCheckpointCreationLeaseCoordinator(Protocol):
    def acquire(
        self,
        *,
        workspace_id: str,
        stub_id: str,
        owner_token: str,
        ttl_seconds: int,
    ) -> AutomaticCheckpointCreationLease: ...

    def release(
        self,
        *,
        workspace_id: str,
        stub_id: str,
        owner_token: str,
    ) -> bool: ...


class AutomaticCheckpointRuntimeState(Protocol):
    def status(self, container_id: str) -> str: ...


@dataclass(slots=True)
class WorkerAutomaticCheckpointService:
    instances: WorkerContainerInstanceStore
    creator: WorkerContainerCheckpointCreator
    leases: AutomaticCheckpointCreationLeaseCoordinator
    runtime: AutomaticCheckpointRuntimeState
    signal_root: str = DEFAULT_CHECKPOINT_SIGNAL_ROOT

    def prepare_mount(
        self,
        context: ContainerExecutionContext,
        mount_result: ContainerMountSetupResult,
    ) -> ContainerMountSetupResult:
        if not context.checkpoint_enabled and not context.checkpoint_id:
            return mount_result
        self._validate(context)
        signal = plan_checkpoint_signal_mount(
            container_id=context.request.container_id,
            container_hostname=socket.gethostname(),
            root=self.signal_root,
        )
        signal_dir = Path(signal.signal_dir)
        signal_dir.mkdir(parents=True, exist_ok=True)
        Path(signal.ready_file).unlink(missing_ok=True)
        Path(signal.complete_file).unlink(missing_ok=True)
        for name, value in signal.file_writes.items():
            (signal_dir / name).write_text(value, encoding="utf-8")
        return mount_result.model_copy(
            update={"oci_mounts": [*mount_result.oci_mounts, signal.mount]}
        )

    def checkpoint_or_complete_restore(
        self,
        context: ContainerExecutionContext,
        *,
        container_hostname: str,
    ) -> str:
        if context.checkpoint_id:
            self._complete(context.request.container_id, container_hostname=container_hostname)
            return context.checkpoint_id
        decision = plan_auto_checkpoint(
            checkpoint_enabled=context.checkpoint_enabled,
            supports_checkpoint=runtime_capabilities(context.runtime).checkpoint_restore,
        )
        if decision.action is CheckpointLifecycleAction.Skip:
            return ""
        owner_token = context.request.container_id
        lease = self.leases.acquire(
            workspace_id=context.request.workspace_id,
            stub_id=context.request.stub_id,
            owner_token=owner_token,
            ttl_seconds=(
                context.checkpoint_readiness_timeout_seconds
                + AUTOMATIC_CHECKPOINT_CREATION_GRACE_SECONDS
            ),
        )
        if not lease.acquired:
            self._complete(
                context.request.container_id,
                container_hostname=container_hostname,
            )
            return lease.available_checkpoint_id
        try:
            self._wait_until_ready(context)
            instance = self.instances.get_container_instance(context.request.container_id)
            if instance is None:
                raise RuntimeError(
                    "container instance disappeared before checkpoint: "
                    f"{context.request.container_id}"
                )
            checkpoint_id = self.creator.create_checkpoint(instance)
            self._complete(context.request.container_id, container_hostname=container_hostname)
            return checkpoint_id
        finally:
            self.leases.release(
                workspace_id=context.request.workspace_id,
                stub_id=context.request.stub_id,
                owner_token=owner_token,
            )

    def cleanup(self, container_id: str) -> None:
        signal_dir = Path(checkpoint_signal_dir(container_id, root=self.signal_root))
        shutil.rmtree(signal_dir.parent, ignore_errors=True)

    def _validate(self, context: ContainerExecutionContext) -> None:
        if context.request.gpu_count > 1:
            raise RuntimeError("checkpointing does not support more than one GPU")
        if context.startup_kind not in CHECKPOINT_RUNNER_KINDS | CHECKPOINT_HTTP_KINDS | {
            WorkerStartupKind.Sandbox
        }:
            raise RuntimeError(
                f"checkpointing is not supported for {context.startup_kind.value} workloads"
            )
        if context.checkpoint_enabled and context.startup_kind in CHECKPOINT_HTTP_KINDS:
            if not context.checkpoint_readiness_path.startswith("/"):
                raise RuntimeError("Pod checkpoint readiness path must be absolute")
            if context.checkpoint_readiness_port <= 0:
                raise RuntimeError("Pod checkpoint readiness port is required")
        if context.checkpoint_enabled and (
            not context.request.workspace_id or not context.request.stub_id
        ):
            raise RuntimeError("automatic checkpoint creation requires workspace and stub identity")

    def _wait_until_ready(self, context: ContainerExecutionContext) -> None:
        deadline = time.monotonic() + context.checkpoint_readiness_timeout_seconds
        while True:
            runtime_status = self.runtime.status(context.request.container_id)
            if runtime_status != RuntimeContainerStatus.Running.value:
                raise RuntimeError(
                    "container runtime exited before checkpoint readiness: "
                    f"{context.request.container_id} ({runtime_status})"
                )
            if context.startup_kind in CHECKPOINT_HTTP_KINDS:
                ready = self._http_ready(context)
            else:
                ready = (
                    Path(
                        checkpoint_signal_dir(
                            context.request.container_id,
                            root=self.signal_root,
                        )
                    )
                    .joinpath("READY_FOR_CHECKPOINT")
                    .is_file()
                )
            plan = plan_checkpoint_signal_wait(
                container_id=context.request.container_id,
                ready_file_exists=ready,
                container_known=(
                    self.instances.get_container_instance(context.request.container_id) is not None
                ),
                deadline_exceeded=time.monotonic() >= deadline,
                root=self.signal_root,
                deadline_seconds=context.checkpoint_readiness_timeout_seconds,
            )
            if plan.action is CheckpointSignalWaitAction.Ready:
                return
            if plan.action is CheckpointSignalWaitAction.Timeout:
                raise RuntimeError(plan.reason)
            time.sleep(context.checkpoint_readiness_interval_seconds)

    def _http_ready(self, context: ContainerExecutionContext) -> bool:
        instance = self.instances.get_container_instance(context.request.container_id)
        if instance is None or not instance.container_ip:
            return False
        host = (
            f"[{instance.container_ip}]" if ":" in instance.container_ip else instance.container_ip
        )
        connection = http.client.HTTPConnection(
            host,
            context.checkpoint_readiness_port,
            timeout=min(context.checkpoint_readiness_interval_seconds, 5.0),
        )
        try:
            connection.request("GET", context.checkpoint_readiness_path)
            response = connection.getresponse()
            return 200 <= response.status < 400
        except OSError:
            return False
        finally:
            connection.close()

    def _complete(self, container_id: str, *, container_hostname: str) -> None:
        signal_dir = Path(checkpoint_signal_dir(container_id, root=self.signal_root))
        signal_dir.mkdir(parents=True, exist_ok=True)
        (signal_dir / "CONTAINER_ID").write_text(container_id, encoding="utf-8")
        (signal_dir / "CONTAINER_HOSTNAME").write_text(container_hostname, encoding="utf-8")
        (signal_dir / "CHECKPOINT_COMPLETE").touch(exist_ok=True)
