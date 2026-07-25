from __future__ import annotations

import base64

import pytest
from images.building.models import (
    ImageRegistryCredentialKind,
    ImageRegistryCredentialPayload,
)
from provider_aws import AwsProvider, EcrAuthorization
from provider_clients.registry_credentials import ProductionRegistryCredentialResolver
from shared.image_building.credentials import ImageCredentialEnvVar


class _EcrClient:
    requested_registry_ids: list[str]

    def __init__(self) -> None:
        self.requested_registry_ids = []

    def get_authorization_token(self, *, registryIds: list[str]) -> object:
        self.requested_registry_ids = registryIds
        token = base64.b64encode(b"AWS:short-lived-password").decode("ascii")
        return {
            "authorizationData": [
                {
                    "authorizationToken": token,
                    "proxyEndpoint": (
                        "https://123456789012.dkr.ecr.us-east-1.amazonaws.com/repository"
                    ),
                }
            ]
        }


def test_resolver_exchanges_source_aws_credentials_for_short_lived_ecr_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _EcrClient()
    provider_settings: list[tuple[str, str | None, str | None, str | None]] = []
    original_authorization = AwsProvider.ecr_authorization

    def resolve_with_fake_ecr(self: AwsProvider, registry_host: str) -> EcrAuthorization:
        provider_settings.append(
            (
                self.settings.region,
                self.settings.access_key_id,
                self.settings.secret_access_key,
                self.settings.session_token,
            )
        )
        return original_authorization(self, registry_host, ecr_client=client)

    monkeypatch.setattr(AwsProvider, "ecr_authorization", resolve_with_fake_ecr)
    payload = ImageRegistryCredentialPayload(
        registry="123456789012.dkr.ecr.us-east-1.amazonaws.com",
        kind=ImageRegistryCredentialKind.Aws,
        credentials={
            ImageCredentialEnvVar.AwsAccessKeyId.value: "source-access-key",
            ImageCredentialEnvVar.AwsSecretAccessKey.value: "source-secret-key",
            ImageCredentialEnvVar.AwsSessionToken.value: "source-session-token",
        },
    )

    resolved = ProductionRegistryCredentialResolver()(payload)

    assert client.requested_registry_ids == ["123456789012"]
    assert provider_settings == [
        ("us-east-1", "source-access-key", "source-secret-key", "source-session-token")
    ]
    assert resolved == ImageRegistryCredentialPayload(
        registry="123456789012.dkr.ecr.us-east-1.amazonaws.com",
        kind=ImageRegistryCredentialKind.Basic,
        credentials={"username": "AWS", "password": "short-lived-password"},
    )
    assert not set(payload.credentials).intersection(resolved.credentials)


def test_resolver_rejects_ecr_lookalike_host_before_provider_auth() -> None:
    payload = ImageRegistryCredentialPayload(
        registry="123456789012.dkr.ecr.us-east-1.amazonaws.com.evil.test",
        kind=ImageRegistryCredentialKind.Aws,
        credentials={},
    )

    with pytest.raises(ValueError, match="not an ECR registry"):
        ProductionRegistryCredentialResolver()(payload)
