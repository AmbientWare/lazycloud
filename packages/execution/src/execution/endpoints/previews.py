import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from database.records.apps import StubRecord
from database.repositories.apps import StubRepository
from database.repositories.cleanup import CleanupRepository
from database.repositories.container_rollouts import ContainerRolloutRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.orchestration import ContainerRepository
from database.repositories.previews import PreviewSessionRepository
from database.tables.previews import PreviewSessionTable
from shared.container_requests import StopContainerReason
from shared.containers import TERMINAL_CONTAINER_STATUSES
from shared.deployments import StubKind
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.previews import (
    PREVIEW_LEASE_SECONDS,
    CreatePreviewRequest,
    PreviewSessionResponse,
    PreviewSessionStatus,
)
from shared.timestamps import utc_now

from execution.services import EndpointExecutionServices

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PreviewSessionService:
    services: EndpointExecutionServices

    def lease_key(self, preview_id: str) -> str:
        return self.services.redis_client.key("preview", preview_id, "lease")

    def create(self, request: CreatePreviewRequest, *, workspace_id: str) -> PreviewSessionResponse:
        now = utc_now()
        preview_id = str(uuid4())
        with self.services.context.database.session() as session:
            WorkspaceRepository(session).lock_active_owner(workspace_id)
            stubs = StubRepository(session)
            source = stubs.get_for_update(request.stub_id, workspace_id=workspace_id)
            if source is None:
                raise NotFoundError("preview source not found")
            if source.kind not in {StubKind.Endpoint, StubKind.Asgi}:
                raise InvalidInputError("preview source must be an endpoint or ASGI app")
            if PreviewSessionRepository(session).for_stub(source.id) is not None:
                raise InvalidInputError("a preview execution cannot be used as a preview source")
            CleanupRepository(session).assert_stub_config_available(
                source.config,
                workspace_id=workspace_id,
                metadata=source.metadata,
            )
            execution = source.model_copy(
                update={
                    "id": str(uuid4()),
                    "name": f"preview-{preview_id}",
                    "deployment_id": None,
                    "created_at": now,
                    "updated_at": now,
                },
                deep=True,
            )
            stubs.upsert(execution)
            row = PreviewSessionTable(
                id=preview_id,
                workspace_id=workspace_id,
                source_stub_id=source.id,
                execution_stub_id=execution.id,
                status=PreviewSessionStatus.Active.value,
                public=source.public,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(seconds=request.timeout) if request.timeout else None,
            )
            session.add(row)
            session.flush()
            if not self.services.redis_client.set(
                self.lease_key(preview_id), preview_id, ex=PREVIEW_LEASE_SECONDS, nx=True
            ):
                raise ConflictError("preview lease already exists")
            return PreviewSessionResponse.model_validate(row, from_attributes=True)

    def get(
        self, preview_id: str, *, workspace_id: str | None = None, public: bool = False
    ) -> PreviewSessionResponse:
        with self.services.context.database.session() as session:
            row = PreviewSessionRepository(session).get(preview_id)
            if (
                row is None
                or (workspace_id is not None and row.workspace_id != workspace_id)
                or (public and not row.public)
            ):
                raise NotFoundError("preview session not found")
            return PreviewSessionResponse.model_validate(row, from_attributes=True)

    def execution_stub(self, preview_id: str) -> StubRecord:
        record = self.get(preview_id)
        self.require_active(record)
        if record.execution_stub_id is None:
            raise NotFoundError("preview execution no longer exists")
        with self.services.context.database.session() as session:
            stub = StubRepository(session).records.get_across_workspaces(record.execution_stub_id)
            if stub is None:
                raise NotFoundError("preview execution no longer exists")
            return stub

    def for_stub(self, stub_id: str) -> PreviewSessionResponse | None:
        with self.services.context.database.session() as session:
            row = PreviewSessionRepository(session).for_stub(stub_id)
            return PreviewSessionResponse.model_validate(row, from_attributes=True) if row else None

    def require_active(
        self, record: PreviewSessionResponse, *, now: datetime | None = None
    ) -> None:
        current = now or utc_now()
        if record.status is not PreviewSessionStatus.Active:
            raise NotFoundError("preview session has ended")
        if (
            record.expires_at is not None and record.expires_at <= current
        ) or not self.services.redis_client.exists(self.lease_key(record.id)):
            self._end(record.id, workspace_id=record.workspace_id, expired=True)
            raise NotFoundError("preview session has expired")

    def renew(self, preview_id: str, *, workspace_id: str) -> PreviewSessionResponse:
        with self.services.context.database.session() as session:
            row = PreviewSessionRepository(session).get(preview_id, lock=True)
            if row is None or row.workspace_id != workspace_id:
                raise NotFoundError("preview session not found")
            record = PreviewSessionResponse.model_validate(row, from_attributes=True)
            active = record.status is PreviewSessionStatus.Active and (
                record.expires_at is None or record.expires_at > utc_now()
            )
            renewed = active and self.services.redis_client.expire(
                self.lease_key(record.id), PREVIEW_LEASE_SECONDS
            )
        if not renewed:
            self._end(preview_id, workspace_id=workspace_id, expired=True)
            raise NotFoundError("preview session has ended")
        return record

    def stop(
        self, preview_id: str, *, workspace_id: str, expired: bool = False
    ) -> PreviewSessionResponse:
        ended = self._end(preview_id, workspace_id=workspace_id, expired=expired)
        if ended.container_id:
            self.services.containers.stop(
                ended.container_id,
                reason=(
                    StopContainerReason.Scheduler
                    if ended.status is PreviewSessionStatus.Expired
                    else StopContainerReason.User
                ),
            )
        self.services.redis_client.delete(self.lease_key(ended.id))
        return ended

    def _end(self, preview_id: str, *, workspace_id: str, expired: bool) -> PreviewSessionResponse:
        now = utc_now()
        while True:
            record = self.get(preview_id, workspace_id=workspace_id)
            with self.services.context.database.session() as session:
                container = (
                    ContainerRepository(session).lock_across_workspaces(record.container_id)
                    if record.container_id
                    else None
                )
                row = PreviewSessionRepository(session).get(preview_id, lock=True)
                if row is None or row.workspace_id != workspace_id:
                    raise NotFoundError("preview session not found")
                # Container locks precede session locks. Startup may have bound a
                # container since the read; retry with its fence before ending.
                if row.container_id != record.container_id:
                    continue
                if row.status == PreviewSessionStatus.Active.value:
                    row.status = (
                        PreviewSessionStatus.Expired.value
                        if expired
                        else PreviewSessionStatus.Stopped.value
                    )
                    row.ended_at = now
                row.updated_at = now
                if container is not None:
                    drains = ContainerRolloutRepository(session)
                    drains.prepare(container, serving_floor=0, now=now)
                    drains.close_admission(container.id, now=now)
                ended = PreviewSessionResponse.model_validate(row, from_attributes=True)
            break
        return ended

    def reconcile(self, *, now: datetime | None = None, limit: int = 100) -> int:
        current = now or utc_now()
        with self.services.context.database.session() as session:
            records = PreviewSessionRepository(session).reconcile_candidates(limit=limit)
        stopped = 0
        for record in records:
            try:
                expired = False
                if record.status is PreviewSessionStatus.Active:
                    live = self.services.redis_client.exists(self.lease_key(record.id))
                    if record.container_id:
                        with self.services.context.database.session() as session:
                            container = ContainerRepository(session).get_across_workspaces(
                                record.container_id
                            )
                        live = (
                            live
                            and container is not None
                            and container.status not in TERMINAL_CONTAINER_STATUSES
                        )
                    expired = (
                        (record.expires_at is not None and record.expires_at <= current)
                        or not live
                        or record.execution_stub_id is None
                    )
                if record.status is not PreviewSessionStatus.Active or expired:
                    self.stop(record.id, workspace_id=record.workspace_id, expired=expired)
                    stopped += 1
            except Exception:
                LOGGER.exception("preview reconciliation failed", extra={"preview_id": record.id})
            finally:
                # A failed stop remains discoverable but cannot starve siblings.
                with self.services.context.database.session() as session:
                    row = PreviewSessionRepository(session).get(record.id, lock=True)
                    if row is not None:
                        row.updated_at = current
        return stopped
