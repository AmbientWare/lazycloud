from __future__ import annotations

from datetime import UTC, datetime, timedelta

from compute.telemetry import (
    AgentDisconnectAction,
    AgentTelemetryDecisionKind,
    AgentTelemetryState,
    TelemetryCredentialKind,
    TelemetryCredentialOperation,
    TelemetryRootStreamConfig,
    TelemetrySinkStatus,
    agent_machine_connected,
    build_scoped_telemetry_config,
    node_usage_seconds,
    plan_agent_disconnect,
    plan_scoped_telemetry_credentials,
    redact_telemetry_line,
    validate_agent_telemetry_token,
)
from shared.compute_policy import MachinePool


def test_scoped_telemetry_credentials_are_append_only_and_workspace_scoped() -> None:
    disabled = plan_scoped_telemetry_credentials(
        TelemetryRootStreamConfig(api_key="", basin="events-basin"),
        "workspace/one",
    )
    assert disabled.status is TelemetrySinkStatus.Disabled
    assert not disabled.config.enabled

    plan = plan_scoped_telemetry_credentials(
        TelemetryRootStreamConfig(
            api_key="root-token",
            basin="events-basin",
            stream_prefix="/custom/",
        ),
        "workspace/one",
        suffixes={
            TelemetryCredentialKind.Logs: b"abcdef",
            TelemetryCredentialKind.Events: b"ghijkl",
        },
    )

    assert plan.status is TelemetrySinkStatus.Planned
    assert [item.kind for item in plan.issue_plans] == [
        TelemetryCredentialKind.Logs,
        TelemetryCredentialKind.Events,
    ]
    for item in plan.issue_plans:
        assert item.scope.basin_exact == "events-basin"
        assert item.scope.operations == (TelemetryCredentialOperation.Append,)

    logs, events = plan.issue_plans
    assert logs.scope.stream_prefix == "custom/logs/workspaces/workspace_one"
    assert events.scope.stream_prefix == "custom/workspaces/workspace_one"

    ready = build_scoped_telemetry_config(
        plan,
        {
            TelemetryCredentialKind.Logs: "log-token",
            TelemetryCredentialKind.Events: "event-token",
        },
    )
    assert ready.status is TelemetrySinkStatus.Ready
    assert ready.config.enabled
    assert ready.config.logs is not None
    assert ready.config.logs.credential == "log-token"
    assert ready.config.events is not None
    assert ready.config.events.stream_prefix == "custom/workspaces/workspace_one"


def test_telemetry_redaction_covers_auth_phrases_json_and_credentials() -> None:
    line = (
        "access_key=access-value secretKey:secret-value Authorization: Bearer auth-value "
        'password="password-value" API key phrase-value '
        '{"AZURE_CLIENT_SECRET":"azure-value","apiKey":"api-value",'
        '"logCredential":"log-credential","eventCredential":"event-credential",'
        '"credentials":"credential-value"} '
        "Bearer loose-token normal=value"
    )

    redacted = redact_telemetry_line(line)

    for leaked in {
        "access-value",
        "secret-value",
        "auth-value",
        "password-value",
        "phrase-value",
        "azure-value",
        "api-value",
        "log-credential",
        "event-credential",
        "credential-value",
        "loose-token",
    }:
        assert leaked not in redacted
    assert "normal=value" in redacted


def test_agent_telemetry_token_decisions_cover_invalid_changed_and_accepted() -> None:
    invalid = validate_agent_telemetry_token("", state_found=True)
    missing = validate_agent_telemetry_token("token", state_found=False)
    changed = validate_agent_telemetry_token("new", current_token="old", state_found=True)
    accepted = validate_agent_telemetry_token(" token ", current_token=None)

    assert invalid.kind is AgentTelemetryDecisionKind.InvalidAgentToken
    assert not missing.accepted
    assert changed.reason == "agent token changed on telemetry stream"
    assert accepted.accepted
    assert accepted.agent_token == "token"


def test_agent_liveness_and_disconnect_decisions_match_heartbeat_rules() -> None:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    fresh = AgentTelemetryState(
        workspace_id="workspace",
        pool=MachinePool("pool"),
        machine_id="machine",
        last_heartbeat_at=now - timedelta(seconds=10),
    )
    stale = fresh.model_copy(update={"last_heartbeat_at": now - timedelta(seconds=90)})
    repeated = stale.model_copy(update={"last_disconnect_at": now - timedelta(seconds=30)})
    future_ok = fresh.model_copy(update={"last_heartbeat_at": now + timedelta(seconds=3)})
    future_bad = fresh.model_copy(update={"last_heartbeat_at": now + timedelta(seconds=30)})

    assert node_usage_seconds(now - timedelta(seconds=5), now) == 5
    assert node_usage_seconds(now - timedelta(seconds=90), now) == 60
    assert agent_machine_connected(fresh, now=now)
    assert agent_machine_connected(future_ok, now=now)
    assert not agent_machine_connected(future_bad, now=now)

    ignore = plan_agent_disconnect(fresh, now=now)
    mark = plan_agent_disconnect(stale, now=now)
    repeat = plan_agent_disconnect(repeated, now=now)

    assert ignore.action is AgentDisconnectAction.Ignore
    assert mark.action is AgentDisconnectAction.MarkDisconnected
    assert mark.disconnected_at == now
    assert repeat.action is AgentDisconnectAction.AlreadyMarked
