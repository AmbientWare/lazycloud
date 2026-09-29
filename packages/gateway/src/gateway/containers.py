from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from coordination.redis_client import AsyncRedisClient
from database.repositories.execution import LogRepository
from database.repositories.orchestration import ContainerRepository
from execution.containers.service import ContainerService
from observability.events import EventService
from observability.log_retention import LogRetentionService
from observability.stream_state import AsyncRedisEventStreamRepository
from shared.containers import ContainerRecord
from shared.errors import InvalidInputError, NotFoundError, UpstreamUnavailableError
from shared.http.gateway import (
    AttachToContainerRequest,
    AttachToContainerResponse,
    CheckpointContainerRequest,
    CheckpointContainerResponse,
)
from shared.http.workspace_sync import WorkspaceSyncBatch, WorkspaceSyncResponse
from shared.logs import LogEntry
from shared.realtime.streams import LogStreamQuery
from sqlalchemy.orm import Session
from worker.container_client.scheduler import SchedulerContainerClientFactory

from database import AsyncDatabaseClient
from gateway.payloads import CONTAINER_OUTPUT_LOG_LIMIT, container_output


@dataclass(frozen=True, slots=True)
class GatewayContainerService:
    containers: ContainerService
    events: EventService
    container_client_factory: SchedulerContainerClientFactory

    def checkpoint_container(
        self,
        request: CheckpointContainerRequest,
        *,
        workspace_id: str,
    ) -> CheckpointContainerResponse:
        try:
            container = self._container_for_workspace(request.container_id, workspace_id)
            response = self.container_client_factory.client_for(container).client.checkpoint(
                container.id,
                checkpoint_id=request.checkpoint_id or str(uuid4()),
            )
            if not response.ok:
                raise InvalidInputError(response.error_msg or "container checkpoint failed")
            if not response.checkpoint_id:
                raise UpstreamUnavailableError("container worker did not return a checkpoint id")
            self.events.emit(
                "container.checkpoint",
                resource_type="container",
                resource_id=container.id,
                message=f"created checkpoint for container {container.name}",
                data={"checkpoint_id": response.checkpoint_id},
                workspace_id=container.workspace_id,
            )
        except KeyError as exc:
            raise NotFoundError(str(exc).strip("\"'")) from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        except RuntimeError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        return CheckpointContainerResponse(checkpoint_id=response.checkpoint_id)

    async def attach_to_container(
        self,
        request: AttachToContainerRequest,
        *,
        workspace_id: str,
        database: AsyncDatabaseClient,
        redis: AsyncRedisClient,
    ) -> AttachToContainerResponse:
        try:
            container, task_logs = await database.run_transaction(
                lambda session: self._container_output_state(
                    session,
                    request.container_id,
                    workspace_id,
                )
            )
        except KeyError as exc:
            raise NotFoundError(str(exc).strip("\"'")) from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        output = await container_output(
            container,
            task_logs=task_logs,
            logs=AsyncRedisEventStreamRepository(redis),
        )
        done = container.finished_at is not None
        return AttachToContainerResponse(
            output=output,
            done=done,
            exit_code=container.exit_code,
        )

    @staticmethod
    def _container_output_state(
        session: Session,
        container_id: str,
        workspace_id: str,
    ) -> tuple[ContainerRecord, tuple[LogEntry, ...]]:
        container = ContainerRepository(session).get(container_id, workspace_id=workspace_id)
        if container is None:
            raise NotFoundError(f"container not found: {container_id}")
        if not container.task_id:
            return container, ()
        page = LogRepository(session).page(
            LogStreamQuery(
                workspace_id=workspace_id,
                task_id=container.task_id,
                start_time=LogRetentionService.cutoff_in_session(session, workspace_id),
            ),
            workspace_id=workspace_id,
            limit=CONTAINER_OUTPUT_LOG_LIMIT,
        )
        return container, tuple(record.entry for record in page.data)

    def sync_container_workspace(
        self,
        request: WorkspaceSyncBatch,
        *,
        workspace_id: str,
    ) -> WorkspaceSyncResponse:
        try:
            container = self._container_for_workspace(request.manifest.container_id, workspace_id)
            response = self.container_client_factory.client_for(container).client.sync_workspace(
                request
            )
        except KeyError as exc:
            raise NotFoundError(str(exc).strip("\"'")) from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        except RuntimeError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        if not response.ok:
            raise InvalidInputError(response.error_msg or "container workspace sync failed")
        return WorkspaceSyncResponse(applied=response.applied)

    def _container_for_workspace(
        self,
        container_id: str,
        workspace_id: str,
    ) -> ContainerRecord:
        container = self.containers.get(container_id)
        if container.workspace_id != workspace_id:
            msg = f"container not found: {container_id}"
            raise NotFoundError(msg)
        return container
