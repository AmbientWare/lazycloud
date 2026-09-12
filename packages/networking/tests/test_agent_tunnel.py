from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import grpc
import grpc.aio
import pytest
from compute.state import RedisComputeStateRepository
from compute.tunnel_authority import AgentTunnelAuthority
from control.routes import RouteService
from coordination.agent_connections import RedisAgentConnectionDirectory
from database.context import ServiceContext
from database.repositories.compute import (
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
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
from networking.dialer import BackendRouteDialer
from networking.tunnel_agent import AgentTunnelClient
from networking.tunnel_client import TunnelRouteClient
from networking.tunnel_gateway import AgentTunnelGateway
from networking.tunnel_protocol import CONNECT_METHOD, ROUTE_METHOD, tunnel_method
from networking.tunnel_tls import TunnelCredentials, agent_certificate_request
from scheduler.routes import SchedulerBackendRouteResolver
from scheduler.state import RedisSchedulerContainerRepository
from shared.compute_enrollment import ComputeMachineEnrollmentStatus
from shared.compute_fleet import Machine, ResourceStatus, Worker
from shared.compute_policy import MachinePool
from shared.containers import ContainerRecord, ContainerStatus
from shared.http.agent_identity import TunnelServiceRole
from shared.http.agent_tunnel import TunnelRouteRequest
from shared.routing import AgentBackendRoute, BackendRouteKind, BackendRouteState
from shared.timestamps import utc_now
from tests.real_redis import RealRedisActors
from tests.workspaces import workspace_owner_user_id


def test_real_agent_tunnel_preserves_half_close_and_revokes_open_streams(
    committed_service_context: ServiceContext,
    real_redis_actors: RealRedisActors,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
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
        MachineRepository(session).upsert(
            Machine(id=machine_id, capacity_owner_id=capacity_owner, status=ResourceStatus.Created),
            workspace_id=workspace,
        )
        WorkerRepository(session).upsert(
            Worker(
                id=worker_id,
                machine_id=machine_id,
                pool=MachinePool("lazycloud"),
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
                pool=MachinePool("lazycloud"),
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
        async def respond_after_eof(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            digest = hashlib.sha256()
            try:
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
            pool=MachinePool("lazycloud"),
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
        agent = AgentTunnelClient(
            f"localhost:{gateway_port}",
            agent_credentials,
            agent_certificate.expires_at,
            lambda route_id: ("127.0.0.1", backend_port) if route_id == route.route_id else None,
        )
        client = TunnelRouteClient(directory, control_credentials, "localhost")
        containers = RedisSchedulerContainerRepository(redis)
        containers.set_worker_address(container_id, route.local_target, route=route)
        dialer = BackendRouteDialer(
            client, SchedulerBackendRouteResolver(RouteService(context), containers)
        )
        replacement: AgentTunnelClient | None = None
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
            request = TunnelRouteRequest(
                workspace_id=workspace, enrollment_id=enrollment.id, route_id=route.route_id
            )
            payload = b"request-before-half-close" * 16000

            def exchange() -> None:
                with dialer.dial_backend_route(route.route_id, timeout_seconds=5) as connection:
                    connection.settimeout(5)
                    connection.sendall(payload)
                    connection.shutdown(socket.SHUT_WR)
                    response = bytearray()
                    while chunk := connection.recv(65536):
                        response.extend(chunk)
                assert bytes(response) == hashlib.sha256(payload).hexdigest().encode()

            await asyncio.to_thread(exchange)
            listener = await asyncio.start_server(agent.forward_control, "127.0.0.1", 0)
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

            old_connection = await asyncio.to_thread(client.connect, request, 5)
            try:
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
                )
                await replacement.start()
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

            connection = await asyncio.to_thread(client.connect, request, 5)
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
                        f"connection={record is not None} streams={len(replacement.streams)}"
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
            if listener is not None:
                listener.close()
                await listener.wait_closed()
            await asyncio.to_thread(client.close)
            await agent.close()
            if replacement is not None:
                await replacement.close()
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
