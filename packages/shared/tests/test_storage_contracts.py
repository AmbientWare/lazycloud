from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from shared.app_identity import (
    IMAGE_BUILD_CONTEXT_BUCKET,
    SOURCE_PACKAGE_BUCKET,
    WORKSPACE_OBJECT_BUCKET,
)
from shared.cache_records import CacheEntry
from shared.http.objects import ObjectMetadata, PutObjectRequest
from shared.objects import ObjectWriteCommand
from shared.queue_messages import QueueMessage
from shared.secrets import SecretRecord


def test_queue_message_round_trip_preserves_json_body_and_claim_state() -> None:
    now = datetime(2026, 7, 19, tzinfo=timezone.utc)
    message = QueueMessage(
        id="message-1",
        queue="inference",
        body={"args": [0, 1.5, None], "options": {"stream": False}},
        attempts=1,
        available_at=now,
        leased_until=now,
        expires_at=now,
        created_at=now,
    )

    assert QueueMessage.model_validate_json(message.model_dump_json()) == message
    with pytest.raises(ValidationError):
        QueueMessage.model_validate(
            {
                "id": "message-invalid",
                "queue": "inference",
                "body": {"created_at": now},
            }
        )


def test_secret_record_masks_values_and_excludes_them_from_representations() -> None:
    secret = SecretRecord(name="PRIVATE_TOKEN", value="plaintext-must-not-appear")

    assert secret.masked() == "********"
    assert "plaintext-must-not-appear" not in repr(secret)
    assert "plaintext-must-not-appear" not in str(secret)
    assert SecretRecord(name="EMPTY", value="").masked() == ""


def test_object_write_command_rejects_mutation_and_negative_storage_counts() -> None:
    command = ObjectWriteCommand(
        bucket="objects",
        key="result.bin",
        path="s3://objects/result.bin",
        size=1,
        sha256="c" * 64,
    )

    with pytest.raises(ValidationError):
        command.size = 2
    with pytest.raises(ValidationError):
        ObjectWriteCommand(
            bucket="objects",
            key="invalid.bin",
            path="s3://objects/invalid.bin",
            size=-1,
            sha256="d" * 64,
        )
    with pytest.raises(ValidationError):
        CacheEntry(key="invalid", path="/cache/invalid", size=1, sha256="e" * 64, hits=-1)
    with pytest.raises(ValidationError):
        CacheEntry.model_validate(
            {
                "key": "legacy-policy",
                "path": "/cache/legacy-policy",
                "size": 1,
                "sha256": "f" * 64,
                "policy": "read-only",
            }
        )


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
