from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping

from pydantic import JsonValue, TypeAdapter
from shared.image_building.credentials import (
    AWS_OPTIONAL_REGISTRY_CREDENTIAL_NAMES,
    AWS_REQUIRED_REGISTRY_CREDENTIAL_NAMES,
    BASIC_REGISTRY_CREDENTIAL_NAME_PAIRS,
    GCP_REGISTRY_CREDENTIAL_NAMES,
    TOKEN_REGISTRY_CREDENTIAL_NAMES,
    ImageCredentialEnvVar,
    ImageCredentialLookupError,
    normalize_registry_host,
    parse_ecr_registry,
    parse_registry_credentials_from_env,
    registry_host_for_image,
)

from images.building.models import (
    ImageBuildCredentialAction,
    ImageBuildCredentialPlan,
    ImageRegistryCredentialKind,
    ImageRegistryCredentialPayload,
)

_GCP_REGISTRY_SUFFIXES = ("gcr.io", "pkg.dev")
_AZURE_REGISTRY_SUFFIXES = ("azurecr.io",)
_NGC_REGISTRY_SUFFIXES = ("nvcr.io",)
_GHCR_REGISTRY_SUFFIXES = ("ghcr.io",)
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def detect_registry_credential_kind(
    registry: str,
    credentials: Mapping[str, str],
) -> ImageRegistryCredentialKind:
    if not credentials:
        return ImageRegistryCredentialKind.Public
    ecr_registry = parse_ecr_registry(registry)
    if ecr_registry is not None and all(
        credentials.get(key) for key in AWS_REQUIRED_REGISTRY_CREDENTIAL_NAMES
    ):
        return ImageRegistryCredentialKind.Aws
    if _registry_matches_suffix(registry, _GCP_REGISTRY_SUFFIXES) and any(
        credentials.get(key) for key in GCP_REGISTRY_CREDENTIAL_NAMES
    ):
        return ImageRegistryCredentialKind.Gcp
    if (
        _registry_matches_suffix(registry, _AZURE_REGISTRY_SUFFIXES)
        and credentials.get(ImageCredentialEnvVar.AzureClientId.value)
        and credentials.get(ImageCredentialEnvVar.AzureClientSecret.value)
    ):
        return ImageRegistryCredentialKind.Azure
    if any(credentials.get(key) for key in TOKEN_REGISTRY_CREDENTIAL_NAMES):
        return ImageRegistryCredentialKind.Token
    if _find_complete_basic_pair(credentials) is not None:
        return ImageRegistryCredentialKind.Basic

    if ecr_registry is not None:
        return ImageRegistryCredentialKind.Aws
    if _registry_matches_suffix(registry, _GCP_REGISTRY_SUFFIXES):
        return ImageRegistryCredentialKind.Gcp
    if _registry_matches_suffix(registry, _AZURE_REGISTRY_SUFFIXES):
        return ImageRegistryCredentialKind.Azure
    return ImageRegistryCredentialKind.Unknown


