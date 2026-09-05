from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from database.tables.images import ImageBuildRequestTable, ImageBuildTable
from shared.image_building.records import ImageBuildRecord
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ImageBuildDispatchClaim:
    build_id: str
    workspace_id: str
    claim_id: str
    payload: str
    created_at: datetime
    started_at: datetime | None


@dataclass(slots=True)
class ImageBuildDispatchRepository:
    session: Session

    def lock_failure_candidate(
        self,
        build_id: str,
        *,
        workspace_id: str,
        stale_before: datetime,
        pending_created_before: datetime,
        claim_id: str | None = None,
    ) -> ImageBuildRecord | None:
        statement = (
            select(ImageBuildTable)
            .where(
                ImageBuildTable.id == build_id,
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.status.in_(("pending", "running")),
                ImageBuildTable.updated_at < stale_before,
                (
                    ImageBuildTable.started_at.is_not(None)
                    | (ImageBuildTable.created_at < pending_created_before)
                ),
            )
            .with_for_update()
        )
        if claim_id is not None:
            statement = statement.where(
                ImageBuildTable.dispatch_claim_id == claim_id,
                ImageBuildTable.started_at.is_(None),
            )
        row = self.session.scalar(statement)
        return ImageBuildRecord.model_validate(row.payload) if row is not None else None

    def schedule_cleanup(self, build_id: str, *, after: datetime | None) -> None:
        self.session.execute(
            update(ImageBuildTable)
            .where(ImageBuildTable.id == build_id)
            .values(execution_cleanup_after=after)
        )

    def cleanup_due(
        self, *, now: datetime, limit: int, build_id: str | None = None
    ) -> list[tuple[str, str]]:
        statement = (
            select(ImageBuildTable.id, ImageBuildTable.workspace_id)
            .where(ImageBuildTable.execution_cleanup_after <= now)
            .order_by(ImageBuildTable.execution_cleanup_after)
            .limit(limit)
        )
        if build_id is not None:
            statement = statement.where(ImageBuildTable.id == build_id)
        rows = self.session.execute(statement)
        return [(str(row.id), str(row.workspace_id)) for row in rows]

    def lock_request(self, request_id: str, *, workspace_id: str) -> None:
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"image-build-request:{workspace_id}:{request_id}"},
        )

    def request_build_id(self, request_id: str, *, workspace_id: str) -> str | None:
        value = self.session.scalar(
            select(ImageBuildRequestTable.build_id).where(
                ImageBuildRequestTable.workspace_id == workspace_id,
                ImageBuildRequestTable.request_id == request_id,
            )
        )
        return str(value) if value is not None else None

    def bind_request(self, request_id: str, *, workspace_id: str, build_id: str) -> None:
        self.session.add(
            ImageBuildRequestTable(
                request_id=request_id, workspace_id=workspace_id, build_id=build_id
            )
        )
        self.session.flush()

    def payload(self, build_id: str, *, workspace_id: str) -> str | None:
        return self.session.scalar(
            select(ImageBuildTable.dispatch_payload).where(
                ImageBuildTable.id == build_id,
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.status.in_(("pending", "running")),
            )
        )

    def enqueue(self, build_id: str, payload: str, *, now: datetime) -> None:
        self.session.execute(
            update(ImageBuildTable)
            .where(ImageBuildTable.id == build_id, ImageBuildTable.dispatch_payload.is_(None))
            .values(dispatch_payload=payload, dispatch_after=now)
        )

    def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        build_id: str | None = None,
    ) -> list[ImageBuildDispatchClaim]:
        statement = (
            select(ImageBuildTable)
            .where(
                ImageBuildTable.dispatch_payload.is_not(None),
                ImageBuildTable.dispatched_at.is_(None),
                ImageBuildTable.dispatch_after <= now,
                ImageBuildTable.status.in_(("pending", "running")),
            )
            .order_by(ImageBuildTable.dispatch_after, ImageBuildTable.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if build_id is not None:
            statement = statement.where(ImageBuildTable.id == build_id)
        claims: list[ImageBuildDispatchClaim] = []
        for row in self.session.scalars(statement):
            if row.workspace_id is None or row.dispatch_payload is None:
                continue
            claim_id = uuid4().hex
            row.dispatch_claim_id = claim_id
            row.dispatch_after = now + timedelta(seconds=30)
            claims.append(
                ImageBuildDispatchClaim(
                    str(row.id),
                    str(row.workspace_id),
                    claim_id,
                    row.dispatch_payload,
                    row.created_at,
                    row.started_at,
                )
            )
        self.session.flush()
        return claims

    def stale_active(self, *, before: datetime, limit: int) -> list[tuple[str, str]]:
        rows = self.session.execute(
            select(ImageBuildTable.id, ImageBuildTable.workspace_id)
            .where(
                ImageBuildTable.status.in_(("pending", "running")),
                (
                    ImageBuildTable.dispatched_at.is_not(None)
                    | ImageBuildTable.dispatch_payload.is_(None)
                ),
                ImageBuildTable.updated_at < before,
            )
            .order_by(ImageBuildTable.updated_at)
            .limit(limit)
        )
        return [(str(row.id), str(row.workspace_id)) for row in rows]

    def complete(self, claim: ImageBuildDispatchClaim, *, now: datetime) -> None:
        self.session.execute(
            update(ImageBuildTable)
            .where(
                ImageBuildTable.id == claim.build_id,
                ImageBuildTable.dispatch_claim_id == claim.claim_id,
            )
            .values(dispatched_at=now, dispatch_claim_id=None)
        )

    def retry(self, claim: ImageBuildDispatchClaim, *, now: datetime) -> None:
        self.session.execute(
            update(ImageBuildTable)
            .where(
                ImageBuildTable.id == claim.build_id,
                ImageBuildTable.dispatch_claim_id == claim.claim_id,
            )
            .values(dispatch_after=now + timedelta(seconds=1), dispatch_claim_id=None)
        )
