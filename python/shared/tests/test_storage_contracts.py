from __future__ import annotations

import pytest
from pydantic import ValidationError
from shared.app_identity import (
    IMAGE_BUILD_CONTEXT_BUCKET,
    SOURCE_PACKAGE_BUCKET,
    WORKSPACE_OBJECT_BUCKET,
)
from shared.http.objects import ObjectMetadata, PutObjectRequest
from shared.secrets import SecretRecord


def test_secret_record_masks_values_and_excludes_them_from_representations() -> None:
    secret = SecretRecord(name="PRIVATE_TOKEN", value="plaintext-must-not-appear")

    assert secret.masked() == "********"
    assert "plaintext-must-not-appear" not in repr(secret)
    assert "plaintext-must-not-appear" not in str(secret)
    assert SecretRecord(name="EMPTY", value="").masked() == ""


@pytest.mark.parametrize(
    "bucket",
    [WORKSPACE_OBJECT_BUCKET, SOURCE_PACKAGE_BUCKET, IMAGE_BUILD_CONTEXT_BUCKET],
)
def test_workspace_upload_contract_accepts_only_server_owned_bucket_purposes(
    bucket: str,
) -> None:
    request = PutObjectRequest(
        object_metadata=ObjectMetadata(name="source.zip", size=1),
        hash="a" * 64,
        bucket=bucket,
        metadata={"kind": "source"},
    )

    assert request.bucket == bucket

    with pytest.raises(ValidationError, match="bucket is not available"):
        PutObjectRequest(
            object_metadata=ObjectMetadata(name="source.zip", size=1),
            hash="a" * 64,
            bucket="platform-secrets",
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {"invalid key": "value"},
        {"kind": "source\r\ninjected: true"},
        {"kind": "source\x00binary"},
        {"kind": "x" * 2049},
    ],
)
def test_workspace_upload_contract_rejects_unsafe_metadata(
    metadata: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        PutObjectRequest(
            object_metadata=ObjectMetadata(name="source.zip", size=1),
            hash="a" * 64,
            metadata=metadata,
        )


@pytest.mark.parametrize("content_type", ["text/plain\r\ninjected: true", "text/plain\x00raw"])
def test_workspace_upload_contract_rejects_unsafe_content_type(content_type: str) -> None:
    with pytest.raises(ValidationError):
        PutObjectRequest(
            object_metadata=ObjectMetadata(name="source.zip", size=1),
            hash="a" * 64,
            content_type=content_type,
        )