def registry_credentials_for_image(
    image_ref: str,
    credentials: Mapping[str, str],
) -> ImageRegistryCredentialPayload:
    registry = registry_host_for_image(image_ref)
    if not registry:
        msg = f"failed to parse registry from image reference: {image_ref}"
        raise ValueError(msg)

    filtered = parse_registry_credentials_from_env(credentials)
    if parse_ecr_registry(registry) is not None:
        payload = _required_credentials(filtered, AWS_REQUIRED_REGISTRY_CREDENTIAL_NAMES)
        payload.update(_present_credentials(filtered, AWS_OPTIONAL_REGISTRY_CREDENTIAL_NAMES))
        return ImageRegistryCredentialPayload(
            registry=registry,
            kind=ImageRegistryCredentialKind.Aws,
            credentials=payload,
        )
    if _registry_matches_suffix(registry, _GCP_REGISTRY_SUFFIXES):
        payload = _present_credentials(
            filtered,
            (
                ImageCredentialEnvVar.GcpAccessToken.value,
                ImageCredentialEnvVar.GoogleApplicationCredentials.value,
            ),
        )
        if not payload:
            raise ImageCredentialLookupError(
                "GCP_ACCESS_TOKEN or GOOGLE_APPLICATION_CREDENTIALS is required for a GCP registry"
            )
        payload.update(_present_credentials(filtered, (ImageCredentialEnvVar.GcpProjectId.value,)))
        return ImageRegistryCredentialPayload(
            registry=registry,
            kind=ImageRegistryCredentialKind.Gcp,
            credentials=payload,
        )
    if _registry_matches_suffix(registry, _AZURE_REGISTRY_SUFFIXES):
        payload = _required_credentials(
            filtered,
            (
                ImageCredentialEnvVar.AzureClientId.value,
                ImageCredentialEnvVar.AzureClientSecret.value,
            ),
        )
        payload.update(_present_credentials(filtered, (ImageCredentialEnvVar.AzureTenantId.value,)))
        return ImageRegistryCredentialPayload(
            registry=registry,
            kind=ImageRegistryCredentialKind.Azure,
            credentials=payload,
        )
    if _registry_matches_suffix(registry, _NGC_REGISTRY_SUFFIXES):
        return ImageRegistryCredentialPayload(
            registry=registry,
            kind=ImageRegistryCredentialKind.Token,
            credentials=_required_credentials(filtered, (ImageCredentialEnvVar.NgcApiKey.value,)),
        )

    if _registry_matches_suffix(registry, _GHCR_REGISTRY_SUFFIXES):
        github_pair = _optional_pair(
            filtered,
            ImageCredentialEnvVar.GithubUsername.value,
            ImageCredentialEnvVar.GithubToken.value,
        )
        if github_pair is not None:
            return ImageRegistryCredentialPayload(
                registry=registry,
                kind=ImageRegistryCredentialKind.Basic,
                credentials=github_pair,
            )
        token = _present_credentials(filtered, (ImageCredentialEnvVar.GithubToken.value,))
        if token:
            return ImageRegistryCredentialPayload(
                registry=registry,
                kind=ImageRegistryCredentialKind.Token,
                credentials=token,
            )
        return ImageRegistryCredentialPayload(registry=registry)

    token = _present_credentials(filtered, TOKEN_REGISTRY_CREDENTIAL_NAMES)
    if token:
        return ImageRegistryCredentialPayload(
            registry=registry,
            kind=ImageRegistryCredentialKind.Token,
            credentials=token,
        )

    basic = _find_complete_basic_pair(filtered)
    if basic is not None:
        return ImageRegistryCredentialPayload(
            registry=registry,
            kind=ImageRegistryCredentialKind.Basic,
            credentials=basic,
        )
    _validate_incomplete_basic_pairs(filtered)
    return ImageRegistryCredentialPayload(registry=registry)


