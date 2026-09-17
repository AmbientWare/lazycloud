from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from database.mappers.images import image_build_from_table
from database.repositories.image_build_logs import ImageBuildLogRepository
from database.tables.capacity_recovery import CapacityRecoveryTable
from database.tables.compute import ComputeCapacityOperationTable, ComputeMachineEnrollmentTable
from database.tables.images import (
    ImageArchiveTable,
    ImageBuildAttemptTable,
    ImageBuildLogTable,
    ImageBuildTable,
)
from database.tables.orchestration import ContainerTable
from shared.errors import ConflictError, NotFoundError
from shared.image_building.records import BuildStatus, ImageBuildPhase, ImageBuildRecord
from sqlalchemy import String, exists, func, or_, select, true
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class ImageBuildProgress:
    id: str
    image_id: str
    python_version: str
    status: BuildStatus
    phase: ImageBuildPhase
    error: str | None
    attempt_number: int
    started_at: datetime | None
    container_id: str | None
    worker_id: str | None
    requested_at: datetime | None
    acquisition_status: str | None
    owns_capacity: bool
    last_sequence: int


@dataclass(slots=True)
class ImageBuildAttemptRepository:
    session: Session

    def progress(self, build_id: str, *, workspace_id: str) -> ImageBuildProgress:
        acquisition = (
            select(
                ComputeCapacityOperationTable.status, ComputeCapacityOperationTable.owns_capacity
            )
            .where(
                ComputeCapacityOperationTable.demand_container_id
                == ImageBuildTable.execution_container_id.cast(String)
            )
            .order_by(ComputeCapacityOperationTable.created_at.desc())
            .limit(1)
            .correlate(ImageBuildTable)
            .lateral()
        )
        last_sequence = (
            select(func.coalesce(func.max(ImageBuildLogTable.sequence), 0))
            .where(ImageBuildLogTable.build_id == ImageBuildTable.id)
            .scalar_subquery()
        )
        row = self.session.execute(
            select(
                ImageBuildTable.id,
                ImageBuildTable.image_id,
                ImageBuildTable.image_definition["python_version"]
                .as_string()
                .label("python_version"),
                ImageBuildTable.status,
                ImageBuildTable.phase,
                ImageBuildTable.error,
                ImageBuildTable.attempt_number,
                ImageBuildTable.started_at,
                ContainerTable.id.label("container_id"),
                ContainerTable.runtime_worker_id,
                ContainerTable.scheduling_requested_at,
                acquisition.c.status.label("acquisition_status"),
                acquisition.c.owns_capacity,
                last_sequence.label("last_sequence"),
            )
            .outerjoin(ContainerTable, ContainerTable.id == ImageBuildTable.execution_container_id)
            .outerjoin(acquisition, true())
            .where(ImageBuildTable.id == build_id, ImageBuildTable.workspace_id == workspace_id)
        ).one_or_none()
        if row is None:
            raise NotFoundError("image build not found")
        return ImageBuildProgress(
            id=row.id,
            image_id=row.image_id or "",
            python_version=row.python_version or "",
            status=BuildStatus(row.status),
            phase=ImageBuildPhase(row.phase),
            error=row.error,
            attempt_number=row.attempt_number,
            started_at=row.started_at,
            container_id=row.container_id,
            worker_id=row.runtime_worker_id,
            requested_at=row.scheduling_requested_at,
            acquisition_status=row.acquisition_status,
            owns_capacity=bool(row.owns_capacity),
            last_sequence=row.last_sequence,
        )

    def interrupted(
        self, build_id: str, *, workspace_id: str, now: datetime, for_update: bool = False
    ) -> ImageBuildRecord | None:
        statement = (
            select(ImageBuildTable)
            .join(ContainerTable, ContainerTable.id == ImageBuildTable.execution_container_id)
            .join(
                CapacityRecoveryTable,
                CapacityRecoveryTable.source_machine_id == ContainerTable.machine_id,
            )
            .where(
                ImageBuildTable.id == build_id,
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.status.in_(("pending", "running")),
                ContainerTable.termination_reason == "PREEMPTED",
                or_(
                    CapacityRecoveryTable.deadline <= now,
                    exists().where(
                        ComputeMachineEnrollmentTable.machine_id == ContainerTable.machine_id,
                        ComputeMachineEnrollmentTable.revoked_at.is_(None),
                        ComputeMachineEnrollmentTable.capacity_state.in_(
                            ("preempting", "cordoned")
                        ),
                    ),
                ),
            )
        )
        if for_update:
            statement = statement.with_for_update(of=ImageBuildTable)
        row = self.session.scalar(statement)
        return image_build_from_table(row) if row else None

    def begin(
        self,
        build_id: str,
        *,
        workspace_id: str,
        container_id: str,
        expected_container_id: str | None,
        now: datetime,
    ) -> ImageBuildRecord:
        row = self.session.scalar(
            select(ImageBuildTable)
            .where(ImageBuildTable.id == build_id, ImageBuildTable.workspace_id == workspace_id)
            .with_for_update()
        )
        if row is None:
            raise NotFoundError("image build not found")
        if (
            row.status not in {"pending", "running"}
            or row.execution_container_id != expected_container_id
            or row.attempt_number >= 2
            or (
                row.publication_claim_id
                and row.publication_claimed_at is not None
                and row.publication_claimed_at >= now - timedelta(seconds=30)
            )
        ):
            raise ConflictError("image build attempt ownership changed")
        if expected_container_id is not None:
            previous = self.session.get(ImageBuildAttemptTable, expected_container_id)
            if previous is None:
                raise ConflictError("image build attempt is missing")
            previous.retired_at = now
            previous.cleanup_after = now
        attempt = ImageBuildAttemptTable(
            container_id=container_id,
            build_id=build_id,
            number=row.attempt_number + 1,
            created_at=now,
            log_sequence_base=ImageBuildLogRepository(self.session).last_sequence(build_id),
        )
        self.session.add(attempt)
        row.execution_container_id = container_id
        row.attempt_number = attempt.number
        row.status = "pending"
        row.phase = "submitted"
        row.publication_claim_id = ""
        row.publication_claimed_at = None
        row.started_at = None
        row.dispatched_at = None
        row.dispatch_claim_id = None
        row.dispatch_after = now
        row.updated_at = now
        self.session.flush()
        return image_build_from_table(row)

    def require_active(self, build_id: str, *, workspace_id: str, container_id: str) -> int:
        base = self.session.scalar(
            select(ImageBuildAttemptTable.log_sequence_base)
            .join(ImageBuildTable, ImageBuildTable.id == ImageBuildAttemptTable.build_id)
            .where(
                ImageBuildTable.id == build_id,
                ImageBuildTable.workspace_id == workspace_id,
                ImageBuildTable.status.in_(("pending", "running")),
                ImageBuildTable.execution_container_id == container_id,
                ImageBuildTable.cleanup_claimed_at.is_(None),
                ImageBuildAttemptTable.container_id == container_id,
                ImageBuildAttemptTable.retired_at.is_(None),
            )
            .with_for_update(of=ImageBuildTable)
        )
        if base is None:
            raise ConflictError("image build execution attempt is no longer active")
        return base

    def build_for_container(
        self, container_id: str, *, workspace_id: str
    ) -> ImageBuildRecord | None:
        row = self.session.scalar(
            select(ImageBuildTable).where(
                ImageBuildTable.execution_container_id == container_id,
                ImageBuildTable.workspace_id == workspace_id,
            )
        )
        return image_build_from_table(row) if row is not None else None

    def cleanup_due(self, *, now: datetime, limit: int) -> list[tuple[str, str, str]]:
        rows = self.session.execute(
            select(
                ImageBuildAttemptTable.container_id,
                ImageBuildTable.id,
                ImageBuildTable.workspace_id,
            )
            .join(ImageBuildTable, ImageBuildTable.id == ImageBuildAttemptTable.build_id)
            .where(ImageBuildAttemptTable.cleanup_after <= now)
            .order_by(ImageBuildAttemptTable.cleanup_after)
            .limit(limit)
        )
        return [(row.container_id, row.id, row.workspace_id) for row in rows if row.workspace_id]

    def schedule_cleanup(self, container_id: str, *, after: datetime | None) -> None:
        attempt = self.session.get(ImageBuildAttemptTable, container_id, with_for_update=True)
        if attempt is not None:
            attempt.cleanup_after = after
            self.session.flush()

    def record_upload(
        self, container_id: str, *, bucket: str, object_key: str, expires_at: datetime
    ) -> None:
        attempt = self.session.get(ImageBuildAttemptTable, container_id, with_for_update=True)
        if attempt is None or attempt.retired_at is not None:
            raise ConflictError("image build upload attempt is no longer active")
        if attempt.upload_object_key is not None and attempt.upload_object_key != object_key:
            raise ConflictError("image build upload ownership cannot change")
        attempt.upload_bucket = bucket
        attempt.upload_object_key = object_key
        attempt.upload_expires_at = expires_at
        self.session.flush()

    def expired_uploads(self, *, now: datetime, limit: int) -> list[tuple[str, str, str, bool]]:
        rows = self.session.execute(
            select(
                ImageBuildAttemptTable.container_id,
                ImageBuildAttemptTable.upload_bucket,
                ImageBuildAttemptTable.upload_object_key,
                exists()
                .where(
                    ImageArchiveTable.bucket == ImageBuildAttemptTable.upload_bucket,
                    ImageArchiveTable.object_key == ImageBuildAttemptTable.upload_object_key,
                )
                .label("retained"),
            )
            .join(ImageBuildTable, ImageBuildTable.id == ImageBuildAttemptTable.build_id)
            .where(
                ImageBuildAttemptTable.upload_object_key.is_not(None),
                ImageBuildAttemptTable.upload_expires_at <= now,
                or_(
                    ImageBuildAttemptTable.retired_at.is_not(None),
                    ImageBuildTable.status.not_in(("pending", "running")),
                ),
            )
            .order_by(ImageBuildAttemptTable.upload_expires_at)
            .limit(limit)
        )
        return [
            (r.container_id, r.upload_bucket, r.upload_object_key, r.retained)
            for r in rows
            if r.upload_bucket and r.upload_object_key
        ]

    def release_upload(self, container_id: str) -> None:
        attempt = self.session.get(ImageBuildAttemptTable, container_id, with_for_update=True)
        if attempt is not None:
            attempt.upload_bucket = None
            attempt.upload_object_key = None
            attempt.upload_expires_at = None
            self.session.flush()
