from __future__ import annotations

import logging
from dataclasses import dataclass

from coordination.redis_client import RedisClient
from database.context import ServiceContext
from database.records.apps import StubRecord
from database.repositories.apps import StubRepository
from database.repositories.execution import TaskRepository
from execution.containers.service import ContainerService
from execution.pods.planning import pod_instance_lock_key
from execution.tasks import TaskService
from shared.container_requests import ContainerShutdownTarget
from shared.deployments import StubKind

from operations.container_shutdown import ContainerShutdownService

LOGGER = logging.getLogger(__name__)


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
            stubs = StubRepository(session).list_for_app(
                workspace_id=workspace_id,
                app_id=app_id,
            )
            # Queued invocations go the way the containers above them just did.
            # Only deletion retires them: a paused app is also refused by app
            # admission, and its backlog is meant to be waiting for the resume.
            cancelled = TaskRepository(session).cancel_queued_for_app(
                workspace_id=workspace_id,
                app_id=app_id,
                error="the app this task belongs to was deleted",
            )
        if cancelled:
            LOGGER.info(
                "app %s deletion cancelled %d queued task(s)",
                app_id,
                len(cancelled),
            )
        self._delete_app_ephemeral_state(workspace_id=workspace_id, stubs=stubs)

    def _delete_app_ephemeral_state(
        self,
        *,
        workspace_id: str,
        stubs: list[StubRecord],
    ) -> None:
        keys: set[str] = set()
        for stub in stubs:
            if stub.kind in {StubKind.Endpoint, StubKind.Asgi}:
                keys.update(self._stub_root_keys(f"endpoint:{workspace_id}:{stub.id}"))
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
                keys.update(self._stub_root_keys(f"pod:{workspace_id}:{stub.id}"))
                keys.add(self.redis.key(pod_instance_lock_key(workspace_id, stub.id)))
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
