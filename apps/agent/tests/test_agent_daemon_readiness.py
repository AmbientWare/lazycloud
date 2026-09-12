from __future__ import annotations

import pytest
from agent.operations import AgentBootstrap, AgentState
from agent_app.daemon import (
    AgentStreamRetryableError,
    _agent_state_from_stream_response,
    _recoverable_stream_error,
)
from gateway.http import StreamAgentResponse
from shared.compute_policy import MachinePool
from shared.http.errors import HttpApiError, HttpTransportError


def test_stale_release_cannot_apply_worker_instructions() -> None:
    state = AgentState(
        gateway_url="https://control.example.com",
        workspace_id="workspace-one",
        pool=MachinePool("pool-one"),
        machine_id="machine-one",
        agent_token="agent-secret",
        credential_id="credential-one",
        credential_generation=1,
        release_generation=2,
        bootstrap=AgentBootstrap(
            gateway_public_http_url="https://control.example.com",
            gateway_runtime_http_url="http://100.96.0.1:9000",
        ),
    )
    response = StreamAgentResponse(
        generation=1,
        credential_id=state.credential_id,
        credential_generation=state.credential_generation,
    )

    with pytest.raises(AgentStreamRetryableError, match="instruction is stale"):
        _agent_state_from_stream_response(state, response)

    assert state.release_generation == 2


def test_transport_failures_are_recoverable_so_a_machine_keeps_rejoining() -> None:
    transport_failure = HttpTransportError(
        "POST",
        "https://gateway.example.com/gateway/agents/private-network/register",
        "EOF occurred in violation of protocol (_ssl.c:1010)",
    )
    assert _recoverable_stream_error(transport_failure)
    assert _recoverable_stream_error(HttpApiError("upstream", status_code=503))
    assert not _recoverable_stream_error(HttpApiError("forbidden", status_code=403))
