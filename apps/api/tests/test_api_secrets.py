from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.http.secrets import GetSecretResponse, SecretMaskedListResponse
from shared.identity import TokenKind


def test_secret_routes_mask_lists_and_reveal_only_explicit_detail(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    raw_token, _ = AuthService(isolated_services.context).create_token(
        "secret-admin",
        kind=TokenKind.Admin,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {"Authorization": f"Bearer {raw_token}"}

    created = client.post(
        "/api/v1/secrets",
        headers=headers,
        json={"name": "API_KEY", "value": "secret-value"},
    )
    assert created.status_code == 201

    listed = client.get("/api/v1/secrets", headers=headers)
    assert listed.status_code == 200
    [masked_secret] = SecretMaskedListResponse.model_validate_json(listed.content).secrets
    assert masked_secret.name == "API_KEY"
    assert masked_secret.value == "********"
    assert masked_secret.created_at is not None
    assert masked_secret.updated_at is not None

    revealed = client.get("/api/v1/secrets/API_KEY", headers=headers)
    assert revealed.status_code == 200
    revealed_secret = GetSecretResponse.model_validate_json(revealed.content).secret
    assert revealed_secret is not None
    assert revealed_secret.id == "API_KEY"
    assert revealed_secret.name == "API_KEY"
    assert revealed_secret.value == "secret-value"
    assert revealed_secret.created_at == masked_secret.created_at
    assert revealed_secret.updated_at == masked_secret.updated_at


def test_secret_mutations_return_exact_conflict_and_not_found_outcomes(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    raw_token, _ = AuthService(isolated_services.context).create_token(
        "secret-mutation-admin",
        kind=TokenKind.Admin,
    )
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))
    headers = {"Authorization": f"Bearer {raw_token}"}

    created = client.post(
        "/api/v1/secrets",
        headers=headers,
        json={"name": "ATOMIC_SECRET", "value": "first"},
    )
    conflict = client.post(
        "/api/v1/secrets",
        headers=headers,
        json={"name": "ATOMIC_SECRET", "value": "collision"},
    )
    missing_update = client.patch(
        "/api/v1/secrets/MISSING_SECRET",
        headers=headers,
        json={"value": "missing"},
    )
    missing_delete = client.delete("/api/v1/secrets/MISSING_SECRET", headers=headers)
    updated = client.patch(
        "/api/v1/secrets/ATOMIC_SECRET",
        headers=headers,
        json={"value": "second"},
    )
    deleted = client.delete("/api/v1/secrets/ATOMIC_SECRET", headers=headers)

    assert created.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "secret already exists: ATOMIC_SECRET"
    assert missing_update.status_code == 404
    assert missing_update.json()["detail"] == "secret not found: MISSING_SECRET"
    assert missing_delete.status_code == 404
    assert missing_delete.json()["detail"] == "secret not found: MISSING_SECRET"
    assert updated.status_code == 200
    assert deleted.status_code == 200
