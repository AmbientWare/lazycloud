from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from api.server.async_io import ApiAsyncIo
from api.server.services import ApiServices
from api.server.tcp_ingress import (
    RedisTcpIngressRouteResolver,
    ReloadingTlsContext,
    TcpIngressRouteNotFound,
    tcp_ingress_hostname,
)
from api.tcp_certificate import ensure_local_tcp_certificate
from control.service import ControlPlaneService, StubKind
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind


@pytest.fixture
async def async_io(isolated_services: ApiServices) -> AsyncIterator[ApiAsyncIo]:
    io = isolated_services.require_async_io()
    await io.start()
    try:
        yield io
    finally:
        await io.close()


@pytest.mark.anyio
async def test_tcp_route_resolver_uses_active_public_pod_hierarchy_and_revalidates_cache(
    isolated_services: ApiServices,
    async_io: ApiAsyncIo,
) -> None:
    deployment = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="tcp-echo",
            kind=DeploymentKind.Pod,
            ports={"tcp": 9090},
            metadata={"app": "networking", "authorized": False, "tcp": True},
        )
    )
    resource = isolated_services.deployment_resources.get_by_deployment_id(deployment.id)
    assert resource is not None
    resolver = RedisTcpIngressRouteResolver(
        services=isolated_services,
        redis=async_io.redis,
        external_host="tcp.example.test",
    )
    sni = tcp_ingress_hostname(resource.stub.id, 9090, "tcp.example.test")

    route = await resolver.resolve(sni)

    assert route.workspace_name == "default"
    assert route.app_id == resource.app.id
    assert route.app_name == "networking"
    assert route.workload_name == "tcp-echo"
    assert route.deployment_id == deployment.id
    assert route.deployment_version == deployment.version
    assert route.stub_id == resource.stub.id
    assert route.port == 9090
    cache_key = async_io.redis.key("tcp-ingress", "sni", sni)
    assert await async_io.redis.ttl(cache_key) > 0

    isolated_services.deployments.delete(deployment.id)

    with pytest.raises(TcpIngressRouteNotFound, match="no active deployment"):
        await resolver.resolve(sni)
    assert await async_io.redis.get(cache_key) is None


@pytest.mark.anyio
async def test_tcp_route_resolver_rejects_private_non_tcp_and_unexposed_pods(
    isolated_services: ApiServices,
    async_io: ApiAsyncIo,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    resolver = RedisTcpIngressRouteResolver(
        services=isolated_services,
        redis=async_io.redis,
        external_host="tcp.example.test",
    )
    private = control.create_stub(
        "private",
        kind=StubKind.Pod,
        public=False,
        config={"ports": {"tcp": 9090}, "tcp": True},
    )
    not_tcp = control.create_stub(
        "not-tcp",
        kind=StubKind.Pod,
        public=True,
        config={"ports": {"tcp": 9090}, "tcp": False},
    )
    wrong_port = control.create_stub(
        "wrong-port",
        kind=StubKind.Pod,
        public=True,
        config={"ports": {"tcp": 8080}, "tcp": True},
    )

    for stub in (private, not_tcp, wrong_port):
        with pytest.raises(TcpIngressRouteNotFound):
            await resolver.resolve(tcp_ingress_hostname(stub.id, 9090, "tcp.example.test"))


@pytest.mark.anyio
async def test_tls_context_captures_sni_and_reloads_atomically_rotated_certificate(
    tmp_path: Path,
) -> None:
    certificate_file = tmp_path / "tls.crt"
    key_file = tmp_path / "tls.key"
    assert ensure_local_tcp_certificate(
        certificate_file=certificate_file,
        key_file=key_file,
        external_host="tcp.localhost",
    )
    tls = await ReloadingTlsContext.create(certificate_file, key_file)
    initial_context = tls._context
    initial_signature = tls._signature
    first = tls.server_context.wrap_bio(ssl.MemoryBIO(), ssl.MemoryBIO(), server_side=True)
    tls._select_context(first, "first.tcp.localhost")

    assert ensure_local_tcp_certificate(
        certificate_file=certificate_file,
        key_file=key_file,
        external_host="tcp.localhost",
        renew_before_days=31,
    )
    await tls.reload_if_changed()
    second = tls.server_context.wrap_bio(ssl.MemoryBIO(), ssl.MemoryBIO(), server_side=True)
    tls._select_context(second, "second.tcp.localhost")

    assert tls._context is not initial_context
    assert tls._signature != initial_signature
    assert tls.server_name(first) == "first.tcp.localhost"
    assert tls.server_name(second) == "second.tcp.localhost"