def marshal_registry_credentials(payload: ImageRegistryCredentialPayload) -> str:
    return json.dumps(
        {
            "registry": payload.registry,
            "type": payload.kind.value,
            "credentials": dict(sorted(payload.credentials.items())),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def unmarshal_registry_credentials(value: str) -> ImageRegistryCredentialPayload:
    try:
        data = _JSON_OBJECT_ADAPTER.validate_json(value)
    except ValueError as exc:
        msg = "registry credential payload must be a JSON object"
        raise ValueError(msg) from exc
    credential_values = data.get("credentials", {})
    if not isinstance(credential_values, dict):
        msg = "registry credential payload credentials must be a JSON object"
        raise ValueError(msg)
    credentials: dict[str, str] = {}
    for key, item in credential_values.items():
        if item is None or item == "":
            continue
        if not isinstance(item, str):
            msg = "registry credential payload values must be strings"
            raise ValueError(msg)
        credentials[key] = item
    registry = data.get("registry", "")
    kind = data.get("type", ImageRegistryCredentialKind.Public.value)
    if not isinstance(registry, str) or not isinstance(kind, str):
        msg = "registry credential payload registry and type must be strings"
        raise ValueError(msg)
    return ImageRegistryCredentialPayload(
        registry=registry,
        kind=ImageRegistryCredentialKind(kind),
        credentials=credentials,
    )


def registry_auth_file_entry(payload: ImageRegistryCredentialPayload) -> dict[str, str]:
    if not payload.has_credentials:
        return {}
    if payload.kind is ImageRegistryCredentialKind.Basic:
        pair = _find_complete_basic_pair(payload.credentials)
        if pair is None:
            raise ImageCredentialLookupError("basic registry credentials require username/password")
        username, password = _username_password(pair)
        return {"auth": _basic_auth_token(username, password)}
    if payload.kind is ImageRegistryCredentialKind.Token:
        ngc_token = payload.credentials.get(ImageCredentialEnvVar.NgcApiKey.value, "")
        if ngc_token:
            return {"auth": _basic_auth_token("$oauthtoken", ngc_token)}
        token = next((value for value in payload.credentials.values() if value), "")
        if token:
            return {"identitytoken": token}
        raise ImageCredentialLookupError("registry token is empty")
    if payload.kind is ImageRegistryCredentialKind.Gcp:
        token = payload.credentials.get(ImageCredentialEnvVar.GcpAccessToken.value, "")
        if token:
            return {"auth": _basic_auth_token("oauth2accesstoken", token)}
        service_account = payload.credentials.get(
            ImageCredentialEnvVar.GoogleApplicationCredentials.value,
            "",
        )
        if service_account:
            _validate_google_service_account_json(service_account)
            return {"auth": _basic_auth_token("_json_key", service_account)}
        raise ImageCredentialLookupError(
            "GCP registry authentication requires an access token or service-account JSON"
        )
    if payload.kind is ImageRegistryCredentialKind.Azure:
        client_id = payload.credentials.get(ImageCredentialEnvVar.AzureClientId.value, "")
        client_secret = payload.credentials.get(
            ImageCredentialEnvVar.AzureClientSecret.value,
            "",
        )
        if client_id and client_secret:
            return {"auth": _basic_auth_token(client_id, client_secret)}
        raise ImageCredentialLookupError(
            "Azure registry service-principal credentials require client id and client secret"
        )
    raise ImageCredentialLookupError(
        f"{payload.kind.value} registry digest inspection requires a resolved registry token"
    )


def plan_image_build_registry_credentials(
    *,
    source_image: str,
    credentials: Mapping[str, str],
    resolved_payload: ImageRegistryCredentialPayload | None = None,
) -> ImageBuildCredentialPlan:
    if not source_image.strip():
        return ImageBuildCredentialPlan(reason="no source image registry reference")
    if resolved_payload is None and not credentials:
        return ImageBuildCredentialPlan(
            registry=registry_host_for_image(source_image),
            reason="no registry credentials were provided",
        )

    payload = resolved_payload or registry_credentials_for_image(source_image, credentials)
    if not payload.has_credentials:
        return ImageBuildCredentialPlan(
            registry=payload.registry,
            kind=payload.kind,
            reason="registry credentials resolve to public access",
        )

    return ImageBuildCredentialPlan(
        action=ImageBuildCredentialAction.UseSourcePullCredentials,
        registry=payload.registry,
        kind=payload.kind,
        credential_keys=payload.credential_keys,
        use_source_pull_credentials=True,
        reason="private source credentials are consumed only by the archive build",
    )


def _present_credentials(
    credentials: Mapping[str, str],
    keys: Iterable[str],
) -> dict[str, str]:
    return {key: credentials[key] for key in keys if credentials.get(key)}


def _registry_matches_suffix(registry: str, suffixes: Iterable[str]) -> bool:
    host = normalize_registry_host(registry)
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


def _validate_google_service_account_json(value: str) -> None:
    try:
        payload = _JSON_OBJECT_ADAPTER.validate_json(value)
    except ValueError as exc:
        raise ImageCredentialLookupError(
            "GOOGLE_APPLICATION_CREDENTIALS must contain service-account JSON"
        ) from exc
    required = ("client_email", "private_key")
    if payload.get("type") != "service_account" or not all(payload.get(key) for key in required):
        raise ImageCredentialLookupError(
            "GOOGLE_APPLICATION_CREDENTIALS is not valid service-account JSON"
        )


def _required_credentials(
    credentials: Mapping[str, str],
    keys: Iterable[str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in keys:
        value = credentials.get(key)
        if not value:
            msg = f"{key} not found or empty"
            raise ImageCredentialLookupError(msg)
        result[key] = value
    return result


def _optional_pair(
    credentials: Mapping[str, str],
    username_key: str,
    password_key: str,
) -> dict[str, str] | None:
    username_present = username_key in credentials
    password_present = password_key in credentials
    username = credentials.get(username_key, "")
    password = credentials.get(password_key, "")
    if not username_present and not password_present:
        return None
    if not username or not password:
        msg = f"both {username_key} and {password_key} must be provided together"
        raise ImageCredentialLookupError(msg)
    return {username_key: username, password_key: password}


def _find_complete_basic_pair(credentials: Mapping[str, str]) -> dict[str, str] | None:
    for username_key, password_key in BASIC_REGISTRY_CREDENTIAL_NAME_PAIRS:
        if username_key not in credentials and password_key not in credentials:
            continue
        return _optional_pair(credentials, username_key, password_key)
    return None


def _username_password(credentials: Mapping[str, str]) -> tuple[str, str]:
    for username_key, password_key in BASIC_REGISTRY_CREDENTIAL_NAME_PAIRS:
        username = credentials.get(username_key, "")
        password = credentials.get(password_key, "")
        if username and password:
            return username, password
    raise ImageCredentialLookupError("registry username/password pair is incomplete")


def _basic_auth_token(username: str, password: str) -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode("ascii")


def _validate_incomplete_basic_pairs(credentials: Mapping[str, str]) -> None:
    for username_key, password_key in BASIC_REGISTRY_CREDENTIAL_NAME_PAIRS:
        if username_key in credentials or password_key in credentials:
            _optional_pair(credentials, username_key, password_key)
