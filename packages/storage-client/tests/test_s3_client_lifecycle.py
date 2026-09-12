from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings


def _settings() -> S3ObjectStoreSettings:
    return S3ObjectStoreSettings(
        endpoint_url="https://objects.example",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",
    )


def test_presigned_upload_binds_exact_headers_and_temporary_session_lifetime() -> None:
    settings = _settings().model_copy(
        update={
            "session_token": "temporary-session-token",
            "credential_expires_at": datetime.now(UTC) + timedelta(minutes=5),
        }
    )
    client = S3ObjectStoreClient.from_settings(settings)
    try:
        upload = client.generate_presigned_put(
            "workspaces/workspace/images/image/archive.clip",
            content_length=1234,
            content_type="application/octet-stream",
            metadata={"artifact-sha256": "a" * 64},
            expires_seconds=900,
        )
    finally:
        client.close()

    assert upload.headers == {
        "content-length": "1234",
        "content-type": "application/octet-stream",
        "x-amz-meta-artifact-sha256": "a" * 64,
    }
    query = parse_qs(urlsplit(upload.url).query)
    assert query["X-Amz-Security-Token"] == ["temporary-session-token"]
    assert set(query["X-Amz-SignedHeaders"][0].split(";")) == {
        "host",
        *upload.headers,
    }
    assert 1 <= int(query["X-Amz-Expires"][0]) < 300


def test_presigned_download_lifetime_respects_signature_limit() -> None:
    client = S3ObjectStoreClient.from_settings(_settings())
    try:
        long_url = client.generate_presigned_get_url("artifact.bin", expires_seconds=2592000)
        short_url = client.generate_presigned_get_url("artifact.bin", expires_seconds=60)
    finally:
        client.close()

    assert parse_qs(urlsplit(long_url).query)["X-Amz-Expires"] == ["604800"]
    assert parse_qs(urlsplit(short_url).query)["X-Amz-Expires"] == ["60"]


def test_object_store_settings_repr_never_contains_credentials() -> None:
    settings = S3ObjectStoreSettings(
        endpoint_url="https://objects.example",
        access_key_id="temporary-access-key",
        secret_access_key="temporary-secret-key",
        session_token="temporary-session-token",
    )

    representation = repr(settings)

    assert "temporary-access-key" not in representation
    assert "temporary-secret-key" not in representation
    assert "temporary-session-token" not in representation
