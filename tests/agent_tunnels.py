from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from api.server.services import ApiServices
from compute.agent_control import hash_compute_token
from control.service import StubRecord
from coordination.agent_connections import RedisAgentConnectionDirectory
from database.repositories.compute import (
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
)
from database.repositories.orchestration import (
    ContainerRepository,
    MachineRepository,
    WorkerRepository,
)
from networking.tunnel_agent import AgentTunnelClient
from networking.tunnel_gateway import AgentTunnelGateway
from networking.tunnel_tls import TunnelCredentials, agent_certificate_request
from scheduler.fleet import SchedulerContainerStatus
from scheduler.state import SchedulerContainerState
from shared.compute_fleet import Machine, ResourceStatus, Worker
from shared.container_requests import CONTAINER_INNER_PORT
from shared.containers import ContainerRecord, ContainerStatus
from shared.http.agent_identity import AgentCertificateRequest, GatewayCertificateRequest
from shared.routing import AgentBackendRoute, BackendRouteKind, BackendRouteState
from shared.timestamps import utc_now

from tests.releases import assign_runtime
from tests.workspaces import workspace_owner_user_id


@asynccontextmanager
async def enrolled_tunnel_route(
    services: ApiServices,
    stub: StubRecord,
    container_id: str,
    backend_address: str,
    directory: Path,
    *,
    port: int = CONTAINER_INNER_PORT,
) -> AsyncIterator[AgentBackendRoute]:
    container = await asyncio.to_thread(_record_container, services, stub, container_id)
    worker = services.scheduler_workers.get_worker(container.runtime_worker_id)
    assert worker is not None
    owner = workspace_owner_user_id(services.context, stub.workspace_id)
    token = uuid4().hex + uuid4().hex
    with services.context.database.session() as session:
        MachineRepository(session).upsert(
            Machine(
                id=worker.machine_id,
                pool=worker.pool,
                capacity_owner_id=worker.capacity_owner_id,
                status=ResourceStatus.Running,
            ),
            workspace_id=stub.workspace_id,
        )
        WorkerRepository(session).upsert(
            Worker(
                id=worker.worker_id,
                machine_id=worker.machine_id,
                pool=worker.pool,
                status=ResourceStatus.Running,
            ),
            workspace_id=stub.workspace_id,
        )
        enrollment = ComputeMachineEnrollmentRepository(session).create(
            ComputeMachineEnrollmentCreate(
                user_id=owner,
                workspace_id=stub.workspace_id,
                capacity_owner_id=worker.capacity_owner_id,
                pool=worker.pool,
                machine_id=worker.machine_id,
                machine_fingerprint_hash=hash_compute_token(worker.machine_id),
                credential_hash=hash_compute_token(token),
                last_join_at=utc_now(),
            )
        )
    issuer = services.tunnel_certificate_service
    agent_credentials = TunnelCredentials(directory / "agent.key", directory / "agent.json")
    certificate = await asyncio.to_thread(
        issuer.issue_agent,
        AgentCertificateRequest(
            agent_token=token, csr_pem=agent_certificate_request(agent_credentials.key_path)
        ),
    )
    agent_credentials.install(
        certificate_pem=certificate.certificate_pem, trust_bundle_pem=certificate.trust_bundle_pem
    )
    gateway_credentials = TunnelCredentials(directory / "gateway.key", directory / "gateway.json")
    gateway_certificate = issuer.issue_gateway(
        GatewayCertificateRequest(
            csr_pem=agent_certificate_request(gateway_credentials.key_path),
            instance_id=str(uuid4()),
        ),
        authorization=f"Bearer {issuer.settings.gateway_bootstrap_secret.get_secret_value()}",
    )
    gateway_credentials.install(
        certificate_pem=gateway_certificate.certificate_pem,
        trust_bundle_pem=gateway_certificate.trust_bundle_pem,
    )
    route = AgentBackendRoute(
        route_id=":".join(
            (
                worker.machine_id,
                worker.worker_id,
                container_id,
                "container",
                str(port),
            )
        ),
        workspace_id=stub.workspace_id,
        enrollment_id=enrollment.id,
        capacity_owner_id=worker.capacity_owner_id,
        machine_id=worker.machine_id,
        worker_id=worker.worker_id,
        container_id=container_id,
        pool=worker.pool,
        kind=BackendRouteKind.Container,
        port=port,
        local_target=backend_address,
        state=BackendRouteState.Ready,
    )
    with socket.create_server(("127.0.0.1", 0)) as reservation:
        gateway_port = reservation.getsockname()[1]
    gateway_address = f"{issuer.settings.hostname}:{gateway_port}"
    gateway = AgentTunnelGateway(
        issuer.authority,
        RedisAgentConnectionDirectory(services.redis()),
        gateway_address,
        "127.0.0.1:1",
    )
    server = gateway.server(gateway_credentials, listen_address=f"127.0.0.1:{gateway_port}")
    backend_host, _, backend_port = backend_address.rpartition(":")
    agent = AgentTunnelClient(
        gateway_address,
        agent_credentials,
        certificate.expires_at,
        lambda route_id: (backend_host, int(backend_port)) if route_id == route.route_id else None,
    )
    try:
        await server.start()
        await agent.start()
        issuer.authority.compute_states.save_agent_route_state(route)
        services.scheduler_containers.set_container_state(
            SchedulerContainerState(
                container_id=container_id,
                stub_id=stub.id,
                workspace_id=stub.workspace_id,
                status=SchedulerContainerStatus.Running,
            )
        )
        services.scheduler_containers.set_container_address(
            container_id, backend_address, route=route
        )
        services.scheduler_containers.set_container_address_map(
            container_id, {port: backend_address}, routes=[route]
        )
        yield route
    finally:
        await agent.close()
        await gateway.close()
        await server.stop(0)
        issuer.authority.compute_states.delete_agent_route_state(
            workspace_id=route.workspace_id,
            capacity_owner_id=route.capacity_owner_id,
            machine_id=route.machine_id,
            route_id=route.route_id,
        )


def _record_container(
    services: ApiServices,
    stub: StubRecord,
    container_id: str,
) -> ContainerRecord:
    with services.context.database.session() as session:
        record = ContainerRepository(session).upsert(
            ContainerRecord(
                id=container_id,
                name=f"tunnel-{container_id}",
                image="test-image",
                command=["python", "-m", "runner.serve"],
                workspace_id=stub.workspace_id,
                app_id=stub.app_id,
                stub_id=stub.id,
                status=ContainerStatus.Pending,
            )
        )
    assign_runtime(services.containers, services.scheduler_workers, container_id)
    with services.context.database.session() as session:
        assigned = ContainerRepository(session).get_across_workspaces(record.id)
        assert assigned is not None
        return ContainerRepository(session).upsert(
            assigned.model_copy(update={"status": ContainerStatus.Running})
        )
