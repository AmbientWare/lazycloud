from __future__ import annotations

from pathlib import Path

import pytest
from agent.operations import AgentBootstrap, AgentState
from agent_app.daemon import (
    AgentStateStore,
    AgentStreamRetryableError,
    _agent_state_from_stream_response,
    _recoverable_stream_error,
)
from compute.agent_control import AgentBootstrapConfig
from gateway.http import StreamAgentResponse
from shared.compute_policy import MachinePool
from shared.http.errors import HttpApiError, HttpTransportError


def test_agent_refreshes_historical_runtime_without_replacing_identity(tmp_path: Path) -> None:
    state_store = AgentStateStore(tmp_path)
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
            gateway_runtime_http_url="http://100.96.0.2:9000",
        ),
    )
    state_store.save(state)
    loaded = state_store.load(state.gateway_url)
    assert loaded is not None
    response = StreamAgentResponse(
        generation=2,
        credential_id=state.credential_id,
        credential_generation=state.credential_generation,
    )
    with pytest.raises(AgentStreamRetryableError, match="requires current runtime bootstrap"):
        _agent_state_from_stream_response(loaded, response)
    assert state_store.load(state.gateway_url) == state

    response.bootstrap = AgentBootstrapConfig(
        gateway_public_http_url=state.gateway_url,
        gateway_runtime_http_url="http://100.96.0.1:9000",
        workspace_id=state.workspace_id,
        pool=state.pool,
    )
    refreshed = _agent_state_from_stream_response(loaded, response)
    state_store.save(refreshed)
    saved = state_store.load(state.gateway_url)
    assert saved is not None
    assert saved.bootstrap.gateway_runtime_http_url == "http://100.96.0.1:9000"
    assert saved.machine_id == state.machine_id
    assert saved.agent_token == state.agent_token
    assert saved.credential_id == state.credential_id
    assert saved.credential_generation == state.credential_generation

    response.bootstrap.gateway_runtime_http_url = state.bootstrap.gateway_runtime_http_url
    with pytest.raises(ValueError, match="WireGuard runtime service"):
        _agent_state_from_stream_response(saved, response)
    assert state_store.load(state.gateway_url) == saved


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
