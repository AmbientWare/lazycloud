from __future__ import annotations

import base64
from datetime import UTC, datetime

from provider_aws import AwsProvider


def test_ecr_authorization_accepts_boto3_response_metadata() -> None:
    client = _AuthorizingEcrClient()

    authorization = AwsProvider().ecr_authorization(
        "123456789012.dkr.ecr.us-east-1.amazonaws.com",
        ecr_client=client,
    )

    assert client.registry_ids == ["123456789012"]
    assert authorization.registry == "123456789012.dkr.ecr.us-east-1.amazonaws.com"
    assert authorization.username == "AWS"
    assert authorization.password == "secret"
    assert authorization.expires_at == datetime(2026, 9, 3, 18, tzinfo=UTC)


def test_ecr_image_deletion_targets_the_exact_repository_digest() -> None:
    client = _RecordingEcrClient()
    digest = "sha256:" + "a" * 64

    AwsProvider().delete_ecr_image(
        "123456789012.dkr.ecr.us-east-1.amazonaws.com/lazycloud/workloads",
        digest,
        ecr_client=client,
    )

    assert client.calls == [
        {
            "registryId": "123456789012",
            "repositoryName": "lazycloud/workloads",
            "imageIds": [{"imageDigest": digest}],
        }
    ]


class _AuthorizingEcrClient:
    def __init__(self) -> None:
        self.registry_ids: list[str] = []

    def get_authorization_token(
        self, *, registryIds: list[str]
    ) -> dict[str, list[dict[str, str | datetime]] | dict[str, str | int]]:
        self.registry_ids.extend(registryIds)
        return {
            "authorizationData": [
                {
                    "authorizationToken": base64.b64encode(b"AWS:secret").decode(),
                    "expiresAt": datetime(2026, 9, 3, 18, tzinfo=UTC),
                    "proxyEndpoint": "https://123456789012.dkr.ecr.us-east-1.amazonaws.com",
                }
            ],
            "ResponseMetadata": {
                "HTTPStatusCode": 200,
                "RequestId": "request-id",
                "RetryAttempts": 0,
            },
        }


class _RecordingEcrClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def batch_delete_image(
        self,
        *,
        registryId: str,
        repositoryName: str,
        imageIds: list[dict[str, str]],
    ) -> object:
        self.calls.append(
            {
                "registryId": registryId,
                "repositoryName": repositoryName,
                "imageIds": imageIds,
            }
        )
        return {"imageIds": imageIds, "failures": []}
