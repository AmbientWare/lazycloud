from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue, TypeAdapter
from shared.contracts import ContractModel

from agent.operations import (
    AGENT_AUTHORITY_REVOKED_FILE,
    AGENT_RUNTIME_READY_FILE,
    AgentAuthorityRevoked,
    AgentRuntimeReady,
    AgentState,
    agent_state_matches_gateway,
    agent_state_payload,
)

AGENT_STATE_FILE = "agent-state.json"
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


@dataclass(slots=True)
class AgentStateStore:
    state_dir: Path
    filename: str = AGENT_STATE_FILE

    @property
    def path(self) -> Path:
        return self.state_dir / self.filename

    @property
    def ready_path(self) -> Path:
        return self.state_dir / AGENT_RUNTIME_READY_FILE

    def load(self, gateway_url: str) -> AgentState | None:
        if not self.path.exists():
            return None
        state = AgentState.from_saved_json(self.path.read_text(encoding="utf-8"))
        if agent_state_matches_gateway(state, gateway_url):
            return state
        return None

    def save(self, state: AgentState) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        payload = _JSON_VALUE_ADAPTER.validate_python(agent_state_payload(state))
        write_json_atomic(self.path, payload, permissions=0o600)

    @property
    def revoked_path(self) -> Path:
        return self.state_dir / AGENT_AUTHORITY_REVOKED_FILE

    def begin_run(self) -> None:
        self.ready_path.unlink(missing_ok=True)

    def mark_authority_revoked(self, state: AgentState) -> None:
        """Remove the saved identity so a restart cannot reuse revoked authority."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        marker = AgentAuthorityRevoked(machine_id=state.machine_id)
        write_json_atomic(self.revoked_path, model_payload(marker), permissions=0o600)
        self.path.unlink(missing_ok=True)

    def authority_revoked(self) -> AgentAuthorityRevoked | None:
        if not self.revoked_path.exists():
            return None
        return AgentAuthorityRevoked.model_validate_json(
            self.revoked_path.read_text(encoding="utf-8")
        )

    def mark_ready(self, state: AgentState, *, stream_iteration: int) -> None:
        marker = AgentRuntimeReady(
            machine_id=state.machine_id,
            stream_iteration=stream_iteration,
        )
        write_json_atomic(
            self.ready_path,
            model_payload(marker),
            permissions=0o600,
        )


def model_payload(model: ContractModel) -> dict[str, JsonValue]:
    return _JSON_OBJECT_ADAPTER.validate_json(model.model_dump_json())


def write_json_atomic(path: Path, payload: JsonValue, *, permissions: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.chmod(permissions)
    os.replace(tmp_path, path)
