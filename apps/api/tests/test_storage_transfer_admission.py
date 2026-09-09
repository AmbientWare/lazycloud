from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
from uuid import uuid4

from api.fastapi_app import create_app
from api.server.services import ApiServices
from database.repositories.billing_credits import BillingCreditRepository
from database.repositories.billing_preferences import BillingPreferencesRepository
from database.repositories.execution import TaskRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.billing_credits import CreditGrant, CreditKind
from shared.bytes_transport import encode_bytes
from shared.http.artifacts import ArtifactSaveResponse
from shared.http.billing_preferences import BillingPreferences
from shared.identity import TokenKind
from shared.tasks import Task
from shared.timestamps import utc_now
from tests.domain_fixtures import unfunded_billing_account
from tests.service_fixtures import isolated_services

__all__ = ["isolated_services"]


def test_empty_credit_blocks_new_transfers_but_preserves_management_and_top_up_recovery(
    isolated_services: ApiServices, client_stack: ExitStack
) -> None:
    services = isolated_services
    now = utc_now()
    user_id, workspace_id = unfunded_billing_account(
        services.context, period_started_at=now, period_ended_at=now + timedelta(days=30)
    )
    task_id = str(uuid4())
    with services.context.database.session() as session:
        other_workspace_id = services.context.default_workspace_id(session)
        credits = BillingCreditRepository(session)
        lot_id = credits.issue(
            user_id=user_id,
            grant=CreditGrant("payment:transfer", CreditKind.Purchased, 10**9, now),
        )
        TaskRepository(session).upsert(Task(id=task_id, name="result", workspace_id=workspace_id))
    token, _ = AuthService(services.context).create_token(
        "transfer-owner", kind=TokenKind.Workspace, workspace_id=workspace_id
    )
    client = client_stack.enter_context(TestClient(create_app(services)))
    client.headers["Authorization"] = f"Bearer {token}"
    body = {
        "task_id": task_id,
        "filename": "result.txt",
        "content_type": "text/plain",
        "value_base64": encode_bytes(b"saved result"),
    }
    saved_response = client.post("/api/v1/artifacts/save", json=body)
    assert saved_response.status_code == 200, saved_response.text
    saved = ArtifactSaveResponse.model_validate(saved_response.json())
    reader, _ = AuthService(services.context).create_token(
        "transfer-reader", scopes=["read"], kind=TokenKind.Workspace, workspace_id=workspace_id
    )
    assert (
        client.post(
            "/api/v1/volumes/presigned-url",
            headers={"Authorization": f"Bearer {reader}"},
            json={
                "volume_name": "private",
                "volume_path": "upload.txt",
                "method": "upload-part",
                "params": {"upload_id": "existing-upload", "part_number": 1},
            },
        ).status_code
        == 403
    )
    params = {"id": saved.id, "task_id": task_id, "filename": "result.txt"}
    with services.context.database.session() as session:
        credits = BillingCreditRepository(session)
        credits.adjust(
            user_id=user_id,
            credit_lot_id=lot_id,
            source_id="refund:transfer",
            amount_nanos=-(10**9),
            effective_at=utc_now(),
        )
    assert client.get("/api/v1/artifacts/content", params=params).status_code == 402
    assert client.post("/api/v1/artifacts/save", json=body).status_code == 402
    assert client.post("/api/v1/artifacts/public-url", json=params).status_code == 402
    assert client.get("/api/v1/artifacts").status_code == 200
    assert (
        client.get(
            "/api/v1/artifacts/content", params={**params, "workspace": other_workspace_id}
        ).status_code
        == 403
    )
    with services.context.database.session() as session:
        BillingCreditRepository(session).issue(
            user_id=user_id,
            grant=CreditGrant("subscription:refill", CreditKind.Subscription, 10**9, utc_now()),
        )
    downloaded = client.get("/api/v1/artifacts/content", params=params)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"saved result"
    with services.context.database.session() as session:
        BillingPreferencesRepository(session).set(
            user_id, BillingPreferences(monthly_usage_limit_nanos=0)
        )
    assert client.get("/api/v1/artifacts/content", params=params).status_code == 402
    assert client.delete(f"/api/v1/artifacts/{saved.id}").status_code == 204
