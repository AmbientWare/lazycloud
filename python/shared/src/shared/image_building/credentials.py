from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from typing import TypeAlias

from shared.enums import StringEnum


class ImageCredentialEnvVar(StringEnum):
    AwsAccessKeyId = "AWS_ACCESS_KEY_ID"
    AwsSecretAccessKey = "AWS_SECRET_ACCESS_KEY"
    AwsSessionToken = "AWS_SESSION_TOKEN"
    AwsRegion = "AWS_REGION"
    DockerUsername = "DOCKER_USERNAME"
    DockerPassword = "DOCKER_PASSWORD"
    DockerhubUsername = "DOCKERHUB_USERNAME"
    DockerhubPassword = "DOCKERHUB_PASSWORD"
    DockerhubToken = "DOCKERHUB_TOKEN"
    RegistryUsername = "REGISTRY_USERNAME"
    RegistryPassword = "REGISTRY_PASSWORD"
    GithubUsername = "GITHUB_USERNAME"
    GithubToken = "GITHUB_TOKEN"
    GoogleApplicationCredentials = "GOOGLE_APPLICATION_CREDENTIALS"
    GcpProjectId = "GCP_PROJECT_ID"
    GcpAccessToken = "GCP_ACCESS_TOKEN"
    NgcApiKey = "NGC_API_KEY"
    AzureClientId = "AZURE_CLIENT_ID"
    AzureClientSecret = "AZURE_CLIENT_SECRET"
    AzureTenantId = "AZURE_TENANT_ID"


ImageCredentialInput: TypeAlias = (
    Mapping[str, str] | Sequence[str | ImageCredentialEnvVar] | str | ImageCredentialEnvVar | None
)


class ImageCredentialLookupError(KeyError):
    """Raised when a requested registry credential environment variable is missing."""


def credential_key_names(credentials: ImageCredentialInput) -> list[str]:
    if credentials is None:
        return []
    if isinstance(credentials, ImageCredentialEnvVar):
        return [credentials.value]
    if isinstance(credentials, str):
        return [credentials]
    if isinstance(credentials, Mapping):
        return [str(key) for key in credentials]
    return [
        key.value if isinstance(key, ImageCredentialEnvVar) else str(key) for key in credentials
    ]


def dedupe_names(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def resolve_registry_credentials(
    credentials: ImageCredentialInput,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if env is None else env
    if credentials is None:
        return {}
    if isinstance(credentials, Mapping):
        return {str(key): value for key, value in credentials.items() if value}

    resolved: dict[str, str] = {}
    for key in credential_key_names(credentials):
        value = source.get(key)
        if not value:
            msg = f"missing registry credential environment variable: {key}"
            raise ImageCredentialLookupError(msg)
        resolved[key] = value
    return resolved


__all__ = [
    "ImageCredentialEnvVar",
    "ImageCredentialInput",
    "ImageCredentialLookupError",
    "credential_key_names",
    "dedupe_names",
    "resolve_registry_credentials",
]
