from __future__ import annotations

import json
from base64 import b64decode, b64encode

import pytest
from images.building import (
    ImageRegistryCredentialKind,
    detect_registry_credential_kind,
    registry_auth_file_entry,
    registry_credentials_for_image,
)
from shared.image_building.credentials import (
    ImageCredentialEnvVar,
    parse_registry_credentials_from_env,
)
from tests.real_redis import RealRedisActors
from worker.repository_payloads import ImageBuildPrivateInputs, ImageBuildRegistryAuth
from worker_repository.image_build_credentials import (
    DEFAULT_IMAGE_BUILD_CREDENTIAL_TTL_SECONDS,
    ImageBuildCredentialLease,
    RedisImageBuildCredentialCache,
)


def test_registry_credentials_filter_and_classify_provider_credentials() -> None:
    env = {
        ImageCredentialEnvVar.AwsAccessKeyId.value: "access",
        ImageCredentialEnvVar.AwsSecretAccessKey.value: "secret",
        ImageCredentialEnvVar.AwsRegion.value: "us-east-1",
        "UNRELATED": "ignored",
    }
    parsed = parse_registry_credentials_from_env(env)
    payload = registry_credentials_for_image(
        "111111111111.dkr.ecr.us-east-1.amazonaws.com/platform/app:latest",
        parsed,
    )

    assert parsed == {
        ImageCredentialEnvVar.AwsAccessKeyId.value: "access",
        ImageCredentialEnvVar.AwsSecretAccessKey.value: "secret",
        ImageCredentialEnvVar.AwsRegion.value: "us-east-1",
    }
    assert payload.kind is ImageRegistryCredentialKind.Aws
    assert payload.registry == "111111111111.dkr.ecr.us-east-1.amazonaws.com"
    assert payload.credential_keys == [
        ImageCredentialEnvVar.AwsAccessKeyId.value,
        ImageCredentialEnvVar.AwsRegion.value,
        ImageCredentialEnvVar.AwsSecretAccessKey.value,
    ]
    assert (
        detect_registry_credential_kind(payload.registry, payload.credentials)
        is ImageRegistryCredentialKind.Aws
    )


def test_registry_provider_detection_rejects_lookalike_hosts() -> None:
    credentials = {
        ImageCredentialEnvVar.AwsAccessKeyId.value: "access",
        ImageCredentialEnvVar.AwsSecretAccessKey.value: "secret",
    }

    assert (
        detect_registry_credential_kind(
            "123456789012.dkr.ecr.us-east-1.amazonaws.com.evil.test",
            credentials,
        )
        is ImageRegistryCredentialKind.Unknown
    )
    assert (
        registry_credentials_for_image(
            "123456789012.dkr.ecr.us-east-1.amazonaws.com.evil.test/team/app:latest",
            credentials,
        ).kind
        is ImageRegistryCredentialKind.Public
    )
    assert (
        registry_credentials_for_image(
            "gcr.io.evil.test/team/app:latest",
            {ImageCredentialEnvVar.GcpAccessToken.value: "token"},
        ).kind
        is ImageRegistryCredentialKind.Public
    )


def test_cloud_registry_authfile_entries_use_supported_docker_credentials() -> None:
    azure = registry_credentials_for_image(
        "team.azurecr.io/app:latest",
        {
            ImageCredentialEnvVar.AzureClientId.value: "service-principal-id",
            ImageCredentialEnvVar.AzureClientSecret.value: "service-principal-secret",
        },
    )
    gcp_access_token = registry_credentials_for_image(
        "us-docker.pkg.dev/project/repository/app:latest",
        {ImageCredentialEnvVar.GcpAccessToken.value: "gcp-access-token"},
    )
    service_account = json.dumps(
        {
            "type": "service_account",
            "client_email": "builder@example.iam.gserviceaccount.com",
            "private_key": "private-key-material",
        }
    )
    gcp_service_account = registry_credentials_for_image(
        "gcr.io/project/app:latest",
        {ImageCredentialEnvVar.GoogleApplicationCredentials.value: service_account},
    )

    azure_entry = registry_auth_file_entry(azure)
    token_entry = registry_auth_file_entry(gcp_access_token)
    service_account_entry = registry_auth_file_entry(gcp_service_account)

    assert b64decode(azure_entry["auth"]).decode() == (
        "service-principal-id:service-principal-secret"
    )
    assert b64decode(token_entry["auth"]).decode() == "oauth2accesstoken:gcp-access-token"
    assert b64decode(service_account_entry["auth"]).decode() == (f"_json_key:{service_account}")


def test_ephemeral_image_build_credentials_are_bound_one_time_and_expiring(
    real_redis_actors: RealRedisActors,
) -> None:
    redis = real_redis_actors.client()
    cache = RedisImageBuildCredentialCache(redis)
    lease = ImageBuildCredentialLease(
        workspace_id="workspace-1",
        build_id="build-1",
        container_id="container-1",
        registry="registry.example.com",
        private_inputs=ImageBuildPrivateInputs(
            registry_auth=ImageBuildRegistryAuth(
                registry="registry.example.com",
                auth=b64encode(b"builder:private-secret").decode("ascii"),
            ),
            build_args={"PRIVATE_ARG": "private-build-arg"},
        ),
    )
    cache.put("cache-key", lease)
    with pytest.raises(RuntimeError, match="already exists"):
        cache.put("cache-key", lease)

    cache_key = redis.key("image-build-credentials", "cache-key")
    assert redis.ttl(cache_key) == DEFAULT_IMAGE_BUILD_CREDENTIAL_TTL_SECONDS
    with pytest.raises(PermissionError, match="binding"):
        cache.consume(
            "cache-key",
            workspace_id="workspace-2",
            build_id="build-1",
            container_id="container-1",
            registry="registry.example.com",
        )
    assert redis.exists(cache_key)

    consumed = cache.consume(
        "cache-key",
        workspace_id="workspace-1",
        build_id="build-1",
        container_id="container-1",
        registry="https://REGISTRY.EXAMPLE.COM/team/base",
    )
    replay = cache.consume(
        "cache-key",
        workspace_id="workspace-1",
        build_id="build-1",
        container_id="container-1",
        registry="registry.example.com",
    )

    assert consumed == ImageBuildPrivateInputs(
        registry_auth=ImageBuildRegistryAuth(
            registry="registry.example.com",
            auth=b64encode(b"builder:private-secret").decode("ascii"),
        ),
        build_args={"PRIVATE_ARG": "private-build-arg"},
    )
    assert replay is None

    cache.put("expired-key", lease)
    redis.delete(redis.key("image-build-credentials", "expired-key"))
    assert (
        cache.consume(
            "expired-key",
            workspace_id="workspace-1",
            build_id="build-1",
            container_id="container-1",
            registry="registry.example.com",
        )
        is None
    )


def test_image_build_credential_lease_rejects_mismatched_registry_auth() -> None:
    with pytest.raises(ValueError, match="does not match lease registry"):
        ImageBuildCredentialLease(
            workspace_id="workspace-1",
            build_id="build-1",
            container_id="container-1",
            registry="registry.example.com",
            private_inputs=ImageBuildPrivateInputs(
                registry_auth=ImageBuildRegistryAuth(
                    registry="other.example.com",
                    auth=b64encode(b"builder:private-secret").decode("ascii"),
                )
            ),
        )
