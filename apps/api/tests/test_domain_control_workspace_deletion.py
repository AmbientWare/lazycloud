from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Never
from uuid import uuid4

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from api.server.worker_repository_service import WorkerRepositoryService
from compute.state import RedisComputeStateRepository
from control.service import ControlPlaneService
from database.repositories.compute import AwsAccountConnectionRepository
from database.repositories.identity import (
    WorkspaceAuditRepository,
    WorkspaceRepository,
)
from database.repositories.source_cache import SourceCacheCleanupRepository
from database.repositories.storage import ObjectRepository
from fastapi.testclient import TestClient
from identity.auth import AuthError, AuthService
from pydantic import JsonValue, TypeAdapter
from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.aws_connections import AwsAccountConnection, AwsAccountConnectionPhase
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.http.workspaces import WorkspaceAuditAction
from shared.identity import (
    WorkspaceStatus,
)
from shared.source_cache_cleanup import SourceCacheCleanupStatus
from shared.timestamps import utc_now
from storage.service import ObjectStorage
from storage_client.s3 import S3ObjectInfo
from tests.fakes import FakeObjectClient
from tests.workspaces import administrator_credential, owned_workspace, workspace_owner_user_id

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def test_workspace_deletion_api_requires_admin_and_returns_no_content(
    isolated_services: ApiServices,
) -> None:
    with ExitStack() as client_stack:
        control = ControlPlaneService(isolated_services.context)
        owned_workspace(control, "default")
        workspace = owned_workspace(control, "tenant")
        auth = AuthService(isolated_services.context)
        admin_token, _ = administrator_credential(isolated_services.context, "admin")
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
) -> None:
    """The connected account backs every workspace its owner holds, so it outlives one.

    Deleting a scratch workspace must not tear down the compute serving production;
    what deletion requires released is the capacity this workspace itself holds.
    """
    with ExitStack() as client_stack:
        control = ControlPlaneService(isolated_services.context)
        default = control.get_workspace("default")
        # One account holding two workspaces is the case that matters: the connection is
        # theirs, so deleting one workspace must not take it from the other.
        owner_id = workspace_owner_user_id(isolated_services.context, default.id)
        workspace = control.set_workspace("tenant", owner_user_id=owner_id)
        admin_token, _actor = administrator_credential(isolated_services.context, "admin")
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
    request: pytest.FixtureRequest,
) -> None:
    with ExitStack() as client_stack:
        control = ControlPlaneService(isolated_services.context)
        owned_workspace(control, "default")
        workspace = owned_workspace(control, "tenant")
        auth = AuthService(isolated_services.context)
        admin_token, _ = administrator_credential(isolated_services.context, "admin")
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with ExitStack() as client_stack:
        control = ControlPlaneService(isolated_services.context)
        owned_workspace(control, "default")
        workspace = owned_workspace(control, "tenant")
        admin_token, _actor = administrator_credential(isolated_services.context, "admin")
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
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    with ExitStack() as client_stack:
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
        admin_token, _actor = administrator_credential(isolated_services.context, "admin")
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
        workspace_storage_issuer=isolated_services.workspace_storage_issuer,
        object_storage=object_storage,
        volume_filesystem=isolated_services.volume_filesystem,
        redis_client=isolated_services.redis_client,
        binary_redis_client=isolated_services.binary_redis_client,
        async_io=isolated_services.require_async_io(),
        owns_redis_client=False,
        owns_binary_redis_client=False,
    )
    request.addfinalizer(services.close)
    return services


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
