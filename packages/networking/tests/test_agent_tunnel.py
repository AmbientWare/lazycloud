from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import grpc
import grpc.aio
import pytest
from compute.state import RedisComputeStateRepository
from compute.tunnel_authority import AgentTunnelAuthority
from coordination.agent_connections import RedisAgentConnectionDirectory
from database.context import ServiceContext
from database.repositories.compute import (
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
    ComputeUnitRepository,
)
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from identity.tunnel_certificates import (
    TunnelCertificateIssuer,
    create_tunnel_certificate_authority,
    csr_public_key_sha256,
)
from networking.async_http import (
    AsyncBackendConnectError,
    AsyncBackendHttpClient,
    AsyncBackendHttpResponse,
    AsyncBackendResponseError,
)
from networking.dialer import BackendRouteDialer, BackendRouteUnavailable
from networking.tunnel_agent import AgentTunnelClient
from networking.tunnel_client import TunnelRouteClient
from networking.tunnel_gateway import AgentTunnelGateway
from networking.tunnel_protocol import CONNECT_METHOD, ROUTE_METHOD, tunnel_method
from networking.tunnel_tls import TunnelCredentials, agent_certificate_request
from scheduler.routes import SchedulerBackendRouteResolver
from scheduler.state import RedisSchedulerContainerRepository
from shared.compute_enrollment import ComputeMachineEnrollmentStatus
from shared.compute_fleet import Machine, ResourceStatus, Worker
from shared.compute_policy import ComputeUnitRecord, UnitName
from shared.containers import ContainerRecord, ContainerStatus
from shared.http.agent_identity import TunnelServiceRole
from shared.http.agent_tunnel import TUNNEL_MAX_ROUTE_STREAMS, TunnelRouteRequest
from shared.placement import Placement
from shared.routing import AgentBackendRoute, BackendRouteKind, BackendRouteState
from shared.timestamps import utc_now
from tests.real_redis import RealRedisActors
from tests.workspaces import workspace_owner_user_id

from identity import tunnel_certificates


