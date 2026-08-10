from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from lazycloud.cli.main import build_public_cli
from pydantic import JsonValue
from shared.http.aws_connections import (
    AwsConnectionAuthorization,
    AwsConnectionAuthorizationResponse,
    AwsConnectionResponse,
)
from typer.testing import CliRunner

cli = build_public_cli()


def _connection(
    *,
    phase: str,
    validation_error: bool = False,
    customer_action: bool = False,
) -> AwsConnectionResponse:
    authorization: dict[str, JsonValue] = {
        "generation": 1,
        "authorization_mode": "managed_stack",
        "phase": "degraded" if validation_error else "ready",
        "last_validation_started_at": "2026-07-15T12:00:00Z",
        "last_validated_at": None if validation_error else "2026-07-15T12:00:01Z",
        "error_code": "assume_role_denied" if validation_error else None,
        "created_at": "2026-07-15T12:00:00Z",
        "updated_at": "2026-07-15T12:00:01Z",
    }
    return AwsConnectionResponse.model_validate(
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "account_id": "123456789012",
            "phase": phase,
            "revision": 2,
            "compute": {},
            "hosts_workloads": phase == "ready",
            "can_manage_existing_capacity": phase == "ready",
            "available_actions": ["retry"] if phase == "action_required" else [],
            "detail": (
                "Automatic AWS cleanup needs attention before removal can finish."
                if phase == "action_required"
                else "Checking AWS authorization."
                if validation_error
                else "Removing AWS compute."
            ),
            "customer_action": (
                {
                    "url": "https://console.aws.amazon.com/cloudformation/final",
                    "label": "Review cleanup in AWS",
                }
                if customer_action
                else None
            ),
            "next_retry_at": None,
            "active_authorization": None if validation_error else authorization,
            "pending_authorization": authorization if validation_error else None,
            "retiring_authorization": None,
            "created_at": "2026-07-15T12:00:00Z",
            "updated_at": "2026-07-15T12:00:01Z",
        }
    )


@dataclass(slots=True)
class _ValidationClient:
    def validate_connection(self) -> AwsConnectionResponse:
        return _connection(phase="degraded", validation_error=True)


@dataclass(slots=True)
class _DisconnectedClient:
    def current_connection(self) -> AwsConnectionResponse | None:
        return None


@dataclass(slots=True)
class _RemovalClient:
    observations: list[AwsConnectionResponse | None] = field(
        default_factory=lambda: [_connection(phase="ready"), None]
    )
    remove_count: int = 0

    def current_connection(self) -> AwsConnectionResponse | None:
        return self.observations.pop(0)

    def remove_account(self) -> AwsConnectionResponse:
        self.remove_count += 1
        return _connection(phase="disconnect_draining")


@dataclass(slots=True)
class _RecoveryClient:
    def current_connection(self) -> AwsConnectionResponse:
        return _connection(phase="ready")

    def remove_account(self) -> AwsConnectionResponse:
        return _connection(phase="action_required", customer_action=True)


def test_cloud_status_reports_disconnected_as_valid_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_compute_client(**_kwargs: object) -> _DisconnectedClient:
        return _DisconnectedClient()

    monkeypatch.setattr(
        "lazycloud.cli.resources.compute_client",
        fake_compute_client,
    )

    result = CliRunner().invoke(cli, ["--json", "cloud", "status"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"connection": None}


def test_cloud_validate_json_is_clean_and_exits_nonzero_for_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_compute_client(**_kwargs: object) -> _ValidationClient:
        return _ValidationClient()

    monkeypatch.setattr(
        "lazycloud.cli.resources.compute_client",
        fake_compute_client,
    )

    result = CliRunner().invoke(cli, ["--json", "cloud", "validate"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["pending_authorization"]["error_code"] == "assume_role_denied"
    assert "Validation failed" not in result.stdout


def test_cloud_disconnect_waits_for_automatic_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RemovalClient()

    def fake_compute_client(**_kwargs: object) -> _RemovalClient:
        return client

    def skip_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("lazycloud.cli.resources.compute_client", fake_compute_client)
    monkeypatch.setattr("lazycloud.cli.resources.time.sleep", skip_sleep)

    result = CliRunner().invoke(
        cli,
        ["cloud", "disconnect", "--wait", "--interval", "0.2"],
    )

    assert result.exit_code == 0, result.output
    assert client.remove_count == 1
    assert "disconnect_draining" in result.stdout
    assert "removed" in result.stdout


def test_cloud_disconnect_opens_only_terminal_recovery_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []

    def fake_compute_client(**_kwargs: object) -> _RecoveryClient:
        return _RecoveryClient()

    monkeypatch.setattr(
        "lazycloud.cli.resources.compute_client",
        fake_compute_client,
    )
    monkeypatch.setattr("lazycloud.cli.resources.webbrowser.open", opened.append)

    result = CliRunner().invoke(cli, ["cloud", "disconnect", "--open"])

    assert result.exit_code == 0, result.output
    assert "action_required" in result.stdout
    assert "Review cleanup in AWS" in result.stdout
    assert opened == ["https://console.aws.amazon.com/cloudformation/final"]


@dataclass(slots=True)
class _ConnectClient:
    requests: list[tuple[str, str | None]] = field(default_factory=list)

    def connect_account(
        self,
        *,
        account_id: str,
        role_arn: str | None = None,
    ) -> AwsConnectionAuthorizationResponse:
        self.requests.append((account_id, role_arn))
        return AwsConnectionAuthorizationResponse(
            connection=_connection(phase="awaiting_authorization"),
            authorization=AwsConnectionAuthorization(
                url="https://console.aws.amazon.com/cloudformation/create"
            ),
        )


def test_cloud_connect_requires_a_provider_subcommand_and_account_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ConnectClient()

    def fake_compute_client(**_kwargs: object) -> _ConnectClient:
        return client

    monkeypatch.setattr("lazycloud.cli.resources.compute_client", fake_compute_client)

    bare = CliRunner().invoke(cli, ["cloud", "connect"])
    assert bare.exit_code != 0
    assert client.requests == []

    missing_account = CliRunner().invoke(cli, ["cloud", "connect", "aws"])
    assert missing_account.exit_code != 0
    assert client.requests == []

    connected = CliRunner().invoke(
        cli,
        [
            "cloud",
            "connect",
            "aws",
            "--account-id",
            "123456789012",
            "--role-arn",
            "arn:aws:iam::123456789012:role/platform-management",
        ],
    )
    assert connected.exit_code == 0, connected.output
    assert client.requests == [
        ("123456789012", "arn:aws:iam::123456789012:role/platform-management")
    ]
    assert "run `cloud validate`" in connected.stdout
