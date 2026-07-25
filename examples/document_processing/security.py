"""HMAC-signed capability tokens for document job access."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from examples.document_processing.resources import JOB_TOKEN_SECRET_NAME
from examples.document_processing.storage import validate_document_identity

TOKEN_LIFETIME_SECONDS = 24 * 60 * 60


class InvalidJobToken(ValueError):
    """Raised when a job token is malformed, forged, or expired."""


@dataclass(frozen=True, slots=True)
class JobClaims:
    task_id: str
    document_id: str
    suffix: str
    expires_at: int


class _JobTokenPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=128)
    document_id: str
    suffix: str
    expires_at: int


def get_job_token_secret() -> bytes:
    value = os.environ.get(JOB_TOKEN_SECRET_NAME, "").encode("utf-8")
    if len(value) < 32:
        raise RuntimeError(f"{JOB_TOKEN_SECRET_NAME} must contain at least 32 bytes")
    return value


def issue_job_token(
    task_id: str,
    document_id: str,
    suffix: str,
    *,
    secret: bytes,
    now: int | None = None,
) -> str:
    if not task_id or len(task_id) > 128:
        raise ValueError("task id must contain between 1 and 128 characters")
    safe_id, safe_suffix = validate_document_identity(document_id, suffix)
    issued_at = int(time.time()) if now is None else now
    payload = json.dumps(
        {
            "document_id": safe_id,
            "expires_at": issued_at + TOKEN_LIFETIME_SECONDS,
            "suffix": safe_suffix,
            "task_id": task_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = _encode(payload)
    signature = _encode(hmac.digest(secret, encoded.encode("ascii"), hashlib.sha256))
    return f"{encoded}.{signature}"


def verify_job_token(token: str, *, secret: bytes, now: int | None = None) -> JobClaims:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _encode(hmac.digest(secret, encoded.encode("ascii"), hashlib.sha256))
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise InvalidJobToken("invalid job token")
        payload = _JobTokenPayload.model_validate_json(_decode(encoded))
        safe_id, safe_suffix = validate_document_identity(
            payload.document_id,
            payload.suffix,
        )
    except (
        binascii.Error,
        UnicodeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as exc:
        if isinstance(exc, InvalidJobToken):
            raise
        raise InvalidJobToken("invalid job token") from exc
    current_time = int(time.time()) if now is None else now
    if payload.expires_at <= current_time:
        raise InvalidJobToken("job token has expired")
    return JobClaims(
        task_id=payload.task_id,
        document_id=safe_id,
        suffix=safe_suffix,
        expires_at=payload.expires_at,
    )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode("utf-8")
