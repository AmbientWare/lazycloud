from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from lazycloud.cli.main import build_public_cli
from shared.http.compute_policy import (
    WorkspaceComputePolicyPatchRequest,
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
            "default_placement": "managed",
            "aws": {
                "default_region": "us-east-1",
                "max_cpu_instances": 10,
                "max_gpu_instances": 2,
                "allowed_regions": ["us-east-1"],
                "allowed_instance_types": [],
                "idle_timeout_seconds": 300,
                "root_volume_gib": 200,
            },
            "created_at": "2026-07-15T12:00:00Z",
            "updated_at": "2026-07-15T12:00:00Z",
        }
    )


@dataclass(slots=True)
class _ComputeClient:
    updates: list[WorkspaceComputePolicyUpdateRequest] = field(default_factory=list)
    patches: list[WorkspaceComputePolicyPatchRequest] = field(default_factory=list)

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
                "default_placement": request.default_placement,
                "aws": request.aws,
            }
        )

    def patch_policy(
        self,
        request: WorkspaceComputePolicyPatchRequest,
    ) -> WorkspaceComputePolicyResponse:
        self.patches.append(request)
        base = _policy()
        changed = request.aws.model_dump(exclude_none=True)
        return base.model_copy(
            update={
                "revision": request.expected_revision + 1,
                "default_placement": request.default_placement or base.default_placement,
                "aws": base.aws.model_copy(update=changed),
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


def test_compute_policy_update_sends_revisioned_guardrails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ComputeClient()

    def fake_compute_client(**_kwargs: object) -> _ComputeClient:
        return client

    monkeypatch.setattr("lazycloud.cli.resources.compute_client", fake_compute_client)

    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "compute",
            "policy",
            "update",
            "--default-placement",
            "aws",
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
            "--idle-timeout",
            "600",
            "--root-volume-gib",
            "250",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(client.patches) == 1
    request = client.patches[0]
    assert request.expected_revision == 4
    assert request.default_placement is not None
    assert request.default_placement.value == "aws"
    assert request.aws.default_region == "us-west-2"
    assert request.aws.default_instance_type == "g6.xlarge"
    assert request.aws.initial_cpu_workers == 2
    assert request.aws.min_cpu_workers == 0
    assert request.aws.allowed_regions == ("us-west-2",)
    assert request.aws.allowed_instance_types == ("g6.xlarge",)
    assert request.aws.max_cpu_instances == 6
    assert request.aws.max_gpu_instances == 1
    assert request.aws.min_free_cpu_millicores == 0
    assert request.aws.min_free_memory_mib == 0
    assert request.aws.idle_timeout_seconds == 600
    assert request.aws.root_volume_gib == 250
