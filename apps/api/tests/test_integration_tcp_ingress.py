from __future__ import annotations

import ssl
from pathlib import Path

import pytest
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


@pytest.mark.anyio
async def test_tcp_route_resolver_uses_active_public_pod_hierarchy_and_revalidates_cache(
    async_services: ApiServices,
) -> None:
    async_io = async_services.require_async_io()
    deployment = async_services.deployments.deploy(
        DeploymentSpec(
            name="tcp-echo",
            kind=DeploymentKind.Pod,
            ports={"tcp": 9090},
            metadata={"app": "networking", "authorized": False, "tcp": True},
        )
    )
    resource = async_services.deployment_resources.get_by_deployment_id(deployment.id)
    assert resource is not None
    resolver = RedisTcpIngressRouteResolver(
        services=async_services,
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

    async_services.deployments.delete(deployment.id)

    with pytest.raises(TcpIngressRouteNotFound, match="no active deployment"):
        await resolver.resolve(sni)
    assert await async_io.redis.get(cache_key) is None


@pytest.mark.anyio
async def test_tcp_route_resolver_rejects_private_non_tcp_and_unexposed_pods(
    async_services: ApiServices,
) -> None:
    async_io = async_services.require_async_io()
    control = ControlPlaneService(async_services.context)
    resolver = RedisTcpIngressRouteResolver(
        services=async_services,
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
    accepting_context = tls.server_context
    first, first_certificate = _handshake(
        accepting_context, "first.tcp.localhost", certificate_file
    )

    assert ensure_local_tcp_certificate(
        certificate_file=certificate_file,
        key_file=key_file,
        external_host="tcp.localhost",
        renew_before_days=31,
    )
    await tls.reload_if_changed()
    second, second_certificate = _handshake(
        accepting_context, "second.tcp.localhost", certificate_file
    )

    assert first_certificate != second_certificate
    assert tls.server_name(first) == "first.tcp.localhost"
    assert tls.server_name(second) == "second.tcp.localhost"


def _handshake(
    context: ssl.SSLContext, server_name: str, certificate_file: Path
) -> tuple[ssl.SSLObject, bytes]:
    client_context = ssl.create_default_context(cafile=certificate_file)
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_in, client_out, server_hostname=server_name)
    server = context.wrap_bio(server_in, server_out, server_side=True)
    client_ready = server_ready = False
    for _ in range(10):
        if not client_ready:
            try:
                client.do_handshake()
                client_ready = True
            except ssl.SSLWantReadError:
                pass
        if outgoing := client_out.read():
            server_in.write(outgoing)
        if not server_ready:
            try:
                server.do_handshake()
                server_ready = True
            except ssl.SSLWantReadError:
                pass
        if outgoing := server_out.read():
            client_in.write(outgoing)
        if client_ready and server_ready:
            certificate = client.getpeercert(binary_form=True)
            assert certificate is not None
            return server, certificate
    raise AssertionError("TLS handshake did not complete")
