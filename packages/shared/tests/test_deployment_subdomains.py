import pytest
from shared.deployment_subdomains import (
    deployment_host_label,
    deployment_subdomain,
    parse_deployment_host,
)
from shared.deployments import DeploymentKind

_IDENTITY = {
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
    difference: dict[str, object],
) -> None:
    assert deployment_subdomain(**_IDENTITY) != deployment_subdomain(**{**_IDENTITY, **difference})


@pytest.mark.parametrize("version", [None, 1, 42])
@pytest.mark.parametrize(
    "resource_name",
    [
        "service",
        "nested-calculation",
        "my_app",
        # A name shaped like the version marker the label also carries.
        "foo-v3",
        # A name with nothing DNS can keep, leaving the digest to stand alone.
        "***",
    ],
)
def test_host_label_round_trips_to_the_resource_it_addresses(
    resource_name: str,
    version: int | None,
) -> None:
    subdomain = deployment_subdomain(**{**_IDENTITY, "name": resource_name})

    target = parse_deployment_host(deployment_host_label(subdomain, version=version))

    assert target is not None
    assert (target.subdomain, target.version) == (subdomain, version)


def test_latest_suffix_addresses_the_same_resource_as_the_bare_label() -> None:
    subdomain = deployment_subdomain(**_IDENTITY)

    assert parse_deployment_host(f"{subdomain}-latest") == parse_deployment_host(subdomain)


@pytest.mark.parametrize("label", ["", "-service", "service-", "Service--x", "service..x"])
def test_labels_that_cannot_stand_in_a_hostname_resolve_to_nothing(label: str) -> None:
    assert parse_deployment_host(label) is None
