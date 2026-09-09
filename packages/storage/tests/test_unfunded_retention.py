from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from api.server.services import ApiServices
from database.repositories import billing_credits
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.identity import WorkspaceMemberRepository, WorkspaceRepository
from database.repositories.storage import VolumeRepository
from database.repositories.storage_retention import StorageRetentionRepository
from database.tables.email_outbox import EmailOutboxTable
from database.tables.identity import UserTable
from shared.billing_credits import CreditGrant, CreditKind
from shared.identity import WorkspaceStorageConfig
from shared.timestamps import to_utc, utc_now
from sqlalchemy import func, select
from storage.unfunded_retention import UnfundedStorageRetentionService

from storage import unfunded_retention


def test_only_positive_balance_ends_retention_and_prevents_a_concurrent_claim(
    postgres_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = postgres_services
    volume = services.volumes.get_or_create("keep-on-topup", admit=None)
    current = utc_now() + timedelta(days=31)
    with services.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
        user_id = WorkspaceMemberRepository(session).owner_user_id(workspace_id)
        user = session.get(UserTable, user_id)
        assert user is not None
        user.email = "storage-retention@example.com"
        credits = BillingCreditRepository(session)
        lot_id = credits.issue(
            user_id=user_id,
            grant=CreditGrant("refunded-credit", CreditKind.Purchased, 10**9, current),
        )
        credits.adjust(
            user_id=user_id,
            credit_lot_id=lot_id,
            source_id="refund-debt",
            amount_nanos=-2 * 10**9,
            effective_at=current,
        )
    retention = UnfundedStorageRetentionService(services.context, services.volume_service.deletion)
    retention.reconcile(now=current)
    partial_topup_at = current + timedelta(days=10)
    with services.database.session() as session:
        credits = BillingCreditRepository(session)
        credits.issue(
            user_id=user_id,
            grant=CreditGrant("partial-topup", CreditKind.Purchased, 500_000_000, partial_topup_at),
        )
        assert credits.balance(user_id=user_id, at=partial_topup_at) == -500_000_000
    retention.reconcile(now=partial_topup_at)
    retention.reconcile(now=current + timedelta(days=29))
    with services.database.session() as session:
        period = StorageRetentionRepository(session).active(user_id=user_id)
        assert period is not None and to_utc(period.started_at) == current
        assert session.scalar(select(func.count()).select_from(EmailOutboxTable)) == 1
        assert VolumeRepository(session).get(volume.name, workspace_id=workspace_id) is not None

    sweep_at = current + timedelta(days=30)
    restored_at = sweep_at + timedelta(seconds=1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with services.database.session() as session:
            BillingAccountRepository(session).get_by_user(user_id, for_update=True)
            sweep = executor.submit(retention.reconcile, now=sweep_at)
            monkeypatch.setattr(unfunded_retention, "utc_now", lambda: restored_at)
            BillingCreditRepository(session).issue(
                user_id=user_id,
                grant=CreditGrant(
                    source_id="retention-topup",
                    kind=CreditKind.Purchased,
                    amount_nanos=1_000_000_000,
                    effective_at=restored_at,
                ),
            )
            assert StorageRetentionRepository(session).intervals(
                user_id=user_id, started_at=current, ended_at=current + timedelta(days=30)
            ) == ((current, current + timedelta(days=30)),)
        sweep.result(timeout=10)
    with services.database.session() as session:
        assert StorageRetentionRepository(session).active(user_id=user_id) is None
        assert StorageRetentionRepository(session).intervals(
            user_id=user_id, started_at=current, ended_at=current + timedelta(days=31)
        ) == ((current, restored_at),)
        kept = VolumeRepository(session).get(volume.name, workspace_id=workspace_id)
        assert kept is not None and kept.deletion_requested_at is None


def test_retention_restarts_after_observed_recovery_and_claims_only_managed_data(
    postgres_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = postgres_services
    volume = services.volumes.get_or_create("managed-expiry", admit=None)
    current = utc_now() + timedelta(days=31)
    with services.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
        user_id = WorkspaceMemberRepository(session).owner_user_id(workspace_id)
        user = session.get(UserTable, user_id)
        assert user is not None
        user.email = "storage-retention@example.com"
    external = services.control_plane_service.set_workspace(
        "customer-bucket",
        owner_user_id=user_id,
        storage=WorkspaceStorageConfig(
            backend="s3",
            bucket="customer-owned-bucket",
            config={"access_key": "test-only", "secret_key": "test-only"},
        ),
    )
    with services.database.session() as session:
        VolumeRepository(session).create("customer-data", workspace_id=external.id)
    retention = UnfundedStorageRetentionService(services.context, services.volume_service.deletion)
    retention.reconcile(now=current)
    restored_at = current + timedelta(days=20)
    with services.database.session() as session:
        credits = BillingCreditRepository(session)
        lot_id = credits.issue(
            user_id=user_id,
            grant=CreditGrant(
                source_id="brief-storage-topup",
                kind=CreditKind.Purchased,
                amount_nanos=1_000_000_000,
                effective_at=restored_at,
            ),
        )
    retention.reconcile(now=restored_at)
    with services.database.session() as session:
        assert StorageRetentionRepository(session).active(user_id=user_id) is None
        BillingCreditRepository(session).adjust(
            user_id=user_id,
            credit_lot_id=lot_id,
            source_id="topup-returned",
            amount_nanos=-1_000_000_000,
            effective_at=restored_at + timedelta(hours=1),
        )
    restarted_at = current + timedelta(days=31)
    monkeypatch.setattr(billing_credits, "utc_now", lambda: restarted_at)
    retention.reconcile(now=restarted_at)
    retention.reconcile(now=restarted_at + timedelta(days=29))
    with services.database.session() as session:
        period = StorageRetentionRepository(session).active(user_id=user_id)
        assert period is not None and to_utc(period.started_at) == restarted_at
        kept = VolumeRepository(session).get(volume.name, workspace_id=workspace_id)
        assert kept is not None and kept.deletion_requested_at is None
    with ThreadPoolExecutor(max_workers=1) as executor, services.database.session() as session:
        busy = VolumeRepository(session).lock(volume.name, workspace_id=workspace_id)
        executor.submit(retention.reconcile, now=restarted_at + timedelta(days=30)).result(
            timeout=10
        )
        assert busy.deletion_requested_at is None
    retention.reconcile(now=restarted_at + timedelta(days=30))
    with services.database.session() as session:
        claimed = VolumeRepository(session).get(volume.name, workspace_id=workspace_id)
        assert claimed is not None and claimed.deletion_requested_at is not None
        customer = VolumeRepository(session).get("customer-data", workspace_id=external.id)
        assert customer is not None and customer.deletion_requested_at is None
        assert WorkspaceRepository(session).get(workspace_id) is not None
