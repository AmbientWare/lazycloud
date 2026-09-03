from __future__ import annotations

from provider_aws import AwsProvider


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
