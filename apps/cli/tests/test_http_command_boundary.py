from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from cli.api_client import AdminApiClient
from cli.main import build_admin_cli
from pydantic import JsonValue
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.http.apps import AppResponse
from shared.http.compute import ContainerWithAppPageResponse, PoolCreateRequest, PoolResponse
from shared.http.system import AuthTokenResponse
from shared.http_transport import HttpChannel
from typer.testing import CliRunner

from cli import identity, resources

cli = build_admin_cli()


@dataclass
class _RecordingHttpChannel(HttpChannel):
    requests: list[str] = field(default_factory=list)
    posts: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)

    def get(self, path: str) -> JsonValue:
        self.requests.append(path)
        return ContainerWithAppPageResponse().model_dump(mode="json")

    def post(
        self,
        path: str,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> JsonValue:
        self.posts.append((path, dict(payload or {})))
        if "/api/v1/tokens/" in path:
            return AuthTokenResponse(
                id="provider-token",
                name="provider-acceptance-certification",
                prefix="lzc_prefix",
                workspace_id="workspace-provider-acceptance",
                created_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
            ).model_dump(mode="json")
        return PoolResponse(
            capacity_owner_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            capacity_owner_kind=CapacityOwnerKind.PooledProvider,
            capacity_owner_source=CapacityOwnerSource.Provider,
            name="gpu-pool",
            created_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        ).model_dump(mode="json")


@dataclass
class _CanonicalAppClient:
    calls: list[str] = field(default_factory=list)

    def app(self, app_id: str) -> AppResponse:
        self.calls.append(app_id)
        return AppResponse(
            id=app_id,
            workspace_id="workspace-a",
            name="app-one",
            created_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
            updated_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        )


def test_operator_pool_create_sends_explicit_capacity_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _RecordingHttpChannel()
    client = AdminApiClient(channel=channel, workspace="workspace-a")

    def resolve_client(workspace: str | None = None) -> AdminApiClient:
        del workspace
        return client

    monkeypatch.setattr(resources, "admin_api_client", resolve_client)

    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "pool",
            "create",
            "gpu-pool",
            "--provider",
            "kubernetes",
            "--initial-workers",
            "1",
            "--max-workers",
            "4",
            "--scaling-enabled",
            "--default-eligible",
            "--worker-cpu-millicores",
            "8000",
            "--worker-memory-mib",
            "32768",
            "--worker-gpu-type",
            "L4",
            "--worker-gpu-count",
            "1",
            "--worker-runtime",
            "runc",
            "--idle-drain-timeout",
            "900",
            "--registration-timeout",
            "240",
        ],
    )

    assert result.exit_code == 0, result.output
    request = PoolCreateRequest.model_validate(channel.posts[0][1])
    assert request.initial_workers == 1
    assert request.max_workers == 4
    assert request.scaling_enabled
    assert request.default_eligible
    assert request.worker_cpu_millicores == 8_000
    assert request.worker_memory_mib == 32_768
    assert request.worker_gpu_type == "L4"
    assert request.worker_gpu_count == 1
    assert request.worker_runtimes == ("runc",)
    assert request.idle_drain_timeout_seconds == 900
    assert request.registration_timeout_seconds == 240


def test_operator_token_revoke_targets_explicit_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _RecordingHttpChannel()
    selected_workspaces: list[str | None] = []

    def resolve_client(workspace: str | None = None) -> AdminApiClient:
        selected_workspaces.append(workspace)
        return AdminApiClient(channel=channel, workspace=workspace or "default")

    monkeypatch.setattr(identity, "admin_api_client", resolve_client)

    result = CliRunner().invoke(
        cli,
        [
            "--json",
            "token",
            "revoke",
            "provider-token",
            "--workspace",
            "provider-acceptance",
        ],
    )

    assert result.exit_code == 0, result.output
    assert selected_workspaces == ["provider-acceptance"]
    assert channel.posts == [
        (
            "/api/v1/tokens/provider-token/revoke?workspace=provider-acceptance",
            {},
        )
    ]
