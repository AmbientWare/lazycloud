from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from database.repositories.artifacts import ArtifactRepository
from database.repositories.billing_rates import PlatformRateRepository
from database.repositories.execution import TaskRepository
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.billing_outbox import BillingMeterOutboxTable
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.artifacts import ArtifactRetentionSource
from shared.bytes_transport import encode_bytes
from shared.http.artifacts import ArtifactListResponse, ArtifactSaveResponse
from shared.identity import TokenKind
from shared.tasks import Task
from shared.timestamps import to_utc, utc_now
from sqlalchemy import select
from storage.artifact_metering import meter_artifact
from tests.service_fixtures import isolated_services, owned_workspace

__all__ = ["isolated_services"]


def test_artifact_retention_and_access_survive_task_deletion_without_crossing_workspaces(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    services = isolated_services
    workspace = owned_workspace(services.control_plane_service, "artifact-owner")
    other = owned_workspace(services.control_plane_service, "artifact-neighbor")
    token, _ = AuthService(services.context).create_token(
        "artifact-owner", kind=TokenKind.Workspace, workspace_id=workspace.id
    )
    task_id = str(uuid4())
    with services.context.database.session() as session:
        TaskRepository(session).upsert(Task(id=task_id, name="produce", workspace_id=workspace.id))
    client = client_stack.enter_context(TestClient(create_app(services)))
    client.headers["Authorization"] = f"Bearer {token}"
    base = "/api/v1/artifacts"
    assert client.put(f"{base}/retention", json={"retention_seconds": 3600}).status_code == 200
    body = {
        "task_id": task_id,
        "filename": "résumé.txt",
        "content_type": "text/plain",
        "value_base64": encode_bytes(b"report"),
    }
    inherited_response = client.post(f"{base}/save", json=body)
    assert inherited_response.status_code == 200, inherited_response.text
    inherited = ArtifactSaveResponse.model_validate(inherited_response.json())
    kept = ArtifactSaveResponse.model_validate(
        client.post(f"{base}/save", json={**body, "retention_seconds": None}).json()
    )
    assert inherited.expires_at is not None
    assert inherited.retention_source is ArtifactRetentionSource.Workspace
    assert kept.expires_at is None
    assert kept.retention_source is ArtifactRetentionSource.Explicit
    assert client.post(f"{base}/save", json={**body, "retention_seconds": 0}).status_code == 422
    assert client.post(f"{base}/save", json={**body, "retention_seconds": True}).status_code == 422
    first = ArtifactListResponse.model_validate(client.get(base, params={"limit": 1}).json())
    second = ArtifactListResponse.model_validate(
        client.get(base, params={"limit": 1, "cursor": first.next}).json()
    )
    assert {first.data[0].id, second.data[0].id} == {inherited.id, kept.id}
    assert not second.next
    assert client.get(base, params={"workspace": other.id}).status_code == 403
    other_token, _ = AuthService(services.context).create_token(
        "neighbor", kind=TokenKind.Workspace, workspace_id=other.id
    )
    content_params = {"id": inherited.id, "task_id": task_id, "filename": "résumé.txt"}
    assert (
        client.get(
            f"{base}/content",
            params=content_params,
            headers={"Authorization": f"Bearer {other_token}"},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"{base}/{inherited.id}", headers={"Authorization": f"Bearer {other_token}"}
        ).status_code
        == 204
    )
    with services.context.database.session() as session:
        TaskRepository(session).records.delete(task_id, workspace_id=workspace.id)
    assert client.get(f"{base}/content", params=content_params).content == b"report"
    applied = client.post(
        f"{base}/retention/apply", json={"ids": [inherited.id, kept.id], "retention_seconds": 7200}
    )
    assert applied.status_code == 200, applied.text
    assert [row["id"] for row in applied.json()["data"]] == [inherited.id]
    assert client.delete(f"{base}/{inherited.id}").status_code == 204
    assert client.get(f"{base}/content", params=content_params).status_code == 404
    assert [
        row.id for row in ArtifactListResponse.model_validate(client.get(base).json()).data
    ] == [kept.id]


def test_short_lived_artifact_deletion_settles_storage_once(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = isolated_services
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
        task_id = str(uuid4())
        TaskRepository(session).upsert(Task(id=task_id, name="produce", workspace_id=workspace_id))
        PlatformRateRepository(session).publish(
            pricing_version="artifact-test",
            effective_at=utc_now() - timedelta(days=1),
            nanos_per_egress_byte=Decimal(0),
            nanos_per_volume_byte_second=Decimal(1),
        )
    saved = services.artifact_service.save(
        workspace_id=workspace_id,
        task_id=task_id,
        filename="result.txt",
        content=b"1234567890",
        retention_seconds=None,
    )
    with services.context.database.session() as session:
        record = ArtifactRepository(session).get(saved.id, workspace_id=workspace_id)
        assert record is not None and record.artifact_metered_at is not None
        start = to_utc(record.artifact_metered_at)
        meter_artifact(
            session,
            workspace_id=workspace_id,
            artifact_id=saved.id,
            now=start + timedelta(seconds=2),
        )
    monkeypatch.setattr("storage.service.utc_now", lambda: start + timedelta(seconds=3))
    services.artifact_service.delete(workspace_id=workspace_id, artifact_id=saved.id)
    services.artifact_service.delete(workspace_id=workspace_id, artifact_id=saved.id)
    with services.context.database.session() as session:
        costs = list(
            session.scalars(
                select(BillingLedgerSegmentTable).where(
                    BillingLedgerSegmentTable.subject_id == saved.id
                )
            )
        )
        assert sum(row.cost_nanos for row in costs) == 30
        assert {row.dimension for row in costs} == {"volume_storage"}
        outbox = list(session.scalars(select(BillingMeterOutboxTable)))
        assert outbox
        assert ArtifactRepository(session).get(saved.id, workspace_id=workspace_id) is None
