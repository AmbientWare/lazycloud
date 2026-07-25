from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import TypeAlias

from shared.contracts import ContractModel
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


class EcrRegistryRef(ContractModel):
    account_id: str
    region: str
    host: str


AWS_REQUIRED_REGISTRY_CREDENTIAL_NAMES = (
    ImageCredentialEnvVar.AwsAccessKeyId.value,
    ImageCredentialEnvVar.AwsSecretAccessKey.value,
)
AWS_OPTIONAL_REGISTRY_CREDENTIAL_NAMES = (
    ImageCredentialEnvVar.AwsSessionToken.value,
    ImageCredentialEnvVar.AwsRegion.value,
)
GCP_REGISTRY_CREDENTIAL_NAMES = (
    ImageCredentialEnvVar.GcpAccessToken.value,
    ImageCredentialEnvVar.GoogleApplicationCredentials.value,
    ImageCredentialEnvVar.GcpProjectId.value,
)
AZURE_REGISTRY_CREDENTIAL_NAMES = (
    ImageCredentialEnvVar.AzureClientId.value,
    ImageCredentialEnvVar.AzureClientSecret.value,
    ImageCredentialEnvVar.AzureTenantId.value,
)
TOKEN_REGISTRY_CREDENTIAL_NAMES = (
    ImageCredentialEnvVar.NgcApiKey.value,
    ImageCredentialEnvVar.GithubToken.value,
    ImageCredentialEnvVar.DockerhubToken.value,
)
BASIC_REGISTRY_CREDENTIAL_NAME_PAIRS = (
    ("username", "password"),
    (ImageCredentialEnvVar.DockerhubUsername.value, ImageCredentialEnvVar.DockerhubPassword.value),
    (ImageCredentialEnvVar.DockerUsername.value, ImageCredentialEnvVar.DockerPassword.value),
    (ImageCredentialEnvVar.RegistryUsername.value, ImageCredentialEnvVar.RegistryPassword.value),
    ("USERNAME", "PASSWORD"),
    (ImageCredentialEnvVar.GithubUsername.value, ImageCredentialEnvVar.GithubToken.value),
)
KNOWN_REGISTRY_CREDENTIAL_NAMES = frozenset(
    {
        *AWS_REQUIRED_REGISTRY_CREDENTIAL_NAMES,
        *AWS_OPTIONAL_REGISTRY_CREDENTIAL_NAMES,
        *GCP_REGISTRY_CREDENTIAL_NAMES,
        *AZURE_REGISTRY_CREDENTIAL_NAMES,
        *TOKEN_REGISTRY_CREDENTIAL_NAMES,
        *[name for pair in BASIC_REGISTRY_CREDENTIAL_NAME_PAIRS for name in pair],
    }
)

_ECR_REGISTRY_PATTERN = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr(?:-fips)?\."
    r"(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$"
)


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


def image_secret_names(secrets: Iterable[str]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for value in secrets:
        name = value.strip().split("=", 1)[0].strip()
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


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


def parse_registry_credentials_from_env(values: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in values.items()
        if key in KNOWN_REGISTRY_CREDENTIAL_NAMES and value
    }


def normalize_registry_host(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return ""
    for prefix in ("docker://", "oci://"):
        if candidate.lower().startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    if "://" in candidate:
        candidate = candidate.split("://", 1)[1]
    candidate = candidate.removeprefix("//")
    authority = re.split(r"[/#?]", candidate, maxsplit=1)[0].strip().rstrip(".")
    if not authority or "@" in authority or any(char.isspace() for char in authority):
        return ""
    return authority.lower()


def registry_host_for_image(image_ref: str) -> str:
    candidate = image_ref.strip()
    if not candidate:
        return ""
    lowered = candidate.lower()
    for prefix in ("docker://", "oci://"):
        if lowered.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    else:
        if "://" in candidate:
            return normalize_registry_host(candidate)
    if "/" not in candidate:
        return "docker.io"
    first = candidate.split("/", 1)[0]
    if "." in first or ":" in first or first.lower() == "localhost":
        return normalize_registry_host(first)
    return "docker.io"


def registry_hosts_equal(left: str, right: str) -> bool:
    left_host = normalize_registry_host(left)
    right_host = normalize_registry_host(right)
    return bool(left_host) and left_host == right_host


def parse_ecr_registry(host: str) -> EcrRegistryRef | None:
    normalized = normalize_registry_host(host)
    if not normalized:
        return None
    hostname, separator, port = normalized.rpartition(":")
    if separator and port.isdigit():
        if port != "443":
            return None
        normalized = hostname
    match = _ECR_REGISTRY_PATTERN.fullmatch(normalized)
    if match is None:
        return None
    return EcrRegistryRef(
        account_id=match.group("account"),
        region=match.group("region"),
        host=normalized,
    )


__all__ = [
    "AWS_OPTIONAL_REGISTRY_CREDENTIAL_NAMES",
    "AWS_REQUIRED_REGISTRY_CREDENTIAL_NAMES",
    "AZURE_REGISTRY_CREDENTIAL_NAMES",
    "BASIC_REGISTRY_CREDENTIAL_NAME_PAIRS",
    "GCP_REGISTRY_CREDENTIAL_NAMES",
    "KNOWN_REGISTRY_CREDENTIAL_NAMES",
    "TOKEN_REGISTRY_CREDENTIAL_NAMES",
    "EcrRegistryRef",
    "ImageCredentialEnvVar",
    "ImageCredentialInput",
    "ImageCredentialLookupError",
    "credential_key_names",
    "image_secret_names",
    "normalize_registry_host",
    "parse_ecr_registry",
    "parse_registry_credentials_from_env",
    "registry_host_for_image",
    "registry_hosts_equal",
    "resolve_registry_credentials",
]
