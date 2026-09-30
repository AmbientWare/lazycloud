from typing import TypedDict

import pytest
from shared.deployment_subdomains import (
    DeploymentHostTarget,
    deployment_host_label,
    deployment_subdomain,
    parse_deployment_host,
)
from shared.deployments import DeploymentKind


class _Identity(TypedDict):
    """The arguments `deployment_subdomain` takes, so splatting stays checkable."""

    workspace_id: str
    app_name: str
    name: str
    kind: DeploymentKind


class _IdentityDiff(TypedDict, total=False):
    workspace_id: str
    app_name: str
    name: str
    kind: DeploymentKind


_IDENTITY: _Identity = {
    "workspace_id": "workspace-1",
    "app_name": "demo",
    "name": "service",
    "kind": DeploymentKind.Asgi,
}


def test_subdomain_is_stable_for_one_resource() -> None:
    assert deployment_subdomain(**_IDENTITY) == deployment_subdomain(**_IDENTITY)


@pytest.mark.parametrize(
    "difference",
    [
        {"workspace_id": "workspace-2"},
        {"app_name": "other"},
        {"name": "predict"},
        {"kind": DeploymentKind.Endpoint},
    ],
)
def test_resources_that_differ_anywhere_get_different_subdomains(
    difference: _IdentityDiff,
) -> None:
    other: _Identity = {**_IDENTITY, **difference}
    assert deployment_subdomain(**_IDENTITY) != deployment_subdomain(**other)


@pytest.mark.parametrize("version", [None, 1, 42])
@pytest.mark.parametrize(
    "resource_name",
    [
        "service",
        "nested-calculation",
        "my_app",
        # A name shaped like the version suffix the label also carries.
        "foo-v3",
        # A name with nothing DNS can keep, leaving the digest to stand alone.
        "***",
        "x" * 200,
    ],
)
def test_host_label_round_trips_to_the_resource_and_version_it_addresses(
    resource_name: str,
    version: int | None,
) -> None:
    named: _Identity = {**_IDENTITY, "name": resource_name}
    subdomain = deployment_subdomain(**named)
    label = deployment_host_label(subdomain, version=version)

    assert len(label) <= 63
    assert parse_deployment_host(label) == DeploymentHostTarget(
        subdomain=subdomain,
        version=version,
    )


def test_latest_suffix_addresses_the_same_resource_as_the_bare_label() -> None:
    subdomain = deployment_subdomain(**_IDENTITY)

    assert parse_deployment_host(f"{subdomain}-latest") == parse_deployment_host(subdomain)


@pytest.mark.parametrize("label", ["", "-service", "service-", "service..x", "a--b"])
def test_labels_that_cannot_stand_in_a_hostname_are_refused(label: str) -> None:
    assert parse_deployment_host(label) is None


def test_host_labels_are_matched_case_insensitively_as_dns_is() -> None:
    assert parse_deployment_host("Service-A1B2C3D4") == parse_deployment_host("service-a1b2c3d4")
