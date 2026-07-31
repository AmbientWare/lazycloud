from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent.operations import AgentBootstrap, AgentState
from agent_app.daemon import (
    TAILNET_ENROLLMENT_NOT_AWAITING_DETAIL,
    AgentDaemonOptions,
    AgentDaemonService,
    _recoverable_stream_error,
    build_agent_daemon_service,
)
from gateway.http import (
    RegisterAgentTailnetDeviceRequest,
    RegisterAgentTailnetDeviceResponse,
    RequestAgentTransportCredentialRequest,
    RequestAgentTransportCredentialResponse,
)
from networking.tailnet import TailnetStatus
from shared.http.errors import ErrorResponse, HttpApiError
from shared.routing import BackendRouteTransport


def _rejected(detail: str) -> HttpApiError:
    return HttpApiError(detail, status_code=400, error=ErrorResponse(detail=detail))


def _status(node_id: str, hostname: str) -> TailnetStatus:
    return TailnetStatus(
        backend_state="Running",
        self_node_id=node_id,
        self_host_name=hostname,
        self_online=True,
        tailnet_ips=["100.64.0.2"],
    )


@dataclass(slots=True)
class _AuthenticatedRuntime:
    """A daemon that is already logged in, as it is after a restart."""

    status_value: TailnetStatus = field(
        # A machine-scoped session, which is what a restart resumes. The agent
        # only rotates an identity that is not its own, so a name shaped like
        # something else would prove recovery the daemon never reached.
        default_factory=lambda: _status("node-old", "lazycloud-agent-machine-g1")
    )
    forced: list[str] = field(default_factory=list)

    def start(self) -> None: ...

    def status(self) -> TailnetStatus:
        return self.status_value

    def authenticate(
        self,
        *,
        auth_key: str,
        hostname: str,
        control_url: str = "",
        force: bool = False,
    ) -> TailnetStatus:
        del auth_key, control_url
        if not force:
            return self.status_value
        self.forced.append(hostname)
        self.status_value = _status("node-new", hostname)
        return self.status_value

    def close(self) -> None: ...

    def wait_for_peer(self, host: str, timeout_seconds: float) -> None:
        del host, timeout_seconds

    def resolve_peer_host(self, host: str) -> str:
        del host
        return ""


@dataclass(slots=True)
class _DivergedGateway:
    """Rejects the first registration the way a reset control plane does."""

    registrations: list[str] = field(default_factory=list)
    credentials_issued: int = 0

    def request_agent_transport_credential(
        self,
        request: RequestAgentTransportCredentialRequest,
    ) -> RequestAgentTransportCredentialResponse:
        del request
        self.credentials_issued += 1
        return RequestAgentTransportCredentialResponse(
            auth_key="tskey-test",
            hostname="lazycloud-agent-machine-g1",
            control_url="",
        )

    def register_agent_tailnet_device(
        self,
        request: RegisterAgentTailnetDeviceRequest,
    ) -> RegisterAgentTailnetDeviceResponse:
        self.registrations.append(request.node_id)
        if len(self.registrations) == 1:
            raise _rejected(TAILNET_ENROLLMENT_NOT_AWAITING_DETAIL)
        return RegisterAgentTailnetDeviceResponse(device_id="device-1", node_id=request.node_id)


def _service(tmp_path: Path, gateway: object, runtime: object) -> AgentDaemonService:
    return build_agent_daemon_service(
        AgentDaemonOptions(
            join_token="join",
            worker_image="worker:test",
            state_dir=str(tmp_path),
        ),
        client=gateway,  # type: ignore[arg-type]
        tailnet_runtime=runtime,  # type: ignore[arg-type]
    )


def _state() -> AgentState:
    return AgentState(
        gateway_url="http://gateway.invalid:9000",
        workspace_id="workspace",
        pool_name="default",
        machine_id="machine",
        agent_token="agent-token",
        credential_id="credential",
        credential_generation=1,
        bootstrap=AgentBootstrap(
            gateway_public_http_url="http://gateway.invalid:9000",
            transport=BackendRouteTransport.TsnetRestricted,
        ),
    )


def test_agent_reenrolls_when_the_control_plane_has_no_identity_for_its_session(
    tmp_path: Path,
) -> None:
    """A restart that outlives its enrollment record must recover on its own.

    The daemon's own auth state says nothing about whether the control plane
    still knows the device; when they diverge the agent used to reject every
    join for the life of the process.
    """
    gateway = _DivergedGateway()
    runtime = _AuthenticatedRuntime()

    _, advertise_host = _service(tmp_path, gateway, runtime)._start_tailnet(_state())

    assert runtime.forced == ["lazycloud-agent-machine-g1"]
    assert gateway.credentials_issued == 1
    assert gateway.registrations == ["node-old", "node-new"]
    assert advertise_host


@dataclass(slots=True)
class _EnrollingGateway:
    """Accepts the identity it just issued, as a fresh enrollment does."""

    registrations: list[str] = field(default_factory=list)
    credentials_issued: int = 0

    def request_agent_transport_credential(
        self,
        request: RequestAgentTransportCredentialRequest,
    ) -> RequestAgentTransportCredentialResponse:
        del request
        self.credentials_issued += 1
        return RequestAgentTransportCredentialResponse(
            auth_key="tskey-test",
            hostname="lazycloud-agent-machine-g1",
            control_url="",
        )

    def register_agent_tailnet_device(
        self,
        request: RegisterAgentTailnetDeviceRequest,
    ) -> RegisterAgentTailnetDeviceResponse:
        self.registrations.append(request.node_id)
        return RegisterAgentTailnetDeviceResponse(device_id="device-1", node_id=request.node_id)


def test_a_node_that_booted_on_the_pool_key_trades_it_for_its_own_identity(
    tmp_path: Path,
) -> None:
    """The bootstrap identity must not be what keeps a node on the tailnet.

    A managed-pool node joins from user-data with a key shared by every
    instance its launch template starts, tagged to reach the control plane and
    nothing else. Left in place it would satisfy the agent's own liveness check
    while denying the control plane the route-proxy hop that makes the machine
    usable, and the pool-scoped key would be the credential holding a running
    machine on the network.
    """
    gateway = _EnrollingGateway()
    runtime = _AuthenticatedRuntime(
        status_value=_status("node-bootstrap", "bootstrap-i-0123456789abcdef0")
    )

    _, advertise_host = _service(tmp_path, gateway, runtime)._start_tailnet(_state(), runtime)

    assert runtime.forced == ["lazycloud-agent-machine-g1"]
    assert gateway.credentials_issued == 1
    # Registered once, under the machine identity — never under the pool's.
    assert gateway.registrations == ["node-new"]
    assert advertise_host


def test_a_diverged_enrollment_does_not_end_the_agent_process() -> None:
    assert _recoverable_stream_error(_rejected(TAILNET_ENROLLMENT_NOT_AWAITING_DETAIL))
    assert not _recoverable_stream_error(_rejected("invalid agent token"))
