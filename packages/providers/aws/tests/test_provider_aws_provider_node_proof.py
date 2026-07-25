from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import pytest
from provider_aws.provider_node_proof import (
    AwsEc2ProviderNodeIdentityProofProvider,
    AwsInstanceMetadataResponse,
    AwsProviderNodeProofError,
)


@dataclass(slots=True)
class _MetadataTransport:
    responses: list[AwsInstanceMetadataResponse]
    requests: list[tuple[str, str, dict[str, str], int]] = field(default_factory=list)

    def request(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> AwsInstanceMetadataResponse:
        assert timeout_seconds == 2.0
        self.requests.append((method, path, headers, max_response_bytes))
        return self.responses.pop(0)


def test_ec2_identity_proof_uses_imdsv2_and_regional_sts() -> None:
    metadata = _MetadataTransport(
        responses=[
            AwsInstanceMetadataResponse(status_code=200, body=b"imds-session"),
            AwsInstanceMetadataResponse(status_code=200, body=b"i-0123456789abcdef0"),
            AwsInstanceMetadataResponse(status_code=200, body=b"us-east-1"),
            AwsInstanceMetadataResponse(status_code=200, body=b"provider-node"),
            AwsInstanceMetadataResponse(
                status_code=200,
                body=json.dumps(
                    {
                        "Code": "Success",
                        "AccessKeyId": "A" * 20,
                        "SecretAccessKey": "s" * 40,
                        "Token": "t" * 64,
                        "Expiration": "2099-01-01T00:00:00Z",
                    }
                ).encode(),
            ),
        ]
    )

    proof = AwsEc2ProviderNodeIdentityProofProvider(transport=metadata).create()

    proof_url = proof.presigned_url.get_secret_value()
    parsed = urlparse(proof_url)
    query = parse_qs(parsed.query)
    assert proof.instance_id == "i-0123456789abcdef0"
    assert proof.region == "us-east-1"
    assert parsed.hostname == "sts.us-east-1.amazonaws.com"
    assert query["Action"] == ["GetCallerIdentity"]
    assert query["X-Amz-Expires"] == ["60"]
    assert query["X-Amz-Security-Token"] == ["t" * 64]
    assert metadata.requests[0] == (
        "PUT",
        "/latest/api/token",
        {"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        2048,
    )
    assert all(
        request_headers.get("X-aws-ec2-metadata-token") == "imds-session"
        for _, _, request_headers, _ in metadata.requests[1:]
    )


def test_ec2_identity_proof_rejects_region_mismatch_before_credentials() -> None:
    metadata = _MetadataTransport(
        responses=[
            AwsInstanceMetadataResponse(status_code=200, body=b"imds-session"),
            AwsInstanceMetadataResponse(status_code=200, body=b"i-0123456789abcdef0"),
            AwsInstanceMetadataResponse(status_code=200, body=b"us-west-2"),
        ]
    )

    with pytest.raises(AwsProviderNodeProofError, match="does not match"):
        AwsEc2ProviderNodeIdentityProofProvider(transport=metadata).create(
            expected_region="us-east-1"
        )

    assert len(metadata.requests) == 3


def test_ec2_identity_proof_requires_imdsv2_token() -> None:
    metadata = _MetadataTransport(
        responses=[AwsInstanceMetadataResponse(status_code=401, body=b"")]
    )

    with pytest.raises(AwsProviderNodeProofError, match="status 401"):
        AwsEc2ProviderNodeIdentityProofProvider(transport=metadata).create(
            expected_region="us-east-1"
        )

    assert len(metadata.requests) == 1
