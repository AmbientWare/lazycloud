from __future__ import annotations

from dataclasses import dataclass

from images.building import (
    ImageRegistryCredentialKind,
    ImageRegistryCredentialPayload,
)
from provider_aws import AwsProvider, AwsProviderSettings
from shared.image_building.credentials import ImageCredentialEnvVar, parse_ecr_registry


@dataclass(slots=True)
class ProductionRegistryCredentialResolver:
    def __call__(
        self,
        payload: ImageRegistryCredentialPayload,
    ) -> ImageRegistryCredentialPayload:
        if payload.kind is not ImageRegistryCredentialKind.Aws:
            return payload
        registry = parse_ecr_registry(payload.registry)
        if registry is None:
            raise ValueError(f"AWS registry host is not an ECR registry: {payload.registry}")
        credentials = payload.credentials
        provider = AwsProvider(
            AwsProviderSettings(
                region=registry.region,
                access_key_id=credentials.get(ImageCredentialEnvVar.AwsAccessKeyId.value),
                secret_access_key=credentials.get(ImageCredentialEnvVar.AwsSecretAccessKey.value),
                session_token=credentials.get(ImageCredentialEnvVar.AwsSessionToken.value),
            )
        )
        authorization = provider.ecr_authorization(payload.registry)
        return ImageRegistryCredentialPayload(
            registry=authorization.registry,
            kind=ImageRegistryCredentialKind.Basic,
            credentials={
                "username": authorization.username,
                "password": authorization.password,
            },
        )


__all__ = ["ProductionRegistryCredentialResolver"]
