from __future__ import annotations

import pytest
from shared.image_building.credentials import (
    ImageCredentialEnvVar,
    ImageCredentialLookupError,
    credential_key_names,
    image_secret_names,
    normalize_registry_host,
    parse_ecr_registry,
    registry_host_for_image,
    registry_hosts_equal,
    resolve_registry_credentials,
)


@pytest.mark.parametrize(
    ("image_ref", "expected"),
    [
        ("ubuntu:24.04", "docker.io"),
        ("library/ubuntu:24.04", "docker.io"),
        ("docker://ghcr.io/Acme/app:latest", "ghcr.io"),
        ("oci://localhost:5000/team/app", "localhost:5000"),
        ("registry.example.com:5443/team/app@sha256:abc", "registry.example.com:5443"),
        ("https://REGISTRY.EXAMPLE.COM/team/app", "registry.example.com"),
        ("", ""),
    ],
)
def test_registry_host_for_image_uses_docker_reference_rules(
    image_ref: str,
    expected: str,
) -> None:
    assert registry_host_for_image(image_ref) == expected


def test_registry_host_normalization_is_shared_by_host_comparison() -> None:
    assert normalize_registry_host(" HTTPS://Registry.Example.com./team/app?x=1 ") == (
        "registry.example.com"
    )
    assert registry_hosts_equal(
        "docker://REGISTRY.EXAMPLE.COM/team/app",
        "https://registry.example.com",
    )
    assert not registry_hosts_equal(
        "registry.example.com.evil.test/team/app",
        "registry.example.com",
    )
    assert normalize_registry_host("https://user:secret@registry.example.com/app") == ""


@pytest.mark.parametrize(
    ("value", "account_id", "region", "host"),
    [
        (
            "https://123456789012.dkr.ecr.us-east-1.amazonaws.com/team/app",
            "123456789012",
            "us-east-1",
            "123456789012.dkr.ecr.us-east-1.amazonaws.com",
        ),
        (
            "123456789012.dkr.ecr-fips.us-gov-west-1.amazonaws.com:443",
            "123456789012",
            "us-gov-west-1",
            "123456789012.dkr.ecr-fips.us-gov-west-1.amazonaws.com",
        ),
        (
            "123456789012.dkr.ecr.cn-north-1.amazonaws.com.cn/repository",
            "123456789012",
            "cn-north-1",
            "123456789012.dkr.ecr.cn-north-1.amazonaws.com.cn",
        ),
    ],
)
def test_parse_ecr_registry_accepts_complete_aws_hosts(
    value: str,
    account_id: str,
    region: str,
    host: str,
) -> None:
    registry = parse_ecr_registry(value)

    assert registry is not None
    assert registry.account_id == account_id
    assert registry.region == region
    assert registry.host == host


@pytest.mark.parametrize(
    "value",
    [
        "123456789012.dkr.ecr.us-east-1.amazonaws.com.evil.test",
        "12345678901.dkr.ecr.us-east-1.amazonaws.com",
        "public.ecr.aws",
        "123456789012.dkr.ecr.us_east_1.amazonaws.com",
        "123456789012.dkr.ecr.us-east-1.amazonaws.com:4444",
        "https://user:secret@123456789012.dkr.ecr.us-east-1.amazonaws.com",
    ],
)
def test_parse_ecr_registry_rejects_noncanonical_hosts(value: str) -> None:
    assert parse_ecr_registry(value) is None


def test_registry_credential_inputs_resolve_environment_and_secret_names() -> None:
    assert credential_key_names(
        [ImageCredentialEnvVar.DockerUsername, "CUSTOM_REGISTRY_TOKEN"]
    ) == [ImageCredentialEnvVar.DockerUsername.value, "CUSTOM_REGISTRY_TOKEN"]
    assert resolve_registry_credentials(
        [ImageCredentialEnvVar.DockerUsername, "CUSTOM_REGISTRY_TOKEN"],
        env={
            ImageCredentialEnvVar.DockerUsername.value: "user",
            "CUSTOM_REGISTRY_TOKEN": "token",
        },
    ) == {
        ImageCredentialEnvVar.DockerUsername.value: "user",
        "CUSTOM_REGISTRY_TOKEN": "token",
    }
    assert image_secret_names(["TOKEN=value", " TOKEN ", "PASSWORD=secret"]) == [
        "TOKEN",
        "PASSWORD",
    ]
    with pytest.raises(ImageCredentialLookupError, match="MISSING"):
        resolve_registry_credentials(["MISSING"], env={})
