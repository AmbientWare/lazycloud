from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from lazycloud.cli.main import build_public_cli
from shared.http.compute_policy import WorkspaceComputeSummaryResponse
from typer.testing import CliRunner

cli = build_public_cli()


@dataclass(slots=True)
class _ComputeClient:
    def summary(self) -> WorkspaceComputeSummaryResponse:
        return WorkspaceComputeSummaryResponse.model_validate(
            {
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
