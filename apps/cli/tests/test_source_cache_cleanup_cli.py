from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from cli.api_client import AdminApiClient
from cli.main import build_admin_cli
from lazycloud.cli.main import build_public_cli
from pydantic import JsonValue
from shared.http.source_cache_cleanup import SourceCacheCleanupStatusResponse
from shared.http_transport import HttpChannel
from typer.testing import CliRunner

from cli import control_plane


@dataclass
class _RecordingStatusChannel(HttpChannel):
    requests: list[str] = field(default_factory=list)

    def get(self, path: str) -> JsonValue:
        self.requests.append(path)
        return SourceCacheCleanupStatusResponse(
            workspace_id="workspace-a",
            pending_count=3,
            claimed_count=1,
            completed_count=7,
            generations_pending=2,
            oldest_pending_age_seconds=41,
            complete=False,
        ).model_dump(mode="json")


def test_workspace_cleanup_status_uses_typed_admin_api_and_clean_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _RecordingStatusChannel()
    client = AdminApiClient(channel=channel, workspace="default")

    def resolve_client(workspace: str | None = None) -> AdminApiClient:
        assert workspace is None
        return client

    monkeypatch.setattr(control_plane, "admin_api_client", resolve_client)

    result = CliRunner().invoke(
        build_admin_cli(),
        ["--json", "workspace", "cleanup-status", "team/a"],
    )

    assert result.exit_code == 0, result.output
    parsed = SourceCacheCleanupStatusResponse.model_validate_json(result.stdout)
    assert parsed.oldest_pending_age_seconds == 41
    assert channel.requests == [
        "/api/v1/workspaces/team%2Fa/source-cache-cleanup",
    ]


def test_workspace_cleanup_status_is_operator_only() -> None:
    result = CliRunner().invoke(
        build_public_cli(),
        ["workspace", "cleanup-status", "default"],
    )

    assert result.exit_code != 0
    assert "No such command" in result.output
