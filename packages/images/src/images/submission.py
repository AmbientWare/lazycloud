from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import uuid4

from database.repositories.image_build_attempts import ImageBuildAttemptRepository
from database.repositories.image_build_dispatch import ImageBuildDispatchRepository
from database.repositories.images import ImageBuildRepository
from shared.errors import InvalidInputError, PaymentRequiredError
from shared.image_building.authoring import ImageSpec
from shared.image_building.records import BuildStatus, ImageBuildPhase, ImageBuildRecord
from shared.timestamps import utc_now
from storage.service import ObjectByteClient

from database import DatabaseClient
from images.building import ImageBuildCredentialPlan, build_image_plan, plan_image_build_session
from images.execution import ImageBuildExecutionRequest

LOGGER = logging.getLogger(__name__)


class ImageBuildDispatchExecutor(Protocol):
    def prepare(self, request: ImageBuildExecutionRequest) -> str: ...

    def dispatch(self, build_id: str, workspace_id: str, payload: str) -> None: ...

    def abort(self, build_id: str, workspace_id: str, *, container_id: str) -> None: ...

    def prepare_retry(
        self, build_id: str, workspace_id: str, payload: str, *, container_id: str
    ) -> str: ...


@dataclass(slots=True)
class ImageBuildSubmissionService:
    database: DatabaseClient
    executor: ImageBuildDispatchExecutor
    archive_store: ObjectByteClient

    def submit(
        self,
        image: ImageSpec,
        *,
        workspace_id: str,
        tag: str | None = None,
        credential_plan: ImageBuildCredentialPlan | None = None,
        registry_credential_payload: str | None = None,
        build_args: dict[str, str] | None = None,
        request_id: str | None = None,
    ) -> ImageBuildRecord:
        plan = build_image_plan(image)
        build_id = str(uuid4())
        container_id = str(uuid4())
        lifecycle = plan_image_build_session(
            image,
            container_id=container_id,
            require_archive_publication=True,
            image_id=plan.image_id,
            build_id=build_id,
        )
        record = ImageBuildRecord(
            id=build_id,
            image=plan.spec,
            fingerprint=plan.cache_key,
            image_id=plan.image_id,
            cache_key=plan.cache_key,
            dockerfile=plan.dockerfile,
            context_digest=plan.context_digest,
            status=BuildStatus.Pending,
            phase=ImageBuildPhase.Submitted,
            tag=tag or f"local:{plan.cache_key[:12]}",
            cache_metadata={
                "build_container_required": "true",
                "scheduler_submit_status": "queued",
                "executor": "build-container",
            },
        )
        payload = self.executor.prepare(
            ImageBuildExecutionRequest(
                build_id=build_id,
                workspace_id=workspace_id,
                image_id=plan.image_id,
                tag=record.tag or "",
                build_dir=build_id,
                dockerfile_path=f"{build_id}/Dockerfile",
                manifest_path=f"{build_id}/manifest.json",
                plan=plan,
                session=lifecycle,
                credential_plan=credential_plan,
                registry_credential_payload=registry_credential_payload or "",
                build_args=build_args or {},
            )
        )
        with self.database.session() as session:
            repository = ImageBuildRepository(session)
            dispatch = ImageBuildDispatchRepository(session)
            if request_id is not None:
                dispatch.lock_request(request_id, workspace_id=workspace_id)
                prior_id = dispatch.request_build_id(request_id, workspace_id=workspace_id)
                prior = repository.get(prior_id, workspace_id=workspace_id) if prior_id else None
                if prior is not None:
                    return prior
            repository.lock_fingerprint(plan.cache_key, workspace_id=workspace_id)
            completed = repository.list_completed_by_fingerprint(
                plan.cache_key, workspace_id=workspace_id, limit=16
            )
            reusable = next(
                (
                    item
                    for item in completed
                    if item.cache_metadata.get("image_archive_format_version") == "2"
                    and item.cache_metadata.get("build_container_required") == "true"
                ),
                None,
            )
            if reusable is not None:
                if request_id is not None:
                    dispatch.bind_request(
                        request_id, workspace_id=workspace_id, build_id=reusable.id
                    )
                return reusable
            active = repository.get_active_by_fingerprint(plan.cache_key, workspace_id=workspace_id)
            if active is not None:
                if request_id is not None:
                    dispatch.bind_request(request_id, workspace_id=workspace_id, build_id=active.id)
                return active
            record = repository.upsert(record, workspace_id=workspace_id)
            record = ImageBuildAttemptRepository(session).begin(
                build_id,
                workspace_id=workspace_id,
                container_id=container_id,
                expected_container_id=None,
                now=utc_now(),
            )
            dispatch.enqueue(build_id, payload, now=utc_now())
            if request_id is not None:
                dispatch.bind_request(request_id, workspace_id=workspace_id, build_id=build_id)
        self.drain(build_id=build_id, limit=1)
        return record

    def drain(self, *, build_id: str | None = None, limit: int = 16) -> int:
        with self.database.session() as session:
            claims = ImageBuildDispatchRepository(session).claim_due(
                now=utc_now(), limit=limit, build_id=build_id
            )
        completed = 0
        for claim in claims:
            try:
                if claim.started_at is not None:
                    with self.database.session() as session:
                        ImageBuildDispatchRepository(session).complete(claim, now=utc_now())
                    completed += 1
                    continue
                if claim.created_at < utc_now() - timedelta(minutes=5):
                    self._fail(
                        claim.build_id,
                        claim.workspace_id,
                        "image build submission deadline exceeded",
                        claim_id=claim.claim_id,
                    )
                    continue
                self.executor.dispatch(claim.build_id, claim.workspace_id, claim.payload)
            except PaymentRequiredError as exc:
                if self._fail(
                    claim.build_id,
                    claim.workspace_id,
                    exc.message,
                    claim_id=claim.claim_id,
                ):
                    self.cleanup(build_id=claim.build_id, limit=1)
            except Exception as exc:
                LOGGER.warning(
                    "image build dispatch failed for %s (%s)", claim.build_id, type(exc).__name__
                )
                with self.database.session() as session:
                    ImageBuildDispatchRepository(session).retry(claim, now=utc_now())
            else:
                with self.database.session() as session:
                    ImageBuildDispatchRepository(session).complete(claim, now=utc_now())
                completed += 1
        return completed

    def recover(self, *, limit: int = 100) -> int:
        now = utc_now()
        with self.database.session() as session:
            candidates = ImageBuildDispatchRepository(session).stale_active(
                before=now - timedelta(seconds=120), limit=limit
            )
        failed = 0
        for build_id, workspace_id in candidates:
            with self.database.session() as session:
                record = ImageBuildRepository(session).get(build_id, workspace_id=workspace_id)
            if record is None:
                continue
            try:
                if self.retry_interrupted(build_id, workspace_id=workspace_id):
                    continue
                if record.started_at is None and record.created_at > now - timedelta(minutes=5):
                    continue
                if self._fail(build_id, workspace_id, "image build worker progress lease expired"):
                    failed += 1
            except Exception as exc:
                LOGGER.warning(
                    "image build recovery failed for %s (%s)", build_id, type(exc).__name__
                )
        self.cleanup(limit=limit)
        return failed

    def retry_interrupted(self, build_id: str, *, workspace_id: str) -> bool:
        now = utc_now()
        with self.database.session() as session:
            attempts = ImageBuildAttemptRepository(session)
            record = attempts.interrupted(build_id, workspace_id=workspace_id, now=now)
            payload = ImageBuildDispatchRepository(session).payload(
                build_id, workspace_id=workspace_id
            )
        if record is None:
            return False
        if record.attempt_number != 1:
            return self._fail_interrupted(
                record,
                workspace_id=workspace_id,
                reason="image build interrupted after its automatic retry",
            )
        if payload is None:
            return self._fail_interrupted(
                record,
                workspace_id=workspace_id,
                reason="interrupted image build inputs are unavailable",
            )
        if record.created_at < now - timedelta(minutes=5):
            return self._fail_interrupted(
                record,
                workspace_id=workspace_id,
                reason="interrupted image build exceeded its original submission deadline",
            )
        container_id = str(uuid4())
        try:
            payload = self.executor.prepare_retry(
                build_id, workspace_id, payload, container_id=container_id
            )
        except InvalidInputError as exc:
            return self._fail_interrupted(record, workspace_id=workspace_id, reason=exc.message)
        with self.database.session() as session:
            attempts = ImageBuildAttemptRepository(session)
            current = attempts.interrupted(
                build_id, workspace_id=workspace_id, now=now, for_update=True
            )
            if current is None or current.execution_container_id != record.execution_container_id:
                return False
            attempts.begin(
                build_id,
                workspace_id=workspace_id,
                container_id=container_id,
                expected_container_id=record.execution_container_id,
                now=now,
            )
            ImageBuildDispatchRepository(session).replace_payload(build_id, payload)
        return True

    def _fail_interrupted(
        self, expected: ImageBuildRecord, *, workspace_id: str, reason: str
    ) -> bool:
        with self.database.session() as session:
            repository = ImageBuildRepository(session)
            current = repository.lock_build(expected.id, workspace_id=workspace_id)
            if (
                current.execution_container_id != expected.execution_container_id
                or current.status not in {BuildStatus.Pending, BuildStatus.Running}
            ):
                return False
            current.status = BuildStatus.Failed
            current.phase = ImageBuildPhase.Failed
            current.error = reason
            current.finished_at = utc_now()
            repository.upsert(current, workspace_id=workspace_id)
            ImageBuildDispatchRepository(session).schedule_cleanup(current.id, after=utc_now())
        return True

    def _fail(
        self, build_id: str, workspace_id: str, reason: str, *, claim_id: str | None = None
    ) -> bool:
        now = utc_now()
        with self.database.session() as session:
            dispatch = ImageBuildDispatchRepository(session)
            record = dispatch.lock_failure_candidate(
                build_id,
                workspace_id=workspace_id,
                stale_before=now if claim_id else now - timedelta(seconds=120),
                pending_created_before=now if claim_id else now - timedelta(minutes=5),
                claim_id=claim_id,
            )
            if record is None:
                return False
            record.status = BuildStatus.Failed
            record.phase = ImageBuildPhase.Failed
            record.error = reason
            record.finished_at = now
            ImageBuildRepository(session).upsert(record, workspace_id=workspace_id)
            dispatch.schedule_cleanup(build_id, after=now)
        return True

    def cleanup(self, *, limit: int = 16, build_id: str | None = None) -> None:
        with self.database.session() as session:
            candidates = ImageBuildDispatchRepository(session).cleanup_due(
                now=utc_now(), limit=limit, build_id=build_id
            )
        for build_id, workspace_id in candidates:
            retry_at = None
            try:
                with self.database.session() as session:
                    record = ImageBuildRepository(session).get(build_id, workspace_id=workspace_id)
                if record is not None and record.execution_container_id is not None:
                    self.executor.abort(
                        build_id, workspace_id, container_id=record.execution_container_id
                    )
            except Exception as exc:
                retry_at = utc_now() + timedelta(seconds=5)
                LOGGER.warning(
                    "image build cleanup failed for %s (%s)", build_id, type(exc).__name__
                )
            with self.database.session() as session:
                ImageBuildDispatchRepository(session).schedule_cleanup(build_id, after=retry_at)
        with self.database.session() as session:
            retired = ImageBuildAttemptRepository(session).cleanup_due(now=utc_now(), limit=limit)
        for container_id, attempt_build_id, workspace_id in retired:
            retry_at = None
            try:
                self.executor.abort(attempt_build_id, workspace_id, container_id=container_id)
            except Exception as exc:
                retry_at = utc_now() + timedelta(seconds=5)
                LOGGER.warning(
                    "retired image build attempt cleanup failed for %s (%s)",
                    container_id,
                    type(exc).__name__,
                )
            with self.database.session() as session:
                ImageBuildAttemptRepository(session).schedule_cleanup(container_id, after=retry_at)
        with self.database.session() as session:
            uploads = ImageBuildAttemptRepository(session).expired_uploads(
                now=utc_now(), limit=limit
            )
        for container_id, bucket, key, retained in uploads:
            try:
                if not retained:
                    self.archive_store.delete(key, bucket=bucket)
                with self.database.session() as session:
                    ImageBuildAttemptRepository(session).release_upload(container_id)
            except Exception as exc:
                LOGGER.warning(
                    "image build upload cleanup failed for %s (%s)",
                    container_id,
                    type(exc).__name__,
                )

    def cancel(self, build_id: str, *, workspace_id: str, reason: str) -> ImageBuildRecord:
        with self.database.session() as session:
            repository = ImageBuildRepository(session)
            record = repository.lock_build(build_id, workspace_id=workspace_id)
            if record.status not in {BuildStatus.Pending, BuildStatus.Running}:
                return record
            record.status = BuildStatus.Cancelled
            record.phase = ImageBuildPhase.Failed
            record.error = reason
            record.finished_at = utc_now()
            record = repository.upsert(record, workspace_id=workspace_id)
            ImageBuildDispatchRepository(session).schedule_cleanup(build_id, after=utc_now())
        self.cleanup(build_id=build_id, limit=1)
        return record
