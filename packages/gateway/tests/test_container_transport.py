import socket
from uuid import uuid4

import pytest
from api.server.services import ApiServices
from gateway.container_transport import HttpContainerServiceTransport
from networking.dialer import BackendRouteConnection, BackendRouteDialer
from networking.sync_http import BackendHttpConnectionPool
from shared.agent_connections import AgentConnectionRecord
from shared.errors import UpstreamTimeoutError, UpstreamUnavailableError
from shared.http.agent_identity import AgentTunnelIdentity
from shared.routing import AgentBackendRoute
from shared.timestamps import utc_now
from worker.container_client.control import plan_container_client_connection_options
from worker.container_client.models import ContainerServiceMethod, ContainerStatusRequest


@pytest.mark.parametrize("stream", [False, True])
def test_unresponsive_container_closes_connection_and_reports_timeout(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    connection, backend = socket.socketpair()

    def dial(
        self: BackendRouteDialer, route_id: str, *, timeout_seconds: float | None = None
    ) -> BackendRouteConnection:
        return BackendRouteConnection(
            AgentBackendRoute(route_id=route_id),
            connection,
            AgentConnectionRecord(
                identity=AgentTunnelIdentity(
                    workspace_id=str(uuid4()),
                    enrollment_id=str(uuid4()),
                    credential_generation=1,
                ),
                gateway_id=str(uuid4()),
                connection_id=str(uuid4()),
                gateway_address="localhost:1234",
                expires_at=utc_now(),
            ),
        )

    monkeypatch.setattr(BackendRouteDialer, "dial_backend_route", dial)
    transport = HttpContainerServiceTransport(
        plan_container_client_connection_options("", backend_route_id="container-route"),
        BackendHttpConnectionPool(isolated_services.backend_route_dialer),
    )
    try:
        with pytest.raises(UpstreamTimeoutError):
            if stream:
                list(
                    transport.stream(
                        ContainerServiceMethod.ContainerStatus,
                        ContainerStatusRequest(container_id="container"),
                        timeout_seconds=0.02,
                    )
                )
            else:
                transport.unary(
                    ContainerServiceMethod.ContainerStatus,
                    ContainerStatusRequest(container_id="container"),
                    timeout_seconds=0.02,
                )
        backend.settimeout(1)
        assert backend.recv(4096)
        assert backend.recv(1) == b""
    finally:
        connection.close()
        backend.close()


def test_missing_container_route_is_an_upstream_failure(isolated_services: ApiServices) -> None:
    transport = HttpContainerServiceTransport(
        plan_container_client_connection_options("", backend_route_id="missing-route"),
        BackendHttpConnectionPool(isolated_services.backend_route_dialer),
    )
    with pytest.raises(UpstreamUnavailableError):
        transport.unary(
            ContainerServiceMethod.ContainerStatus,
            ContainerStatusRequest(container_id="container"),
        )
