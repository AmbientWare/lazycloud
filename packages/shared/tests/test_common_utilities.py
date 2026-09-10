from __future__ import annotations

import pytest
from pydantic import ValidationError
from shared.deployment_records import DeploymentSpec
from shared.deployments import StubKind
from shared.urls import (
    StubUrlTarget,
    build_deployment_url,
    build_pod_url,
    build_stub_url,
    pod_proxy_url,
)
from tests.url_constants import EXAMPLE_DOMAIN, EXAMPLE_URL


def test_deployed_resources_are_addressed_by_hostname() -> None:
    target = StubUrlTarget(
        kind="function",
        stub_id="stub-123",
        deployment_name="hello",
        deployment_version=3,
        subdomain="hello-a1b2c3d4",
    )

    assert build_deployment_url(EXAMPLE_URL, target) == f"https://hello-a1b2c3d4.{EXAMPLE_DOMAIN}"
    assert (
        build_deployment_url(EXAMPLE_URL, target, pin_version=True)
        == f"https://hello-a1b2c3d4-v3.{EXAMPLE_DOMAIN}"
    )
    assert build_stub_url(EXAMPLE_URL, target) == f"https://stub-123.{EXAMPLE_DOMAIN}"

    pod_target = target.model_copy(update={"kind": "pod", "ports": [8080]})
    assert build_pod_url(EXAMPLE_URL, pod_target) == f"https://stub-123-8080.{EXAMPLE_DOMAIN}"


def test_a_deployment_without_a_subdomain_has_no_address() -> None:
    with pytest.raises(ValueError, match="subdomain"):
        build_deployment_url(
            EXAMPLE_URL,
            StubUrlTarget(kind="function", stub_id="stub-123"),
        )


@pytest.mark.parametrize("port", [0, 65536, 8080.5, "8080", True])
def test_deployment_spec_rejects_invalid_or_coerced_ports(port: object) -> None:
    with pytest.raises(ValidationError):
        DeploymentSpec.model_validate({"name": "pod", "ports": {"http": port}})


def test_pod_and_sandbox_ports_are_addressed_by_hostname() -> None:
    assert (
        pod_proxy_url(
            "https://lazycloud.dev",
            resource=StubKind.Sandbox,
            stub_id="stub-1",
            container_id="container-1",
            port=8080,
        )
        == "https://container-1-8080.lazycloud.dev"
    )
    assert (
        pod_proxy_url(
            "https://lazycloud.dev",
            resource=StubKind.Pod,
            stub_id="stub-2",
            port=9000,
        )
        == "https://stub-2-9000.lazycloud.dev"
    )


@pytest.mark.parametrize(
    ("origin", "port", "message"),
    [
        ("https://lazy cloud.dev", 8080, None),
        (" https://lazycloud.dev", 8080, None),
        ("https://lazycloud.dev", 0, "between 1 and 65535"),
        ("https://lazycloud.dev", 65536, "between 1 and 65535"),
        ("https://lazycloud.dev", True, "between 1 and 65535"),
    ],
)
def test_pod_proxy_url_rejects_invalid_origin_and_port_matrix(
    origin: str,
    port: int,
    message: str | None,
) -> None:
    with pytest.raises(ValueError, match=message):
        pod_proxy_url(
            origin,
            resource=StubKind.Pod,
            stub_id="stub-1",
            port=port,
        )
