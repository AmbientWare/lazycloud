from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from lazycloud.cli.main import build_public_cli
from shared.http.aws_connections import (
    AwsComputeConfigurationUpdateRequest,
    AwsConnectionResponse,
)
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


def _connection() -> AwsConnectionResponse:
    return AwsConnectionResponse.model_validate(
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "account_id": "123456789012",
            "pool": "aws",
            "phase": "ready",
            "revision": 7,
            "compute": {
                "revision": 4,
                "default_region": "us-east-1",
                "max_cpu_instances": 10,
                "max_gpu_instances": 2,
                "allowed_regions": ["us-east-1"],
                "allowed_instance_types": [],
                "idle_timeout_seconds": 300,
                "root_volume_gib": 200,
            },
            "hosts_workloads": True,
            "can_manage_existing_capacity": True,
            "available_actions": ["reconnect", "remove"],
            "detail": "AWS compute is available for your workspaces.",
            "customer_action": None,
            "next_retry_at": None,
            "active_authorization": None,
            "pending_authorization": None,
            "retiring_authorization": None,
            "created_at": "2026-07-15T12:00:00Z",
            "updated_at": "2026-07-15T12:00:00Z",
        }
    )


@dataclass(slots=True)
class _ComputeClient:
    updates: list[WorkspaceComputePolicyUpdateRequest] = field(default_factory=list)
    compute_updates: list[AwsComputeConfigurationUpdateRequest] = field(default_factory=list)

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

    def current_connection(self) -> AwsConnectionResponse:
        return _connection()

    def update_compute_configuration(
        self,
        request: AwsComputeConfigurationUpdateRequest,
    ) -> AwsConnectionResponse:
        self.compute_updates.append(request)
        return _connection().model_copy(
            update={
                "compute": request.compute.model_copy(
                    update={"revision": request.expected_revision + 1}
                )
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


def test_cloud_compute_update_revises_the_account_configuration_it_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Options given change; options omitted keep what the account already carries.

    The command builds the whole configuration from what it just read, so a
    partial edit that dropped an untouched field would silently reset it, and a
    stale revision would overwrite a concurrent edit instead of being refused.
    """
    client = _ComputeClient()

    def fake_compute_client(**_kwargs: object) -> _ComputeClient:
        return client

    monkeypatch.setattr("lazycloud.cli.resources.compute_client", fake_compute_client)

    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "cloud",
            "compute",
            "update",
            "--default-region",
            "us-west-2",
            "--default-instance-type",
            "g6.xlarge",
            "--initial-cpu-workers",
            "2",
            "--min-cpu-workers",
            "0",
            "--allowed-region",
            "us-west-2",
            "--allowed-instance-type",
            "g6.xlarge",
            "--max-cpu",
            "6",
            "--max-gpu",
            "1",
            "--min-free-cpu-millicores",
            "0",
            "--min-free-memory-mib",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(client.compute_updates) == 1
    request = client.compute_updates[0]
    assert request.expected_revision == 4
    assert request.compute.default_region == "us-west-2"
    assert request.compute.default_instance_type == "g6.xlarge"
    assert request.compute.initial_cpu_workers == 2
    assert request.compute.min_cpu_workers == 0
    assert request.compute.allowed_regions == ("us-west-2",)
    assert request.compute.allowed_instance_types == ("g6.xlarge",)
    assert request.compute.max_cpu_instances == 6
    assert request.compute.max_gpu_instances == 1
    assert request.compute.min_free_cpu_millicores == 0
    assert request.compute.min_free_memory_mib == 0
    # Untouched by the command line, so carried forward from what it read.
    assert request.compute.idle_timeout_seconds == 300
    assert request.compute.root_volume_gib == 200
