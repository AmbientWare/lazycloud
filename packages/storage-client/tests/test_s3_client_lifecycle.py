from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings, _PresignParams


class _ClosingClient:
    def __init__(self, *, close_error: RuntimeError | None = None) -> None:
        self.close_error = close_error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _ClosingPresignClient(_ClosingClient):
    def __init__(self, *, close_error: RuntimeError | None = None) -> None:
        super().__init__(close_error=close_error)
        self.requests: list[tuple[str, _PresignParams, int]] = []

    def generate_presigned_url(
        self,
        ClientMethod: str,
        *,
        Params: _PresignParams,
        ExpiresIn: int,
    ) -> str:
        self.requests.append((ClientMethod, Params, ExpiresIn))
        return "https://objects.example/presigned"


def _settings() -> S3ObjectStoreSettings:
    return S3ObjectStoreSettings(
        endpoint_url="https://objects.example",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",
    )


def test_close_is_idempotent_and_closes_primary_and_presign_clients() -> None:
    primary = _ClosingClient()
    presign = _ClosingPresignClient()
    client = S3ObjectStoreClient[_ClosingClient](
        settings=_settings(),
        client=primary,
        presign_client=presign,
    )

    client.close()
    client.close()

    assert primary.close_calls == 1
    assert presign.close_calls == 1


def test_close_attempts_every_client_and_aggregates_failures_in_order() -> None:
    primary_failure = RuntimeError("primary close failed")
    presign_failure = RuntimeError("presign close failed")
    primary = _ClosingClient(close_error=primary_failure)
    presign = _ClosingPresignClient(close_error=presign_failure)
    client = S3ObjectStoreClient[_ClosingClient](
        settings=_settings(),
        client=primary,
        presign_client=presign,
    )

    with pytest.raises(ExceptionGroup) as captured:
        client.close()

    assert captured.value.exceptions == (primary_failure, presign_failure)
    assert primary.close_calls == 1
    assert presign.close_calls == 1

    client.close()
    assert primary.close_calls == 1
    assert presign.close_calls == 1


def test_close_closes_a_shared_primary_and_presign_client_once() -> None:
    shared = _ClosingPresignClient()
    client = S3ObjectStoreClient[_ClosingPresignClient](
        settings=_settings(),
        client=shared,
        presign_client=shared,
    )

    client.close()

    assert shared.close_calls == 1


def test_presigned_upload_binds_exact_headers_and_temporary_session_lifetime() -> None:
    presign = _ClosingPresignClient()
    settings = _settings().model_copy(
        update={
            "session_token": "temporary-session-token",
            "credential_expires_at": datetime.now(UTC) + timedelta(minutes=5),
        }
    )
    client = S3ObjectStoreClient[_ClosingClient](
        settings=settings,
        client=_ClosingClient(),
        presign_client=presign,
    )

    upload = client.generate_presigned_put(
        "workspaces/workspace/images/image/archive.clip",
        content_length=1234,
        content_type="application/octet-stream",
        metadata={"artifact-sha256": "a" * 64},
        expires_seconds=900,
    )

    assert upload.url == "https://objects.example/presigned"
    assert upload.headers == {
        "content-length": "1234",
        "content-type": "application/octet-stream",
        "x-amz-meta-artifact-sha256": "a" * 64,
    }
    method, params, expires_seconds = presign.requests[0]
    assert method == "put_object"
    assert params == {
        "Bucket": settings.bucket,
        "Key": "workspaces/workspace/images/image/archive.clip",
        "ContentLength": 1234,
        "ContentType": "application/octet-stream",
        "Metadata": {"artifact-sha256": "a" * 64},
    }
    assert 1 <= expires_seconds < 300


def test_object_store_settings_repr_never_contains_credentials() -> None:
    settings = S3ObjectStoreSettings(
        access_key_id="temporary-access-key",
        secret_access_key="temporary-secret-key",
        session_token="temporary-session-token",
    )

    representation = repr(settings)

    assert "temporary-access-key" not in representation
    assert "temporary-secret-key" not in representation
    assert "temporary-session-token" not in representation


def test_native_s3_presign_uses_explicit_session_token() -> None:
    client = S3ObjectStoreClient.from_settings(
        S3ObjectStoreSettings(
            bucket="lazycloud-connected-object-store",
            endpoint_url="https://s3.us-east-1.amazonaws.com",
            presigned_endpoint_url="https://s3.us-east-1.amazonaws.com",
            region_name="us-east-1",
            access_key_id="temporary-access-key",
            secret_access_key="temporary-secret-key",
            session_token="temporary-session-token",
            force_path_style=False,
        )
    )
    try:
        query = parse_qs(urlsplit(client.generate_presigned_get_url("objects/item")).query)
    finally:
        client.close()

    assert query["X-Amz-Security-Token"] == ["temporary-session-token"]


def test_native_s3_presign_can_use_ambient_session_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret-key")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "ambient-session-token")
    client = S3ObjectStoreClient.from_settings(
        S3ObjectStoreSettings(
            bucket="lazycloud-connected-object-store",
            endpoint_url="https://s3.us-east-1.amazonaws.com",
            presigned_endpoint_url="https://s3.us-east-1.amazonaws.com",
            region_name="us-east-1",
            access_key_id="",
            secret_access_key="",
            force_path_style=False,
        )
    )
    try:
        query = parse_qs(urlsplit(client.generate_presigned_get_url("objects/item")).query)
    finally:
        client.close()

    assert query["X-Amz-Security-Token"] == ["ambient-session-token"]
