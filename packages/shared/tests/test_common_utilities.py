from __future__ import annotations

import pytest
from foundation.network import (
    normalize_worker_network_prefix,
    parse_worker_network_prefix,
    worker_network_prefix,
)
from foundation.shell import shell_quote
from foundation.validation import (
    validate_allow_list,
    validate_cidr,
)
from pydantic import ValidationError
from shared.deployment_records import DeploymentSpec
from shared.deployments import StubKind
from shared.urls import (
    InvokeUrlMode,
    StubUrlTarget,
    build_deployment_url,
    build_pod_url,
    build_stub_url,
    pod_proxy_url,
)
from tests.url_constants import EXAMPLE_DOMAIN, EXAMPLE_URL


def test_url_builders_cover_path_host_public_and_ports() -> None:
    target = StubUrlTarget(
        kind="function",
        stub_id="stub-123",
        deployment_name="hello",
        deployment_version=3,
        deployment_subdomain="hello-prod",
    )

    assert (
        build_deployment_url(EXAMPLE_URL, InvokeUrlMode.Path, target)
        == f"{EXAMPLE_URL}/api/v1/functions/hello/v3"
    )
    assert (
        build_deployment_url(EXAMPLE_URL, InvokeUrlMode.Host, target)
        == f"https://hello-prod-v3.{EXAMPLE_DOMAIN}"
    )
    assert (
        build_stub_url(EXAMPLE_URL, InvokeUrlMode.Path, target)
        == f"{EXAMPLE_URL}/api/v1/functions/id/stub-123"
    )

    public_target = target.model_copy(update={"public": True})
    assert (
        build_deployment_url(EXAMPLE_URL, InvokeUrlMode.Path, public_target)
        == f"{EXAMPLE_URL}/api/v1/functions/public/stub-123"
    )

    taskqueue_target = target.model_copy(update={"kind": "task-queue"})
    assert (
        build_deployment_url(EXAMPLE_URL, InvokeUrlMode.Path, taskqueue_target)
        == f"{EXAMPLE_URL}/api/v1/taskqueues/hello/v3"
    )
    assert (
        build_stub_url(EXAMPLE_URL, InvokeUrlMode.Path, taskqueue_target)
        == f"{EXAMPLE_URL}/api/v1/taskqueues/id/stub-123"
    )
    assert (
        build_deployment_url(
            EXAMPLE_URL,
            InvokeUrlMode.Path,
            taskqueue_target.model_copy(update={"public": True}),
        )
        == f"{EXAMPLE_URL}/api/v1/taskqueues/public/stub-123"
    )

    pod_target = target.model_copy(update={"kind": "pod", "ports": [8080]})
    assert (
        build_pod_url(EXAMPLE_URL, InvokeUrlMode.Path, pod_target)
        == f"{EXAMPLE_URL}/pod/id/stub-123/8080"
    )
    assert (
        build_pod_url(
            EXAMPLE_URL,
            InvokeUrlMode.Path,
            pod_target.model_copy(update={"public": True}),
        )
        == f"{EXAMPLE_URL}/pod/public/stub-123/8080"
    )


@pytest.mark.parametrize("port", [0, 65536, 8080.5, "8080", True])
def test_deployment_spec_rejects_invalid_or_coerced_ports(port: object) -> None:
    with pytest.raises(ValidationError):
        DeploymentSpec.model_validate({"name": "pod", "ports": {"http": port}})


def test_canonical_pod_proxy_urls_cover_path_and_host_modes() -> None:
    assert (
        pod_proxy_url(
            "https://lazycloud.dev/",
            resource=StubKind.Sandbox,
            stub_id="stub-1",
            port=8080,
            public=True,
            container_id="container-1",
        )
        == "https://lazycloud.dev/sandbox/public/container-1/8080"
    )
    assert (
        pod_proxy_url(
            "http://127.0.0.1:9000",
            resource=StubKind.Pod,
            stub_id="stub-2",
            port=9000,
            public=False,
        )
        == "http://127.0.0.1:9000/pod/id/stub-2/9000"
    )
    assert (
        pod_proxy_url(
            "https://lazycloud.dev",
            resource=StubKind.Sandbox,
            stub_id="stub-1",
            container_id="container-1",
            port=8080,
            public=False,
            mode=InvokeUrlMode.Host,
        )
        == "https://container-1-8080.lazycloud.dev"
    )
    assert (
        pod_proxy_url(
            "https://lazycloud.dev",
            resource=StubKind.Pod,
            stub_id="stub-2",
            port=9000,
            public=True,
            mode=InvokeUrlMode.Host,
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
            public=False,
        )


def test_validation_network_prefix_and_shell_quote() -> None:
    cidr = validate_cidr("10.0.0.1/24")
    assert cidr.normalized == "10.0.0.0/24"
    assert not cidr.is_ipv6
    assert validate_allow_list(["2001:db8::/32"])[0].is_ipv6
    with pytest.raises(ValueError):
        validate_allow_list(["10.0.0.0/8"] * 11)

    prefix = worker_network_prefix("cluster/a", "node one")
    assert prefix == "cluster:cluster_a:node:node_one"
    parsed_prefix = parse_worker_network_prefix(prefix)
    assert parsed_prefix is not None
    assert parsed_prefix.node_name == "node_one"
    assert (
        normalize_worker_network_prefix("cluster", "node\tone") == "cluster:cluster:node:node_one"
    )
    assert shell_quote("can't") == "'can'\\''t'"
