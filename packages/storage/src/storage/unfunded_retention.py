from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.cleanup import (
    OBJECT_CLEANUP_DELETE,
    CleanupRepository,
    object_location_lock_key,
)
from database.repositories.email_outbox import EmailOutboxRepository
from database.repositories.identity import UserRepository
from database.repositories.orchestration import ContainerRepository
from database.repositories.storage import ObjectRepository, VolumeRepository
from database.repositories.storage_retention import StorageRetentionRepository
from database.types import DatabaseSession
from shared.email import EmailMessage
from shared.errors import ConflictError, NotFoundError
from shared.timestamps import to_utc, utc_now

from storage.context import StorageContext
from storage.volume_deletion import VolumeDeletionService

LOGGER = logging.getLogger(__name__)
UNFUNDED_STORAGE_RETENTION = timedelta(days=30)


@dataclass(slots=True)
class UnfundedStorageRetentionService:
    context: StorageContext
    volume_deletion: VolumeDeletionService
    max_items_per_workspace: int = 100

    def reconcile(self, *, now: datetime | None = None) -> None:
        current = to_utc(now or utc_now())
        with self.context.database.session() as session:
            accounts = StorageRetentionRepository(session).account_ids()
        for user_id in accounts:
            try:
                self._reconcile_account(user_id, now=current)
            except Exception:
                LOGGER.exception("unfunded storage retention failed", extra={"user_id": user_id})

    def _reconcile_account(self, user_id: str, *, now: datetime) -> None:
        with self.context.database.session() as session:
            account = BillingAccountRepository(session).get_by_user(user_id, for_update=True)
            if account is None:
                return
            repository = StorageRetentionRepository(session)
            period = repository.active(user_id=user_id)
            credits = BillingCreditRepository(session)
            balance = credits.balance(user_id=user_id, at=now)
            funded = account.complimentary_since is not None or balance > 0
            if period is not None and funded:
                repository.close(period, at=now)
                EmailOutboxRepository(session).discard_if_unsent(
                    period.notification_message_id, now=now
                )
                period = None
            if funded:
                return
            workspace_ids = repository.workspace_ids(user_id=user_id)
            managed: list[str] = []
            for workspace_id in workspace_ids:
                workspace = repository.lock_workspace(workspace_id)
                if (
                    workspace is not None
                    and not (workspace.storage.access_key or workspace.storage.secret_key)
                    and (
                        ObjectRepository(session).workspace_has_objects(workspace_id)
                        or VolumeRepository(session).list(workspace_id=workspace_id)
                    )
                ):
                    managed.append(workspace_id)
            if not managed:
                return
            if period is None:
                period = repository.start(user_id=user_id, at=now)
                user = UserRepository(session).get(user_id)
                if user is None or not user.email:
                    raise ConflictError("storage retention requires the billing owner's email")
                deadline = (now + UNFUNDED_STORAGE_RETENTION).date().isoformat()
                body = (
                    "Your credit balance is empty. Your stored files will be retained "
                    f"at no charge until {deadline}. Add credit before that date "
                    "to keep your data. Otherwise, platform-managed files and volumes will be "
                    "permanently deleted. Customer-owned storage is unaffected."
                )
                period.notification_message_id = EmailOutboxRepository(session).enqueue(
                    EmailMessage(
                        to=user.email,
                        subject="Add credit within 30 days to keep your stored data",
                        text=body,
                        html=f"<p>{body}</p>",
                    ),
                    now=now,
                )
                return
            if now < to_utc(period.started_at) + UNFUNDED_STORAGE_RETENTION:
                return
            for workspace_id in managed:
                if ContainerRepository(session).live_counts_for_workspaces(
                    workspace_ids=(workspace_id,)
                ):
                    continue
                self._claim_workspace_data(session, workspace_id=workspace_id, now=now)

    def _claim_workspace_data(
        self, session: DatabaseSession, *, workspace_id: str, now: datetime
    ) -> None:
        objects = ObjectRepository(session).list(workspace_id=workspace_id)
        claims = CleanupRepository(session)
        repository = StorageRetentionRepository(session)
        claimed = 0
        for record in objects:
            if claimed >= self.max_items_per_workspace:
                break
            try:
                if not claims.try_lock_keys(
                    {
                        f"object:{record.id}",
                        object_location_lock_key(workspace_id, record.bucket, record.key),
                    }
                ) or not repository.lock_object(record.id, workspace_id=workspace_id):
                    continue
                claims.mark_object_claimed(
                    record.id, claimed_at=now, cleanup_kind=OBJECT_CLEANUP_DELETE
                )
                claimed += 1
            except (ConflictError, KeyError):
                continue
        volumes = VolumeRepository(session).list(workspace_id=workspace_id)
        claimed = 0
        for volume in volumes:
            if claimed >= self.max_items_per_workspace:
                break
            if volume.deletion_requested_at is not None or not repository.lock_volume(
                volume.id, workspace_id=workspace_id
            ):
                continue
            try:
                self.volume_deletion.request_in_session(
                    session, volume.name, workspace_id=workspace_id, now=now
                )
                claimed += 1
            except (ConflictError, NotFoundError):
                continue
