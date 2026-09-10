from __future__ import annotations

from fastapi.testclient import TestClient
from shared.http.secrets import GetSecretResponse, SecretMaskedListResponse


def test_secret_routes_mask_lists_and_reveal_only_explicit_detail(
    api_client: TestClient,
) -> None:

    created = api_client.post(
        "/api/v1/secrets",
        json={"name": "API_KEY", "value": "secret-value"},
    )
    assert created.status_code == 201

    listed = api_client.get("/api/v1/secrets")
    assert listed.status_code == 200
    [masked_secret] = SecretMaskedListResponse.model_validate_json(listed.content).secrets
    assert masked_secret.name == "API_KEY"
    assert masked_secret.value == "********"
    assert masked_secret.created_at is not None
    assert masked_secret.updated_at is not None

    revealed = api_client.get("/api/v1/secrets/API_KEY")
    assert revealed.status_code == 200
    revealed_secret = GetSecretResponse.model_validate_json(revealed.content).secret
    assert revealed_secret is not None
    assert revealed_secret.id == "API_KEY"
    assert revealed_secret.name == "API_KEY"
    assert revealed_secret.value == "secret-value"
    assert revealed_secret.created_at == masked_secret.created_at
    assert revealed_secret.updated_at == masked_secret.updated_at


def test_secret_mutations_return_exact_conflict_and_not_found_outcomes(
    api_client: TestClient,
) -> None:

    created = api_client.post(
        "/api/v1/secrets",
        json={"name": "ATOMIC_SECRET", "value": "first"},
    )
    conflict = api_client.post(
        "/api/v1/secrets",
        json={"name": "ATOMIC_SECRET", "value": "collision"},
    )
    missing_update = api_client.patch(
        "/api/v1/secrets/MISSING_SECRET",
        json={"value": "missing"},
    )
    missing_delete = api_client.delete("/api/v1/secrets/MISSING_SECRET")
    updated = api_client.patch(
        "/api/v1/secrets/ATOMIC_SECRET",
        json={"value": "second"},
    )
    deleted = api_client.delete("/api/v1/secrets/ATOMIC_SECRET")

    assert created.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "secret already exists: ATOMIC_SECRET"
    assert missing_update.status_code == 404
    assert missing_update.json()["detail"] == "secret not found: MISSING_SECRET"
    assert missing_delete.status_code == 404
    assert missing_delete.json()["detail"] == "secret not found: MISSING_SECRET"
    assert updated.status_code == 200
    assert deleted.status_code == 200
