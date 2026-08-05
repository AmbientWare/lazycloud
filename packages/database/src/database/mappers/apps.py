from __future__ import annotations

from shared.app_lifecycle import (
    AppDeploymentIntentTarget,
    AppLifecycleState,
    AppLifecycleTarget,
)
from shared.timestamps import to_utc, to_utc_or_none

from database.records.apps import (
    AppContainerShutdownIntentRecord,
    AppDeploymentIntentRecord,
    AppRecord,
)
from database.tables.apps import (
    AppContainerShutdownIntentTable,
    AppDeploymentIntentTable,
    AppTable,
)


def app_record_from_table(row: AppTable) -> AppRecord:
    return AppRecord(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        stub_id=str(row.stub_id) if row.stub_id is not None else None,
        name=row.name,
        version=row.version,
        public=row.public,
        lifecycle_state=AppLifecycleState(row.lifecycle_state),
        lifecycle_revision=row.lifecycle_revision,
        lifecycle_target=(
            AppLifecycleTarget(row.lifecycle_target) if row.lifecycle_target is not None else None
        ),
        lifecycle_operation_id=(
            str(row.lifecycle_operation_id) if row.lifecycle_operation_id is not None else None
        ),
        lifecycle_failure=row.lifecycle_failure,
        reconcile_claim_id=(
            str(row.reconcile_claim_id) if row.reconcile_claim_id is not None else None
        ),
        reconcile_claimed_at=to_utc_or_none(row.reconcile_claimed_at),
        reconcile_attempt_count=row.reconcile_attempt_count,
        lifecycle_event_id=(
            str(row.lifecycle_event_id) if row.lifecycle_event_id is not None else None
        ),
        lifecycle_event_created_at=to_utc_or_none(row.lifecycle_event_created_at),
        lifecycle_change_published_at=to_utc_or_none(row.lifecycle_change_published_at),
        metadata=dict(row.payload),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
        deleted_at=to_utc_or_none(row.deleted_at),
    )


def write_app_row(row: AppTable, app: AppRecord) -> None:
    row.workspace_id = app.workspace_id
    row.stub_id = app.stub_id
    row.name = app.name
    row.version = app.version
    row.public = app.public
    row.lifecycle_state = app.lifecycle_state.value
    row.lifecycle_revision = app.lifecycle_revision
    row.lifecycle_target = app.lifecycle_target.value if app.lifecycle_target is not None else None
    row.lifecycle_operation_id = app.lifecycle_operation_id
    row.lifecycle_failure = app.lifecycle_failure
    row.reconcile_claim_id = app.reconcile_claim_id
    row.reconcile_claimed_at = app.reconcile_claimed_at
    row.reconcile_attempt_count = app.reconcile_attempt_count
    row.lifecycle_event_id = app.lifecycle_event_id
    row.lifecycle_event_created_at = app.lifecycle_event_created_at
    row.lifecycle_change_published_at = app.lifecycle_change_published_at
    row.payload = dict(app.metadata)
    row.created_at = app.created_at
    row.updated_at = app.updated_at
    row.deleted_at = app.deleted_at


def app_deployment_intent_from_table(
    row: AppDeploymentIntentTable,
) -> AppDeploymentIntentRecord:
    return AppDeploymentIntentRecord(
        app_id=str(row.app_id),
        deployment_id=str(row.deployment_id),
        operation_revision=row.operation_revision,
        target=AppDeploymentIntentTarget(row.target),
        event_id=str(row.event_id) if row.event_id is not None else None,
        event_created_at=to_utc_or_none(row.event_created_at),
        workspace_change_published_at=to_utc_or_none(row.workspace_change_published_at),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


def app_container_shutdown_intent_from_table(
    row: AppContainerShutdownIntentTable,
) -> AppContainerShutdownIntentRecord:
    return AppContainerShutdownIntentRecord(
        app_id=str(row.app_id),
        container_id=str(row.container_id),
        worker_id=row.worker_id,
        operation_revision=row.operation_revision,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


__all__ = [
    "app_container_shutdown_intent_from_table",
    "app_deployment_intent_from_table",
    "app_record_from_table",
    "write_app_row",
]
