"""Fake AWS STS identity evidence for provider-node enrollment dev loops.

Everything here satisfies the production identity-verification contract
structurally: the presigned URL passes the strict SigV4 query validation in
``provider_aws.provider_node_identity`` and the HTTP client returns a
GetCallerIdentity body whose assumed-role session matches the claimed instance.
Only the credentials are fake; the verifier, replay semantics, and parsing are
the real production owners.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count

from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

_STS_ROLE_PRINCIPAL_ID = "AROA0123456789ABCDEFG"

_PROOF_NONCE = count(1)


def fake_presigned_proof_url(region: str, instance_id: str) -> str:
    """Presign a dummy STS GetCallerIdentity URL exactly as a node agent would.

    Credentials are derived from the instance id plus a per-call nonce, so
    every proof — including several for one instance signed within the same
    second (phase reports, then enrollment) — hashes uniquely for the
    single-use replay guard.
    """
    request = AWSRequest(
        method="GET",
        url=(f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15"),
    )
    seed = hashlib.sha256(f"{instance_id}:{next(_PROOF_NONCE)}".encode()).hexdigest()
    SigV4QueryAuth(
        Credentials(
            access_key=f"ASIA{seed[:16].upper()}",
            secret_key=seed[:40],
            token=f"fake-session-token/{instance_id}",
        ),
        "sts",
        region,
        expires=30,
    ).add_auth(request)
    if request.url is None:
        raise RuntimeError("SigV4 signing did not produce a presigned URL")
    return request.url


@dataclass(slots=True)
class FakeStsIdentityResponse:
    """Structural ``ProviderNodeIdentityHttpResponse`` payload."""

    status_code: int
    body: bytes
    content_type: str
    content_length: int | None


@dataclass(slots=True)
class FakeStsHttpClient:
    """Fake regional STS answering presigned GetCallerIdentity requests.

    One client serves many instances: the driver points ``current_instance_id``
    at the enrolling instance before each call so the assumed-role session in
    the response matches the claimed provider instance.
    """

    account_id: str
    role_name: str
    current_instance_id: str = ""

    def execute_presigned_get(
        self,
        *,
        url: str,
        timeout_seconds: float,
        max_response_bytes: int,
        follow_redirects: bool,
    ) -> FakeStsIdentityResponse:
        del url, timeout_seconds, max_response_bytes, follow_redirects
        instance_id = self.current_instance_id
        if not instance_id:
            raise RuntimeError("fake STS client has no current instance id")
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
            "<GetCallerIdentityResult>"
            f"<Arn>arn:aws:sts::{self.account_id}:assumed-role/{self.role_name}/{instance_id}</Arn>"
            f"<UserId>{_STS_ROLE_PRINCIPAL_ID}:{instance_id}</UserId>"
            f"<Account>{self.account_id}</Account>"
            "</GetCallerIdentityResult>"
            "<ResponseMetadata><RequestId>01234567-89ab-cdef-0123-456789abcdef</RequestId>"
            "</ResponseMetadata></GetCallerIdentityResponse>"
        ).encode()
        return FakeStsIdentityResponse(
            status_code=200,
            body=body,
            content_type="application/xml",
            content_length=len(body),
        )


@dataclass(slots=True)
class FakeReplayGuard:
    """Single-use claim per proof hash with real replay-rejection semantics."""

    claimed: dict[str, datetime] = field(default_factory=dict)

    def claim_once(self, *, proof_sha256: str, expires_at: datetime) -> bool:
        if proof_sha256 in self.claimed:
            return False
        self.claimed[proof_sha256] = expires_at
        return True


__all__ = [
    "FakeReplayGuard",
    "FakeStsHttpClient",
    "FakeStsIdentityResponse",
    "fake_presigned_proof_url",
]
