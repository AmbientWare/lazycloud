from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from lazycloud.cli.main import build_public_cli
from shared.http.compute_policy import (
    WorkspaceComputePolicyResponse,
    WorkspaceComputePolicyUpdateRequest,
    WorkspaceComputeSummaryResponse,
)
from typer.testing import CliRunner

cli = build_public_cli()


def _policy() -> WorkspaceComputePolicyResponse:
    return WorkspaceComputePolicyResponse.model_validate(
        {
            "revision": 4,
            "default_pool": "lazycloud",
            "created_at": "2026-07-15T12:00:00Z",
            "updated_at": "2026-07-15T12:00:00Z",
        }
    )


@dataclass(slots=True)
class _ComputeClient:
    updates: list[WorkspaceComputePolicyUpdateRequest] = field(default_factory=list)

    def summary(self) -> WorkspaceComputeSummaryResponse:
        return WorkspaceComputeSummaryResponse.model_validate(
            {
                "policy": _policy().model_dump(mode="json"),
                "connection": {"account_id": "123456789012", "phase": "ready"},
                "instances": {"total": 2, "ready": 1, "pending": 1, "degraded": 0},
                "cost": {
                    "hourly_micros": 340000,
                    "daily_micros": 8160000,
                    "currency": "USD",
                    "estimated": True,
                },
                "workload_count": 3,
            }
        )

    def policy(self) -> WorkspaceComputePolicyResponse:
        return _policy()

    def update_policy(
        self,
        request: WorkspaceComputePolicyUpdateRequest,
    ) -> WorkspaceComputePolicyResponse:
        self.updates.append(request)
        return _policy().model_copy(
            update={
                "revision": request.expected_revision + 1,
                "default_pool": request.default_pool,
            }
        )


def test_compute_status_routes_workspace_and_preserves_json_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ComputeClient()
    workspaces: list[str | None] = []

    def compute_client(*, workspace: str | None = None) -> _ComputeClient:
        workspaces.append(workspace)
        return client

    monkeypatch.setattr("lazycloud.cli.resources.compute_client", compute_client)

    result = CliRunner().invoke(
        cli,
        ["--json", "compute", "status", "--workspace", "platform"],
    )

    assert result.exit_code == 0, result.output
    assert workspaces == ["platform"]
    payload = json.loads(result.stdout)
    assert payload["connection"]["account_id"] == "123456789012"
    assert payload["instances"] == {"degraded": 0, "pending": 1, "ready": 1, "total": 2}
    assert payload["workload_count"] == 3