def test_real_agent_tunnel_preserves_half_close_and_revokes_open_streams(
    committed_service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = committed_service_context
    redis = real_redis_actors.client()
    directory = RedisAgentConnectionDirectory(redis)
    states = RedisComputeStateRepository(redis)
    authority = AgentTunnelAuthority(context.database, states)
    with context.database.session() as session:
        workspace = context.default_workspace_id(session)
    owner = workspace_owner_user_id(context, workspace)
    machine_id, worker_id, capacity_owner, container_id = (str(uuid4()) for _ in range(4))
    with context.database.session() as session:
        ComputeUnitRepository(session).upsert(
            ComputeUnitRecord(
                id=capacity_owner,
                capacity_owner_id=capacity_owner,
                workspace_id=workspace,
                name=UnitName("tunnel-machine"),
                placement=Placement.platform(),
            )
        )
        MachineRepository(session).upsert(
            Machine(id=machine_id, capacity_owner_id=capacity_owner, status=ResourceStatus.Created),
            workspace_id=workspace,
        )
        WorkerRepository(session).upsert(
            Worker(
                id=worker_id,
                machine_id=machine_id,
                placement=Placement.platform(),
                status=ResourceStatus.Created,
            ),
            workspace_id=workspace,
        )
        ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name="tunnel-workload",
                workspace_id=workspace,
                image="",
                command=[],
                runtime_machine_id=machine_id,
                runtime_worker_id=worker_id,
                status=ContainerStatus.Running,
            )
        )
        enrollment = ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                user_id=owner,
                workspace_id=workspace,
                capacity_owner_id=capacity_owner,
                placement=Placement.platform(),
                machine_id=machine_id,
                machine_fingerprint_hash=hashlib.sha256(machine_id.encode()).hexdigest(),
                credential_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
                last_join_at=utc_now(),
            )
        )
    ca = create_tunnel_certificate_authority(deployment_hostname="localhost")
    ca_file, ca_key = tmp_path / "ca.pem", tmp_path / "ca.key"
    ca_file.write_text(ca.certificate_pem)
    ca_key.write_text(ca.private_key_pem)
    ca_key.chmod(0o600)
    issuer = TunnelCertificateIssuer.load(ca_file, ca_key)
    agent_credentials = TunnelCredentials(tmp_path / "agent.key", tmp_path / "agent.json")
    csr = agent_certificate_request(agent_credentials.key_path)
    identity = authority.bind_key(
        enrollment.id, workspace, enrollment.credential_hash, csr_public_key_sha256(csr)
    )
    agent_certificate = issuer.issue_agent(csr, identity)
    agent_credentials.install(
        certificate_pem=agent_certificate.certificate_pem, trust_bundle_pem=issuer.trust_bundle_pem
    )
    gateway_credentials = TunnelCredentials(tmp_path / "gateway.key", tmp_path / "gateway.json")
    gateway_certificate = issuer.issue_service(
        agent_certificate_request(gateway_credentials.key_path),
        TunnelServiceRole.Gateway,
        str(uuid4()),
        ("localhost",),
    )
    gateway_credentials.install(
        certificate_pem=gateway_certificate.certificate_pem,
        trust_bundle_pem=issuer.trust_bundle_pem,
    )
    control_credentials = TunnelCredentials(tmp_path / "control.key", tmp_path / "control.json")
    control_certificate = issuer.issue_service(
        agent_certificate_request(control_credentials.key_path),
        TunnelServiceRole.ControlPlane,
        str(uuid4()),
    )
    control_credentials.install(
        certificate_pem=control_certificate.certificate_pem,
        trust_bundle_pem=issuer.trust_bundle_pem,
    )

    async def run() -> None:
        http_connections = 0
        http_requests = 0

        async def respond_after_eof(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            nonlocal http_connections, http_requests
            digest = hashlib.sha256()
            try:
                initial = await reader.read(65536)
                if initial.startswith(b"GET "):
                    http_connections += 1
                    connection_id = str(http_connections).encode()
                    buffered = initial
                    while buffered:
                        while b"\r\n\r\n" not in buffered:
                            chunk = await reader.read(65536)
                            if not chunk:
                                return
                            buffered += chunk
                        head, buffered = buffered.split(b"\r\n\r\n", 1)
                        http_requests += 1
                        if head.startswith(b"GET /disconnect "):
                            return
                        writer.write(
                            b"HTTP/1.1 200 OK\r\nContent-Length: "
                            + str(len(connection_id)).encode()
                            + b"\r\n\r\n"
                            + connection_id
                        )
                        await writer.drain()
                        if not buffered:
                            buffered = await reader.read(65536)
                    return
                digest.update(initial)
                while chunk := await reader.read(65536):
                    digest.update(chunk)
                if digest.digest() == hashlib.sha256(b"slow-reader").digest():
                    for _ in range(1024):
                        writer.write(b"x" * 65536)
                        await writer.drain()
                else:
                    writer.write(digest.hexdigest().encode())
                    await writer.drain()
            except ConnectionError:
                return
            finally:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()

        backend = await asyncio.start_server(respond_after_eof, "127.0.0.1", 0)
        backend_port = backend.sockets[0].getsockname()[1]
        with socket.create_server(("127.0.0.1", 0)) as reservation:
            gateway_port = reservation.getsockname()[1]
        route = AgentBackendRoute(
            route_id=f"{machine_id}:{worker_id}:{container_id}:worker:0",
            enrollment_id=enrollment.id,
            workspace_id=workspace,
            capacity_owner_id=capacity_owner,
            machine_id=machine_id,
            worker_id=worker_id,
            container_id=container_id,
            placement=Placement.platform(),
            kind=BackendRouteKind.Worker,
            state=BackendRouteState.Ready,
            local_target=f"127.0.0.1:{backend_port}",
            port=0,
        )
        states.save_agent_route_state(route)
        gateway = AgentTunnelGateway(
            authority, directory, f"localhost:{gateway_port}", f"127.0.0.1:{backend_port}"
        )
        server = gateway.server(gateway_credentials, listen_address=f"127.0.0.1:{gateway_port}")
        await server.start()
        local_routes = {route.route_id: ("127.0.0.1", backend_port)}
        routes_changed = asyncio.Event()
        watching_routes = await gateway.start_route_notifications()
        agent = AgentTunnelClient(
            f"localhost:{gateway_port}",
            agent_credentials,
            agent_certificate.expires_at,
            local_routes.get,
            lambda update: routes_changed.set(),
        )
        client = TunnelRouteClient(directory, control_credentials, "localhost")
        containers = RedisSchedulerContainerRepository(redis)
        containers.set_worker_address(container_id, route.local_target, route=route)
        dialer = BackendRouteDialer(client, SchedulerBackendRouteResolver(containers))
        http = AsyncBackendHttpClient(dialer)
        replacement: AgentTunnelClient | None = None
        saturated_agent: AgentTunnelClient | None = None
        expiring: AgentTunnelClient | None = None
        renewed: AgentTunnelClient | None = None
        expiring_client: TunnelRouteClient | None = None
        long_lived: socket.socket | None = None
        replacement_gateway: AgentTunnelGateway | None = None
        replacement_server = None
        listener: asyncio.Server | None = None
        try:
            for credentials, method in (
                (agent_credentials, ROUTE_METHOD),
                (gateway_credentials, ROUTE_METHOD),
                (control_credentials, CONNECT_METHOD),
            ):
                async with grpc.aio.secure_channel(
                    f"localhost:{gateway_port}", credentials.client().credentials
                ) as channel:
                    call = tunnel_method(channel, method)(timeout=2)
                    with pytest.raises(grpc.aio.AioRpcError) as refused:
                        await anext(call.__aiter__())
                    assert refused.value.code() is grpc.StatusCode.PERMISSION_DENIED
            disconnected = AgentTunnelClient(
                agent.address,
                agent_credentials,
                agent_certificate.expires_at,
                agent.resolve_route,
                lambda update: None,
            )
            try:
                await disconnected.start()
                record = directory.get(workspace, enrollment.id)
                assert record is not None
                assert record.connection_id == disconnected.connection_id
            finally:
                await disconnected.close()
            for _ in range(10):
                record = directory.get(workspace, enrollment.id)
                print(f"disconnected agent lease={record is not None}")
                if record is None:
                    break
                await asyncio.sleep(0.05)
            assert record is None
            await agent.start()
            await asyncio.to_thread(directory.notify_route_changed, route, 1)
            for _ in range(30):
                print(
                    f"route notification={routes_changed.is_set()} "
                    f"watcher_done={watching_routes.done()}"
                )
                if routes_changed.is_set():
                    break
                await asyncio.sleep(0.01)
            assert routes_changed.is_set()
            request = TunnelRouteRequest(
                workspace_id=workspace, enrollment_id=enrollment.id, route_id=route.route_id
            )
            with socket.socket() as closed_backend:
                closed_backend.bind(("127.0.0.1", 0))
                local_routes[route.route_id] = ("127.0.0.1", closed_backend.getsockname()[1])
                started = asyncio.get_running_loop().time()
                try:
                    with pytest.raises(BackendRouteUnavailable, match="connection_failed"):
                        await asyncio.to_thread(
                            dialer.dial_backend_route, route.route_id, timeout_seconds=5
                        )
                finally:
                    elapsed = asyncio.get_running_loop().time() - started
                    print(f"closed backend rejection seconds={elapsed:.6f}")
                assert elapsed < 1
            del local_routes[route.route_id]
            started = asyncio.get_running_loop().time()
            with pytest.raises(grpc.aio.AioRpcError) as missing:
                await asyncio.to_thread(client.connect, request, 5)
            elapsed = asyncio.get_running_loop().time() - started
            print(f"missing route rejection seconds={elapsed:.6f}")
            assert missing.value.code() is grpc.StatusCode.FAILED_PRECONDITION
            assert elapsed < 1
            local_routes[route.route_id] = ("127.0.0.1", backend_port)
            payload = b"request-before-half-close" * 16000

            def exchange() -> None:
                with dialer.dial_backend_route(
                    route.route_id, timeout_seconds=5
                ).socket as connection:
                    connection.settimeout(5)
                    connection.sendall(payload)
                    connection.shutdown(socket.SHUT_WR)
                    response = bytearray()
                    while chunk := connection.recv(65536):
                        response.extend(chunk)
                assert bytes(response) == hashlib.sha256(payload).hexdigest().encode()

            await asyncio.to_thread(exchange)
            try:

                async def get(path: str = "/") -> AsyncBackendHttpResponse:
                    return await http.open_stream(
                        address=route.local_target,
                        route_id=route.route_id,
                        method="GET",
                        path=path,
                        headers={},
                        body=b"",
                        timeout_seconds=5,
                        resource="test workload",
                    )

                assert await (await get()).read() == b"1"
                assert await (await get()).read() == b"1"
                changed = route.model_copy(update={"updated_at": 1})
                states.save_agent_route_state(changed)
                containers.set_worker_address(container_id, route.local_target, route=changed)
                assert await (await get()).read() == b"2"
                with pytest.raises(AsyncBackendResponseError):
                    await get("/disconnect")
                assert http_requests == 4
                unread = await get()
                await unread.close()
                with pytest.raises(AsyncBackendResponseError, match="closed"):
                    await unread.read()
                assert await (await get()).read() == b"4"
                closing = route.model_copy(update={"state": BackendRouteState.Closing})
                states.save_agent_route_state(closing)
                containers.set_worker_address(container_id, route.local_target, route=closing)
                with pytest.raises(AsyncBackendConnectError):
                    await get()
                assert http_requests == 6
            finally:
                states.save_agent_route_state(route)
                containers.set_worker_address(container_id, route.local_target, route=route)
            occupied: list[socket.socket] = []
            try:
                for _ in range(TUNNEL_MAX_ROUTE_STREAMS + 1):
                    try:
                        opened = await asyncio.to_thread(client.connect, request, 5)
                    except grpc.aio.AioRpcError as exc:
                        assert exc.code() is grpc.StatusCode.RESOURCE_EXHAUSTED
                        break
                    occupied.append(opened.socket)
                else:
                    pytest.fail("route saturation did not preserve control capacity")
                saturated_agent = agent
                agent = AgentTunnelClient(
                    saturated_agent.address,
                    agent_credentials,
                    agent_certificate.expires_at,
                    local_routes.get,
                    lambda update: None,
                    streams=saturated_agent.streams,
                    route_streams=saturated_agent.route_streams,
                )
                await agent.start()
                started = asyncio.get_running_loop().time()
                with pytest.raises(grpc.aio.AioRpcError) as saturated:
                    await asyncio.to_thread(client.connect, request, 5)
                elapsed = asyncio.get_running_loop().time() - started
                print(f"agent stream limit rejection seconds={elapsed:.6f}")
                assert saturated.value.code() is grpc.StatusCode.RESOURCE_EXHAUSTED
                assert elapsed < 1
                listener = await asyncio.start_server(agent.forward_control, "127.0.0.1", 0)
                control_reader, control_writer = await asyncio.open_connection(
                    "127.0.0.1", listener.sockets[0].getsockname()[1]
                )
                try:
                    control_writer.write(b"control-under-load")
                    await control_writer.drain()
                    control_writer.write_eof()
                    async with asyncio.timeout(5):
                        assert (
                            await control_reader.read()
                            == hashlib.sha256(b"control-under-load").hexdigest().encode()
                        )
                finally:
                    control_writer.close()
                    await control_writer.wait_closed()
            finally:

                def finish_connection(connection: socket.socket) -> None:
                    try:
                        connection.settimeout(5)
                        connection.shutdown(socket.SHUT_WR)
                        while connection.recv(65536):
                            pass
                    finally:
                        connection.close()

                await asyncio.gather(
                    *(asyncio.to_thread(finish_connection, connection) for connection in occupied)
                )
            await saturated_agent.retire()
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", listener.sockets[0].getsockname()[1]
            )
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            async with asyncio.timeout(5):
                assert await reader.read() == hashlib.sha256(payload).hexdigest().encode()
            writer.close()
            await writer.wait_closed()

            old_connection = (await asyncio.to_thread(client.connect, request, 5)).socket
            try:
                assert await (await get()).read() == b"5"
                ready_revision = await http.readiness_revision(route.route_id, route.local_target)
                assert ready_revision is not None
                await gateway.drain()
                async with asyncio.timeout(5):
                    await agent.wait_disconnected()
                retiring = asyncio.create_task(agent.retire())
                during_replacement = asyncio.create_task(asyncio.to_thread(exchange))
                for _ in range(2):
                    connected = directory.get(workspace, enrollment.id) is not None
                    print(
                        f"replacement connection={connected} "
                        f"request_done={during_replacement.done()}"
                    )
                    assert not during_replacement.done()
                    await asyncio.sleep(0.05)
                assert await http.readiness_revision(route.route_id, route.local_target) is None
                with socket.create_server(("127.0.0.1", 0)) as reservation:
                    replacement_port = reservation.getsockname()[1]
                replacement_gateway = AgentTunnelGateway(
                    authority,
                    directory,
                    f"localhost:{replacement_port}",
                    f"127.0.0.1:{backend_port}",
                )
                replacement_server = replacement_gateway.server(
                    gateway_credentials, listen_address=f"127.0.0.1:{replacement_port}"
                )
                await replacement_server.start()
                replacement = AgentTunnelClient(
                    f"localhost:{replacement_port}",
                    agent_credentials,
                    agent_certificate.expires_at,
                    agent.resolve_route,
                    lambda update: None,
                )
                await replacement.start()
                assert await (await get()).read() == b"6"
                replacement_revision = await http.readiness_revision(
                    route.route_id, route.local_target
                )
                assert replacement_revision is not None and replacement_revision != ready_revision
                await during_replacement
                await asyncio.to_thread(exchange)

                def finish_accepted_stream() -> None:
                    old_connection.settimeout(5)
                    old_connection.sendall(b"accepted-before-drain")
                    old_connection.shutdown(socket.SHUT_WR)
                    with old_connection.makefile("rb") as response:
                        assert (
                            response.read()
                            == hashlib.sha256(b"accepted-before-drain").hexdigest().encode()
                        )

                await asyncio.to_thread(finish_accepted_stream)
                async with asyncio.timeout(5):
                    await retiring
                current = directory.get(workspace, enrollment.id)
                assert current is not None
                assert current.connection_id == replacement.connection_id
            finally:
                old_connection.close()

            # A stream outlives the agent and control plane certificates that admitted
            # it once a renewal has replaced the session carrying it.
            lifetime = timedelta(seconds=6)
            monkeypatch.setattr(tunnel_certificates, "TUNNEL_CERTIFICATE_LIFETIME", lifetime)
            expiring_agent = issuer.issue_agent(
                agent_certificate_request(agent_credentials.key_path), identity
            )
            expiring_credentials = TunnelCredentials(
                agent_credentials.key_path, tmp_path / "agent-expiring.json"
            )
            expiring_credentials.install(
                certificate_pem=expiring_agent.certificate_pem,
                trust_bundle_pem=issuer.trust_bundle_pem,
            )
            expiring_control = TunnelCredentials(
                control_credentials.key_path, tmp_path / "control-expiring.json"
            )
            expiring_control.install(
                certificate_pem=issuer.issue_service(
                    agent_certificate_request(control_credentials.key_path),
                    TunnelServiceRole.ControlPlane,
                    str(uuid4()),
                ).certificate_pem,
                trust_bundle_pem=issuer.trust_bundle_pem,
            )
            assert replacement_gateway is not None and replacement is not None
            expiring = AgentTunnelClient(
                replacement.address,
                expiring_credentials,
                expiring_agent.expires_at,
                agent.resolve_route,
                lambda update: None,
            )
            await expiring.start()
            async with asyncio.timeout(5):
                await replacement.wait_disconnected()
            await replacement.retire()
            expiring_client = TunnelRouteClient(directory, expiring_control, "localhost")
            long_lived = (await asyncio.to_thread(expiring_client.connect, request, 5)).socket
            renewed = AgentTunnelClient(
                replacement.address,
                agent_credentials,
                agent_certificate.expires_at,
                agent.resolve_route,
                lambda update: None,
            )
            await renewed.start()
            async with asyncio.timeout(5):
                await expiring.wait_disconnected()
            retiring_expired = asyncio.create_task(expiring.retire())
            while utc_now() < expiring_agent.expires_at + timedelta(seconds=2):
                print(
                    f"certificate expired={utc_now() >= expiring_agent.expires_at} "
                    f"retiring={not retiring_expired.done()} "
                    f"streams={len(expiring.streams)}"
                )
                await asyncio.sleep(1)
            assert not retiring_expired.done()

            def finish_long_lived_stream() -> None:
                assert long_lived is not None
                long_lived.settimeout(5)
                long_lived.sendall(b"after-expiry")
                long_lived.shutdown(socket.SHUT_WR)
                with long_lived.makefile("rb") as response:
                    assert response.read() == hashlib.sha256(b"after-expiry").hexdigest().encode()

            await asyncio.to_thread(finish_long_lived_stream)
            async with asyncio.timeout(5):
                await retiring_expired

            connection = (await asyncio.to_thread(client.connect, request, 5)).socket
            try:
                connection.settimeout(2)
                connection.sendall(b"slow-reader")
                connection.shutdown(socket.SHUT_WR)
                assert await asyncio.to_thread(connection.recv, 1) == b"x"
                with context.database.session() as session:
                    repository = ComputeMachineEnrollmentRepository(session)
                    repository.save(
                        enrollment.model_copy(
                            update={"status": ComputeMachineEnrollmentStatus.Revoked}
                        )
                    )
                started = asyncio.get_running_loop().time()
                record = await asyncio.to_thread(directory.get, workspace, enrollment.id)
                for _ in range(40):
                    record = await asyncio.to_thread(directory.get, workspace, enrollment.id)
                    print(
                        f"revocation elapsed={asyncio.get_running_loop().time() - started:.2f}s "
                        f"connection={record is not None} streams={len(renewed.streams)}"
                    )
                    if record is None:
                        break
                    await asyncio.sleep(0.2)
                assert record is None

                def drain_revoked_stream() -> int:
                    connection.settimeout(1)
                    size = 1
                    while chunk := connection.recv(65536):
                        size += len(chunk)
                    return size

                assert await asyncio.to_thread(drain_revoked_stream) < 64 * 1024 * 1024
                assert asyncio.get_running_loop().time() - started < 10
            finally:
                connection.close()
        finally:
            watching_routes.cancel()
            await asyncio.gather(watching_routes, return_exceptions=True)
            await http.close()
            if listener is not None:
                listener.close()
                await listener.wait_closed()
            if long_lived is not None:
                long_lived.close()
            await asyncio.to_thread(client.close)
            if expiring_client is not None:
                await asyncio.to_thread(expiring_client.close)
            await agent.close()
            for session in (replacement, saturated_agent, expiring, renewed):
                if session is not None:
                    await session.close()
            if replacement_server is not None:
                await replacement_server.stop(0)
            if replacement_gateway is not None:
                await replacement_gateway.close()
            await server.stop(0)
            await gateway.close()
            backend.close()
            await backend.wait_closed()

    asyncio.run(run())
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and (record.name.startswith("networking.") or record.name == "grpc._cython.cygrpc")
    ]
