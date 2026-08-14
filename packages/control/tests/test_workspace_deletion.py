from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Never
from uuid import uuid4

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from api.server.worker_repository_service import WorkerRepositoryService
from compute.offers import ComputeOffer
from compute.state import RedisComputeStateRepository
from control.service import ControlPlaneService
from database.repositories.compute import AwsAccountConnectionRepository
from database.repositories.identity import (
    DeviceAuthorizationRepository,
    TokenRepository,
    WorkspaceAuditRepository,
    WorkspaceRepository,
)
from database.repositories.orchestration import AutoscalerStateRepository
from database.repositories.source_cache import SourceCacheCleanupRepository
from database.repositories.storage import ObjectRepository
from database.tables.billing_ledger import (
    BillingLedgerSegmentTable,
    ContainerBillingShapeTable,
)
from database.tables.billing_outbox import BillingMeterOutboxTable
from database.tables.execution import EventTable
from database.tables.identity import SecretTable
from database.tables.observability import UsageRecordTable
from fastapi.testclient import TestClient
from identity.auth import AuthError, AuthService
from identity.device_auth import DeviceAuthorizationService
from identity.users import UserService
from identity.workspaces import WorkspaceDeletionIdentityService
from pydantic import JsonValue, TypeAdapter
from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.autoscaler_state import (
    AutoscalerStateRecord,
    AutoscalerTargetKind,
    autoscaler_state_name,
)
from shared.aws_connections import AwsAccountConnection, AwsAccountConnectionPhase
from shared.billing_quotes import BilledDimension, LedgerBasis, LedgerComponent
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.errors import ConflictError, NotFoundError
from shared.http.workspaces import WorkspaceAuditAction, WorkspaceAuditTarget
from shared.identity import (
    AuthTokenRecord,
    TokenStatus,
    WorkspaceRecord,
    WorkspaceStatus,
)
from shared.source_cache_cleanup import SourceCacheCleanupStatus
from shared.timestamps import utc_now
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from storage.service import ObjectStorage
from storage_client.s3 import S3ObjectInfo
from tests.fakes import FakeObjectClient
from tests.provider_fixtures import configure_test_provider
from tests.service_fixtures import (
    administrator_credential,
    owned_workspace,
    workspace_owner_user_id,
)

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def test_workspace_deletion_tombstones_identity_and_invalidates_tokens(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owned_workspace(control, "default")
    workspace = owned_workspace(
        control,
        "tenant",
        labels={"team": "data"},
        metadata={"owner": "platform"},
    )
    auth = AuthService(isolated_services.context)
    _admin_token, audit_actor = administrator_credential(
        isolated_services, "workspace-delete-admin"
    )
    raw_token, _ = auth.create_token(
        "tenant-primary",
        workspace_id=workspace.id,
    )
    assert (
        AuthService(isolated_services.context).authenticate(raw_token).workspace_id == workspace.id
    )

    deleted = _delete_identity_workspace(
        isolated_services,
        workspace.id,
        audit_actor=audit_actor,
    )

    assert deleted.status is WorkspaceStatus.Deleted
    assert deleted.signing_key == ""
    assert deleted.storage.bucket is None
    assert deleted.labels == {}
    assert deleted.metadata == {}
    assert [item.name for item in control.list_workspaces()] == ["default"]
    assert control.list_workspaces(include_deleted=True)[1].status is WorkspaceStatus.Deleted
    with pytest.raises(NotFoundError, match="workspace not found"):
        control.get_workspace(workspace.id)
    with pytest.raises(AuthError, match="invalid token"):
        AuthService(isolated_services.context).authenticate(raw_token)
    with pytest.raises(NotFoundError, match="workspace not found"):
        AuthService(isolated_services.context).create_token(
            "replacement", workspace_id=workspace.id
        )
    # The tombstone released the name and kept its row: the ledger, usage and audit
    # history still point at the workspace that spent the money.
    replacement = owned_workspace(control, "tenant")
    assert replacement.id != workspace.id
    assert replacement.status is WorkspaceStatus.Active
    with isolated_services.context.database.session() as session:
        assert WorkspaceRepository(session).by_name("tenant") == replacement
    with isolated_services.context.database.session() as session:
        audit_records = (
            WorkspaceAuditRepository(session)
            .page(
                workspace_id=workspace.id,
                limit=10,
            )
            .records
        )
    assert len(audit_records) == 1
    audit = audit_records[0]
    assert audit.action is WorkspaceAuditAction.WorkspaceDeleted
    assert audit.actor_token_id == audit_actor.id
    assert audit.actor_name == audit_actor.name
    assert audit.target_type is WorkspaceAuditTarget.Workspace
    assert audit.target_id == workspace.id
    assert audit.target_name == workspace.name
    assert audit.previous_value == WorkspaceStatus.Active.value
    assert audit.new_value == WorkspaceStatus.Deleted.value

    repeated = _delete_identity_workspace(
        isolated_services,
        workspace.id,
        audit_actor=audit_actor,
    )
    assert repeated.status is WorkspaceStatus.Deleted
    with isolated_services.context.database.session() as session:
        repeated_audit_records = (
            WorkspaceAuditRepository(session)
            .page(
                workspace_id=workspace.id,
                limit=10,
            )
            .records
        )
    assert repeated_audit_records == audit_records


def test_workspace_deleting_transition_atomically_revokes_workspace_credentials(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owned_workspace(control, "default")
    workspace = owned_workspace(control, "tenant")
    auth = AuthService(isolated_services.context)
    _admin_raw, actor = administrator_credential(isolated_services, "workspace-delete-admin")
    _workspace_raw, workspace_token = auth.create_token(
        "tenant-token",
        workspace_id=workspace.id,
    )
    approver = UserService(isolated_services.context).create(
        display_name="device-approver",
    )
    device = DeviceAuthorizationService(isolated_services.context)
    pending = device.start(client_name="cli")
    device.approve(pending.record.user_code, user_id=approver.id)
    identity = WorkspaceDeletionIdentityService(isolated_services.context)
    original_mark_deleting = WorkspaceRepository.mark_deleting

    def fail_mark_deleting(
        repository: WorkspaceRepository,
        target: WorkspaceRecord,
    ) -> Never:
        del repository, target
        raise RuntimeError("transition failed")

    monkeypatch.setattr(WorkspaceRepository, "mark_deleting", fail_mark_deleting)
    with (
        pytest.raises(RuntimeError, match="transition failed"),
        isolated_services.context.database.session() as session,
    ):
        target = identity.lock_and_validate_begin(
            session,
            workspace.id,
            actor_workspace_id=actor.workspace_id,
        )
        identity.mark_deleting(session, target)

    with isolated_services.context.database.session() as session:
        persisted = WorkspaceRepository(session).get(workspace.id)
        persisted_token = TokenRepository(session).get(
            workspace_token.id,
            workspace_id=workspace.id,
        )
        persisted_device = DeviceAuthorizationRepository(session).by_user_code(
            pending.record.user_code
        )
    assert persisted is not None and persisted.status is WorkspaceStatus.Active
    assert persisted_token is not None and persisted_token.status is TokenStatus.Active
    assert persisted_device is not None

    monkeypatch.setattr(WorkspaceRepository, "mark_deleting", original_mark_deleting)
    with isolated_services.context.database.session() as session:
        target = identity.lock_and_validate_begin(
            session,
            workspace.id,
            actor_workspace_id=actor.workspace_id,
        )
        identity.mark_deleting(session, target)

    with isolated_services.context.database.session() as session:
        persisted = WorkspaceRepository(session).get(workspace.id)
        persisted_token = TokenRepository(session).get(
            workspace_token.id,
            workspace_id=workspace.id,
        )
        persisted_device = DeviceAuthorizationRepository(session).by_user_code(
            pending.record.user_code
        )
    assert persisted is not None and persisted.status is WorkspaceStatus.Deleting
    assert persisted_token is not None and persisted_token.status is TokenStatus.Revoked
    # The pending CLI login belongs to the person who started it and reaches every
    # workspace they hold, so deleting one workspace must not cancel it.
    assert persisted_device is not None


def test_workspace_deletion_rolls_back_when_audit_append_fails(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owned_workspace(control, "default")
    workspace = owned_workspace(control, "tenant")
    auth = AuthService(isolated_services.context)
    _admin_token, audit_actor = administrator_credential(
        isolated_services, "workspace-delete-admin"
    )
    workspace_token, workspace_actor = auth.create_token(
        "tenant-primary",
        workspace_id=workspace.id,
    )
    isolated_services.secrets.set("retained-on-rollback", "secret", workspace=workspace.id)
    autoscaler_state = _autoscaler_state(workspace.id, "retained-on-rollback")
    with isolated_services.context.database.session() as session:
        AutoscalerStateRepository(session).upsert(autoscaler_state)

    def fail_audit_append(
        repository: WorkspaceAuditRepository,
        *,
        workspace_id: str,
        actor: AuthTokenRecord,
        target_name: str,
    ) -> Never:
        del (
            repository,
            workspace_id,
            actor,
            target_name,
        )
        raise RuntimeError("audit persistence failed")

    monkeypatch.setattr(
        WorkspaceAuditRepository,
        "append_workspace_deleted",
        fail_audit_append,
    )

    with pytest.raises(RuntimeError, match="audit persistence failed"):
        _delete_identity_workspace(
            isolated_services,
            workspace.id,
            audit_actor=audit_actor,
        )

    with isolated_services.context.database.session() as session:
        deleting = WorkspaceRepository(session).get(workspace.id)
        assert deleting is not None and deleting.status is WorkspaceStatus.Deleting
    with pytest.raises(AuthError, match="invalid token"):
        auth.authenticate(workspace_token)
    with isolated_services.context.database.session() as session:
        assert AutoscalerStateRepository(session).list(workspace_id=workspace.id) == [
            autoscaler_state
        ]
        assert (
            WorkspaceAuditRepository(session)
            .page(
                workspace_id=workspace.id,
                limit=10,
            )
            .records
            == ()
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(SecretTable)
                .where(SecretTable.workspace_id == workspace.id)
            )
            == 1
        )
    del workspace_actor


def test_workspace_deletion_purges_owned_resources_and_protects_identity_scopes(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    default = owned_workspace(control, "default")
    workspace = owned_workspace(control, "tenant")
    app = isolated_services.apps.create("predict", workspace=workspace.id)
    auth = AuthService(isolated_services.context)
    _admin_token, audit_actor = administrator_credential(
        isolated_services, "workspace-delete-admin"
    )

    with isolated_services.context.database.session() as session:
        repository = WorkspaceRepository(session)
        assert "apps" in repository.deletion_blockers(workspace.id)
        assert app.id in repository.owned_resource_ids(workspace.id)

    deleted = _delete_identity_workspace(
        isolated_services,
        workspace.id,
        audit_actor=audit_actor,
    )

    assert deleted.status is WorkspaceStatus.Deleted
    with isolated_services.context.database.session() as session:
        repository = WorkspaceRepository(session)
        assert repository.deletion_blockers(workspace.id) == ()
        assert repository.owned_resource_ids(workspace.id) == ()

    # A workspace-scoped credential is revoked by the deletion of its own workspace,
    # so deleting it with that credential would end the operation midway holding a
    # token that no longer exists. An account credential names no workspace and is
    # not exposed to that, which is why the actor here is a workspace one.
    protected = owned_workspace(control, "protected")
    _protected_token, protected_actor = auth.create_token(
        "protected-writer",
        workspace_id=protected.id,
    )
    with pytest.raises(ConflictError, match="current admin token"):
        _delete_identity_workspace(
            isolated_services,
            protected.id,
            audit_actor=protected_actor,
        )
    _admin_raw, admin_actor = administrator_credential(isolated_services, "protection-admin")
    with pytest.raises(ConflictError, match="default workspace"):
        _delete_identity_workspace(
            isolated_services,
            default.id,
            audit_actor=admin_actor,
        )


def test_workspace_deletion_keeps_the_priced_ledger_and_the_unsent_meter_events(
    isolated_services: ApiServices,
) -> None:
    """Deleting a workspace never destroys what its owner owed.

    The usage a charge derives from was already retained; the charge itself, the
    placement it priced against and the events still owed to the payment provider
    were not, so a deletion used to leave a customer's bill unprovable and the
    provider's meter permanently short.
    """
    control = ControlPlaneService(isolated_services.context)
    owned_workspace(control, "default")
    workspace = owned_workspace(control, "tenant")
    owner_user_id = workspace_owner_user_id(isolated_services.context, workspace.id)
    _admin_token, audit_actor = administrator_credential(
        isolated_services, "workspace-delete-admin"
    )
    usage_record_id = str(uuid4())
    container_id = str(uuid4())
    now = utc_now()
    with isolated_services.context.database.session() as session:
        session.add(
            UsageRecordTable(
                id=usage_record_id,
                workspace_id=workspace.id,
                resource_type="container",
                resource_id=container_id,
                metric="container_runtime_seconds",
                quantity=60.0,
                payload={},
            )
        )
        session.flush()
        session.add(
            ContainerBillingShapeTable(
                container_id=container_id,
                workspace_id=workspace.id,
                billing_owner="platform_fleet",
                gpu_type="",
                cpu_millicores=1000,
                memory_mib=2048,
                gpu_count=0,
            )
        )
        session.add(
            BillingLedgerSegmentTable(
                id=str(uuid4()),
                usage_record_id=usage_record_id,
                segment_index=0,
                workspace_id=workspace.id,
                owner_user_id=owner_user_id,
                dimension=BilledDimension.ComputeRuntime.value,
                component=LedgerComponent.ContainerTime.value,
                basis=LedgerBasis.Reserved.value,
                subject_type="container",
                subject_id=container_id,
                span_started_at=now,
                span_ended_at=now + timedelta(minutes=1),
                segment_started_at=now,
                segment_ended_at=now + timedelta(minutes=1),
                duration_ms=60_000,
                quantity=Decimal("60"),
                pricing_version="test-pricing",
                rate_nanos_per_unit=Decimal("1000"),
                quote_effective_at=now,
                cost_nanos=60_000,
            )
        )
        session.add(
            BillingMeterOutboxTable(
                id=str(uuid4()),
                workspace_id=workspace.id,
                identifier=usage_record_id,
                provider_customer_id="cus_test",
                meter_event_name="compute_runtime",
                value_nanos=60_000,
                pricing_version="test-pricing",
                occurred_at=now,
                status="pending",
                next_attempt_at=now,
            )
        )

    with isolated_services.context.database.session() as session:
        blockers = WorkspaceRepository(session).deletion_blockers(workspace.id)
    assert "billing_ledger_segments" not in blockers
    assert "billing_meter_outbox" not in blockers
    assert "container_billing_shapes" not in blockers

    deleted = _delete_identity_workspace(
        isolated_services,
        workspace.id,
        audit_actor=audit_actor,
    )

    assert deleted.status is WorkspaceStatus.Deleted
    with isolated_services.context.database.session() as session:
        assert _rows_for_workspace(session, BillingLedgerSegmentTable, workspace.id) == 1
        assert _rows_for_workspace(session, BillingMeterOutboxTable, workspace.id) == 1
        assert _rows_for_workspace(session, ContainerBillingShapeTable, workspace.id) == 1
        assert _rows_for_workspace(session, UsageRecordTable, workspace.id) == 1
        assert (
            session.scalar(
                select(BillingMeterOutboxTable.status).where(
                    BillingMeterOutboxTable.workspace_id == workspace.id
                )
            )
            == "pending"
        )


def test_workspace_deletion_purges_autoscaler_state_and_fences_stale_reconciliation(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owned_workspace(control, "default")
    workspace = owned_workspace(control, "tenant")
    peer = owned_workspace(control, "peer")
    AuthService(isolated_services.context)
    _admin_token, audit_actor = administrator_credential(
        isolated_services, "workspace-delete-admin"
    )
    owned_state = _autoscaler_state(workspace.id, "owned-endpoint")
    peer_state = _autoscaler_state(peer.id, "peer-endpoint")
    with isolated_services.context.database.session() as session:
        states = AutoscalerStateRepository(session)
        states.upsert(owned_state)
        states.upsert(peer_state)
        assert "autoscaler_states" in WorkspaceRepository(session).deletion_blockers(workspace.id)

    deleted = _delete_identity_workspace(
        isolated_services,
        workspace.id,
        audit_actor=audit_actor,
    )

    assert deleted.status is WorkspaceStatus.Deleted
    with isolated_services.context.database.session() as session:
        states = AutoscalerStateRepository(session)
        assert states.list(workspace_id=workspace.id) == []
        assert states.list(workspace_id=peer.id) == [peer_state]
        with pytest.raises(NotFoundError, match="workspace not found"):
            states.upsert(owned_state.model_copy(update={"decision": "stale-reconcile"}))
        assert states.list(workspace_id=workspace.id) == []

    with isolated_services.context.database.session() as session:
        deletion_audits = WorkspaceAuditRepository(session).page(
            workspace_id=workspace.id,
            limit=10,
        )
        assert len(deletion_audits.records) == 1
        assert deletion_audits.records[0].action is WorkspaceAuditAction.WorkspaceDeleted


def test_autoscaler_state_write_requires_active_workspace(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = owned_workspace(control, "disabled-tenant")
    workspace.status = WorkspaceStatus.Disabled
    with isolated_services.context.database.session() as session:
        WorkspaceRepository(session).upsert(workspace)

    with (
        isolated_services.context.database.session() as session,
        pytest.raises(NotFoundError, match="workspace not found"),
    ):
        AutoscalerStateRepository(session).upsert(
            _autoscaler_state(workspace.id, "disabled-endpoint")
        )


def test_workspace_deletion_preserves_historical_events_after_resource_cleanup(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owned_workspace(control, "default")
    workspace = owned_workspace(control, "tenant")
    AuthService(isolated_services.context)
    _admin_token, audit_actor = administrator_credential(
        isolated_services, "workspace-delete-admin"
    )

    isolated_services.secrets.set("temporary", "value", workspace=workspace.id)
    with isolated_services.context.database.session() as session:
        repository = WorkspaceRepository(session)
        assert "workspace_secrets" in repository.deletion_blockers(workspace.id)
        retained_events = session.scalar(
            select(func.count())
            .select_from(EventTable)
            .where(EventTable.workspace_id == workspace.id)
        )
    assert retained_events is not None and retained_events > 0

    deleted = _delete_identity_workspace(
        isolated_services,
        workspace.id,
        audit_actor=audit_actor,
    )

    assert deleted.status is WorkspaceStatus.Deleted
    with isolated_services.context.database.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(EventTable)
                .where(EventTable.workspace_id == workspace.id)
            )
            == retained_events
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(SecretTable)
                .where(SecretTable.workspace_id == workspace.id)
            )
            == 0
        )


def test_workspace_deletion_api_requires_admin_and_returns_no_content(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owned_workspace(control, "default")
    workspace = owned_workspace(control, "tenant")
    auth = AuthService(isolated_services.context)
    admin_token, _ = administrator_credential(isolated_services, "admin")
    workspace_token, _ = auth.create_token("tenant", workspace_id=workspace.id)
    isolated_services.apps.create("predict", workspace=workspace.id)
    isolated_services.deployments.deploy(
        DeploymentSpec(name="active-function", kind=DeploymentKind.Function),
        workspace=workspace.id,
    )
    assert isolated_services.redis_client is not None
    compute_states = RedisComputeStateRepository(isolated_services.redis_client)
    hot_state_key = compute_states.keys.agent_route_revision(
        workspace.id,
        "orphaned-pool",
        "orphaned-machine",
    )
    unowned_state_key = isolated_services.redis_client.key("test", workspace.id, "state")
    isolated_services.redis_client.set(hot_state_key, "active")
    isolated_services.redis_client.set(unowned_state_key, "unowned")
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    denied = client.delete(
        f"/api/v1/workspaces/{workspace.id}",
        headers=_auth(workspace_token),
    )
    assert denied.status_code == 403

    response = client.delete(
        f"/api/v1/workspaces/{workspace.id}",
        headers=_auth(admin_token),
    )
    assert response.status_code == 204
    assert response.content == b""
    with isolated_services.context.database.session() as session:
        audit_records = (
            WorkspaceAuditRepository(session)
            .page(
                workspace_id=workspace.id,
                limit=10,
            )
            .records
        )
    assert len(audit_records) == 1
    deletion_audit = audit_records[0]
    assert deletion_audit.action is WorkspaceAuditAction.WorkspaceDeleted
    assert deletion_audit.actor_name == "admin"
    assert deletion_audit.target_id == workspace.id
    assert isolated_services.redis_client.get(hot_state_key) is None
    assert isolated_services.redis_client.get(unowned_state_key) == "unowned"
    with pytest.raises(AuthError, match="invalid token"):
        auth.authenticate(workspace_token)
    workspaces_response = client.get("/api/v1/workspaces", headers=_auth(admin_token))
    workspace_payload = _JSON_OBJECT_ADAPTER.validate_json(workspaces_response.content)
    workspaces = workspace_payload["workspaces"]
    assert isinstance(workspaces, list)
    assert [item["name"] for item in workspaces if isinstance(item, dict)] == ["default"]


def test_deleting_one_workspace_leaves_the_accounts_aws_connection_intact(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    """The connected account backs every workspace its owner holds, so it outlives one.

    Deleting a scratch workspace must not tear down the compute serving production;
    what deletion requires released is the capacity this workspace itself holds.
    """
    control = ControlPlaneService(isolated_services.context)
    default = control.get_workspace("default")
    # One account holding two workspaces is the case that matters: the connection is
    # theirs, so deleting one workspace must not take it from the other.
    owner_id = workspace_owner_user_id(isolated_services.context, default.id)
    workspace = control.set_workspace("tenant", owner_user_id=owner_id)
    admin_token, _actor = administrator_credential(isolated_services, "admin")
    now = utc_now()
    connection_id = str(uuid4())
    with isolated_services.context.database.session() as session:
        AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=connection_id,
                user_id=owner_id,
                account_id="123456789012",
                external_id="x" * 48,
                phase=AwsAccountConnectionPhase.AwaitingAuthorization,
                created_at=now,
                updated_at=now,
            )
        )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.delete(
        f"/api/v1/workspaces/{workspace.id}",
        headers=_auth(admin_token),
    )

    assert response.status_code == 204
    with isolated_services.context.database.session() as session:
        surviving = AwsAccountConnectionRepository(session).get_for_user(owner_id)
    assert surviving is not None and surviving.id == connection_id


def test_workspace_deletion_aborts_when_object_removal_is_not_confirmed(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    request: pytest.FixtureRequest,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owned_workspace(control, "default")
    workspace = owned_workspace(control, "tenant")
    auth = AuthService(isolated_services.context)
    admin_token, _ = administrator_credential(isolated_services, "admin")
    workspace_token, _ = auth.create_token("tenant", workspace_id=workspace.id)
    object_client = _StickyDeleteObjectClient()
    services = _services_with_object_storage(
        isolated_services,
        ObjectStorage(
            isolated_services.context,
            object_client=object_client,
        ),
        request,
    )
    record = services.object_storage.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/workspace-delete.zip",
        data=b"source",
    )
    client = client_stack.enter_context(TestClient(create_app(services)))

    response = client.delete(
        f"/api/v1/workspaces/{workspace.id}",
        headers=_auth(admin_token),
    )

    assert response.status_code == 503
    with services.context.database.session() as session:
        deleting = WorkspaceRepository(session).get(workspace.id)
        owned = ObjectRepository(session).get_owned(record.id, include_operations=True)
    assert deleting is not None and deleting.status is WorkspaceStatus.Deleting
    assert owned is not None and owned.record.cleanup_kind
    with pytest.raises(AuthError, match="invalid token"):
        auth.authenticate(workspace_token)
    physical_key = services.object_storage.physical_key_for_record(record)
    physical_bucket = services.object_storage.physical_bucket(record.bucket)
    assert object_client.exists(physical_key, bucket=physical_bucket)

    object_client.sticky = False
    retry = client.delete(
        f"/api/v1/workspaces/{workspace.id}",
        headers=_auth(admin_token),
    )
    repeated = client.delete(
        f"/api/v1/workspaces/{workspace.id}",
        headers=_auth(admin_token),
    )

    assert retry.status_code == 204
    assert repeated.status_code == 204
    with services.context.database.session() as session:
        deleted = WorkspaceRepository(session).get(workspace.id)
        audits = WorkspaceAuditRepository(session).page(workspace_id=workspace.id, limit=10)
    assert deleted is not None and deleted.status is WorkspaceStatus.Deleted
    assert len(audits.records) == 1
    assert not object_client.exists(physical_key, bucket=physical_bucket)


def test_workspace_deletion_keeps_durable_source_cleanup_when_wake_delivery_fails(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    owned_workspace(control, "default")
    workspace = owned_workspace(control, "tenant")
    admin_token, _actor = administrator_credential(isolated_services, "admin")
    source = isolated_services.object_storage.put_bytes_for_workspace(
        workspace_id=workspace.id,
        bucket=SOURCE_PACKAGE_BUCKET,
        key="sources/durable-cleanup.zip",
        data=b"source",
    )
    with isolated_services.context.database.session() as session:
        SourceCacheCleanupRepository(session).register_generation(
            str(uuid4()),
            worker_id="worker-1",
            storage_id="cache-volume-1",
            workspace_id=None,
            now=utc_now(),
        )

    def fail_wake(repository: WorkerRepositoryService, workspace_id: str) -> Never:
        del repository, workspace_id
        raise OSError("redis unavailable")

    monkeypatch.setattr(WorkerRepositoryService, "wake_source_cache_cleanup", fail_wake)
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.delete(
        f"/api/v1/workspaces/{workspace.id}",
        headers=_auth(admin_token),
    )

    assert response.status_code == 204
    with isolated_services.context.database.session() as session:
        deleted = WorkspaceRepository(session).get(workspace.id)
        targets = SourceCacheCleanupRepository(session).list_targets(workspace_id=workspace.id)
    assert deleted is not None and deleted.status is WorkspaceStatus.Deleted
    assert len(targets) == 1
    assert targets[0].source_object_id == source.id
    assert targets[0].status is SourceCacheCleanupStatus.Pending


def test_concurrent_upload_and_workspace_deletion_converges_without_orphan(
    isolated_services: ApiServices,
    client_stack: ExitStack,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    object_client = _BlockingPutObjectClient()
    services = _services_with_object_storage(
        isolated_services,
        ObjectStorage(
            isolated_services.context,
            object_client=object_client,
            default_bucket="physical-objects",
        ),
        request,
    )
    control = ControlPlaneService(services.context)
    owned_workspace(control, "default")
    workspace = owned_workspace(control, "tenant")
    admin_token, _actor = administrator_credential(isolated_services, "admin")
    source = tmp_path / "concurrent-upload.bin"
    source.write_bytes(b"upload admitted before workspace deletion")
    client = client_stack.enter_context(TestClient(create_app(services)))

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            upload = executor.submit(
                services.object_storage.put_file_for_workspace,
                workspace_id=workspace.id,
                bucket="default",
                key="uploads/concurrent.bin",
                source=source,
                overwrite=False,
            )
            assert object_client.uploaded.wait(timeout=10)

            first_delete = client.delete(
                f"/api/v1/workspaces/{workspace.id}",
                headers=_auth(admin_token),
            )

            assert first_delete.status_code == 409
            object_client.release.set()
            record = upload.result(timeout=10)
    finally:
        object_client.release.set()

    physical_key = services.object_storage.physical_key_for_record(record)
    assert object_client.exists(physical_key, bucket="physical-objects")

    retry = client.delete(
        f"/api/v1/workspaces/{workspace.id}",
        headers=_auth(admin_token),
    )
    repeated = client.delete(
        f"/api/v1/workspaces/{workspace.id}",
        headers=_auth(admin_token),
    )

    assert retry.status_code == 204
    assert repeated.status_code == 204
    assert not object_client.exists(physical_key, bucket="physical-objects")
    with services.context.database.session() as session:
        workspace_record = WorkspaceRepository(session).get(workspace.id)
        objects = ObjectRepository(session).list_for_workspace_deletion(workspace.id)
        audits = WorkspaceAuditRepository(session).page(workspace_id=workspace.id, limit=10)
    assert workspace_record is not None
    assert workspace_record.status is WorkspaceStatus.Deleted
    assert objects == []
    assert len(audits.records) == 1


def _services_with_object_storage(
    isolated_services: ApiServices,
    object_storage: ObjectStorage,
    request: pytest.FixtureRequest,
) -> ApiServices:
    services = ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        object_storage=object_storage,
        volume_filesystem=isolated_services.volume_filesystem,
        redis_client=isolated_services.redis_client,
        binary_redis_client=isolated_services.binary_redis_client,
        owns_redis_client=False,
        owns_binary_redis_client=False,
    )
    request.addfinalizer(services.close)
    return services


def _delete_identity_workspace(
    services: ApiServices,
    workspace_id: str,
    *,
    audit_actor: AuthTokenRecord,
) -> WorkspaceRecord:
    identity = WorkspaceDeletionIdentityService(services.context)
    target = identity.resolve(workspace_id)
    with services.context.database.session() as session:
        workspace = identity.lock_and_validate_begin(
            session,
            target.id,
            actor_workspace_id=audit_actor.workspace_id,
        )
        if workspace.status is not WorkspaceStatus.Deleted:
            identity.mark_deleting(session, workspace)
    services.auth.credentials_revoked()
    with services.context.database.session() as session:
        return identity.finalize(session, target.id, actor=audit_actor)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rows_for_workspace(
    session: Session,
    table: type[BillingLedgerSegmentTable]
    | type[BillingMeterOutboxTable]
    | type[ContainerBillingShapeTable]
    | type[UsageRecordTable],
    workspace_id: str,
) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(table).where(table.workspace_id == workspace_id)
        )
        or 0
    )


def _autoscaler_state(workspace_id: str, target_id: str) -> AutoscalerStateRecord:
    target_kind = AutoscalerTargetKind.Endpoint
    return AutoscalerStateRecord(
        name=autoscaler_state_name(target_kind, target_id),
        workspace_id=workspace_id,
        source="endpoint",
        target_kind=target_kind,
        target_id=target_id,
        decision="hold",
    )


class _StickyDeleteObjectClient(FakeObjectClient):
    sticky: bool = True

    def delete(self, key: str, *, bucket: str | None = None) -> None:
        if not self.sticky:
            super().delete(key, bucket=bucket)


@dataclass
class _BlockingPutObjectClient(FakeObjectClient):
    uploaded: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)

    def put_file(
        self,
        key: str,
        source: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> S3ObjectInfo:
        result = super().put_file(
            key,
            source,
            bucket=bucket,
            content_type=content_type,
            metadata=metadata,
        )
        self.uploaded.set()
        assert self.release.wait(timeout=10)
        return result


def _configure_workspace_provider(isolated_services: ApiServices, *, workspace: str):
    return configure_test_provider(
        isolated_services,
        "workspace-delete",
        [
            ComputeOffer(
                id="cpu-small",
                provider="workspace-delete",
                instance_type="cpu-small",
                region="local",
                cpu_millicores=1000,
                memory_mb=1024,
                hourly_cost_micros=1_000_000,
                available=1,
            )
        ],
        workspace=workspace,
    )
