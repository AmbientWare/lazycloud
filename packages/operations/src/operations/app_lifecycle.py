from __future__ import annotations

from dataclasses import dataclass

from coordination.redis_client import RedisClient
from database.context import ServiceContext
from database.records.apps import StubRecord
from database.repositories.apps import StubRepository
from database.repositories.execution import QueueRepository, TaskRepository
from execution.containers.service import ContainerService
from execution.endpoints.keys import endpoint_instance_lock_key, endpoint_serve_lock_key
from execution.pods.planning import pod_instance_lock_key
from execution.taskqueues.planning import (
    task_queue_instance_lock_key,
    task_queue_list_key,
    task_queue_scheduler_serve_lock_key,
)
from execution.tasks import TaskService
from shared.container_requests import ContainerShutdownTarget
from shared.deployments import StubKind
from shared.tasks import TaskStatus

from operations.container_shutdown import ContainerShutdownService


@dataclass(frozen=True, slots=True)
class ProductionAppExecutionLifecycleEffects:
    context: ServiceContext
    containers: ContainerService
    tasks: TaskService
    redis: RedisClient
    shutdowns: ContainerShutdownService
    shutdown_timeout_seconds: float = 30.0

    def stop_app_containers(
        self,
        *,
        workspace_id: str,
        app_id: str,
        container_targets: list[ContainerShutdownTarget],
    ) -> None:
        del workspace_id, app_id
        for target in container_targets:
            self.containers.stop(target.container_id)
        self.shutdowns.confirm(
            container_targets,
            timeout_seconds=self.shutdown_timeout_seconds,
        )

    def delete_app_execution(self, *, workspace_id: str, app_id: str) -> None:
        with self.context.database.session() as session:
            workspace_name = self.context.workspace(session, workspace_id).name
            stubs = StubRepository(session).list_for_app(
                workspace_id=workspace_id,
                app_id=app_id,
            )
            task_queue_stub_ids = {stub.id for stub in stubs if stub.kind is StubKind.TaskQueue}
            tasks = TaskRepository(session).list_inflight_for_stubs(
                workspace_id=workspace_id,
                stub_ids=task_queue_stub_ids,
            )
        for task in tasks:
            self.tasks.transition(
                task,
                TaskStatus.Cancelled,
                error="owning app deleted",
            )
        if task_queue_stub_ids:
            with self.context.database.session() as session:
                QueueRepository(session).delete_messages_for_queues(
                    workspace_id=workspace_id,
                    queues={f"taskqueue:{stub_id}" for stub_id in task_queue_stub_ids},
                )
        self._delete_app_ephemeral_state(
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            stubs=stubs,
        )

    def _delete_app_ephemeral_state(
        self,
        *,
        workspace_id: str,
        workspace_name: str,
        stubs: list[StubRecord],
    ) -> None:
        keys: set[str] = set()
        for stub in stubs:
            if stub.kind is StubKind.TaskQueue:
                keys.update(self._stub_root_keys(task_queue_list_key(workspace_name, stub.id)))
                keys.add(self.redis.key(task_queue_instance_lock_key(workspace_name, stub.id)))
                keys.add(
                    self.redis.key(task_queue_scheduler_serve_lock_key(workspace_name, stub.id))
                )
                keys.add(
                    self.redis.key(
                        "autoscaling",
                        "taskqueues",
                        workspace_id,
                        stub.id,
                        "lock",
                    )
                )
            elif stub.kind in {StubKind.Endpoint, StubKind.Asgi}:
                keys.update(self._stub_root_keys(f"endpoint:{workspace_name}:{stub.id}"))
                keys.add(self.redis.key(endpoint_instance_lock_key(workspace_name, stub.id)))
                keys.add(self.redis.key(endpoint_serve_lock_key(workspace_name, stub.id)))
                keys.add(
                    self.redis.key(
                        "autoscaling",
                        "endpoints",
                        workspace_id,
                        stub.id,
                        "lock",
                    )
                )
            elif stub.kind in {StubKind.Pod, StubKind.Sandbox}:
                keys.update(self._stub_root_keys(f"pod:{workspace_name}:{stub.id}"))
                keys.add(self.redis.key(pod_instance_lock_key(workspace_name, stub.id)))
                keys.add(
                    self.redis.key(
                        "autoscaling",
                        "pods",
                        workspace_id,
                        stub.id,
                        "lock",
                    )
                )
        if keys:
            self.redis.delete(*sorted(keys))

    def _stub_root_keys(self, logical_root: str) -> set[str]:
        root = self.redis.key(logical_root)
        return {root, *self.redis.scan(f"{root}:*")}


__all__ = ["ProductionAppExecutionLifecycleEffects"]
