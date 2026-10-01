"""`lazycloud cloud` on the public API's AWS connection operations."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from lazycloud._terminal.streams import console
from lazycloud.cli.main import build_public_cli
from typer.testing import CliRunner

from tests.api_server import ApiRequest, FakeApi, Reply, error_reply, json_reply

NOW = "2026-09-30T12:00:00Z"
ACCOUNT = "123456789012"
ROLE = "arn:aws:iam::123456789012:role/platform-management"
EXTERNAL_ID = "customer-test-external-id-0123456789abcdef"

cli = build_public_cli()


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console, "width", 200)


def _no_sleep(_seconds: float) -> None:
    return None


def _authorization(**extra: object) -> dict[str, object]:
    return {
        "generation": 1,
        "authorization_mode": "managed_stack",
        "phase": "ready",
        "created_at": NOW,
        "updated_at": NOW,
        **extra,
    }


def _stack() -> dict[str, object]:
    return {
        "account_id": ACCOUNT,
        "region": "us-east-2",
        "template_sha256": "a" * 64,
        "request": {
            "StackName": "lazycloud-connection",
            "TemplateBody": '{"Resources": {}}',
            "Parameters": [{"ParameterKey": "ExternalId", "ParameterValue": EXTERNAL_ID}],
            "Capabilities": ["CAPABILITY_NAMED_IAM"],
            "OnFailure": "DELETE",
        },
    }


def _connection(phase: str, **extra: object) -> dict[str, object]:
    return {
        "id": "0192f0a0-0000-7000-8000-0000000000c1",
        "account_id": ACCOUNT,
        "phase": phase,
        "revision": 2,
        "hosts_workloads": phase == "ready",
        "can_manage_existing_capacity": phase == "ready",
        "available_actions": [],
        "detail": "",
        "created_at": NOW,
        "updated_at": NOW,
        **extra,
    }


class _Connection:
    """The AWS connection routes over one mutable connection."""

    def __init__(self, api: FakeApi, current: dict[str, object] | None) -> None:
        self.current = current
        # What the next GETs return, in order; then `current` again.
        self.upcoming: list[dict[str, object] | None] = []
        api.route("GET", "/v1/aws-connection")(self.get)
        api.route("DELETE", "/v1/aws-connection")(self.delete)

    def get(self, _: ApiRequest) -> Reply:
        if self.upcoming:
            self.current = self.upcoming.pop(0)
        return json_reply({"connection": self.current})

    def delete(self, _: ApiRequest) -> Reply:
        if self.current is None:
            return error_reply("not_found", "no AWS connection", 404)
        self.current = _connection(
            "disconnect_draining", detail="Removing AWS compute.", available_actions=["retry"]
        )
        return json_reply({"connection": self.current}, 202)


def test_cloud_connect_returns_the_authorization_to_complete(fake_api: FakeApi) -> None:
    @fake_api.route("POST", "/v1/aws-connection")
    def connect(request: ApiRequest) -> Reply:
        body = request.json()
        authorization = {"external_id": EXTERNAL_ID} if "role_arn" in body else {"stack": _stack()}
        return json_reply(
            {
                "connection": _connection("awaiting_authorization"),
                "authorization": authorization,
            },
            201,
        )

    bare = CliRunner().invoke(cli, ["cloud", "connect"])
    assert bare.exit_code != 0
    assert CliRunner().invoke(cli, ["cloud", "connect", "aws"]).exit_code != 0
    assert not fake_api.requests

    managed = CliRunner().invoke(cli, ["cloud", "connect", "aws", "--account-id", ACCOUNT])
    assert managed.exit_code == 0, managed.output
    assert "AWS authorization required" in managed.stdout
    assert "lazycloud cloud authorize --profile YOUR_AWS_PROFILE" in managed.stdout
    assert "lazycloud cloud validate" in managed.stdout
    assert fake_api.calls("POST", "/v1/aws-connection")[-1].json() == {"account_id": ACCOUNT}

    networks: dict[str, object] = {
        "us-east-2": {
            "vpc_id": "vpc-1",
            "subnet_ids": ["subnet-a", "subnet-b"],
            "security_group_id": "sg-1",
        }
    }
    existing = CliRunner().invoke(
        cli,
        [
            "--json",
            "cloud",
            "connect",
            "aws",
            "--account-id",
            ACCOUNT,
            "--role-arn",
            ROLE,
            "--networks-json",
            json.dumps(networks),
        ],
    )
    assert existing.exit_code == 0, existing.output
    assert json.loads(existing.stdout)["authorization"]["external_id"] == EXTERNAL_ID
    assert fake_api.calls("POST", "/v1/aws-connection")[-1].json() == {
        "account_id": ACCOUNT,
        "role_arn": ROLE,
        "networks": networks,
    }
    shown = CliRunner().invoke(
        cli, ["cloud", "connect", "aws", "--account-id", ACCOUNT, "--role-arn", ROLE]
    )
    assert EXTERNAL_ID in shown.stdout
    assert "cloud authorize" not in shown.stdout


def test_cloud_reconnect_cancel_and_retry(fake_api: FakeApi) -> None:
    @fake_api.route("POST", "/v1/aws-connection/reconnect")
    def reconnect(request: ApiRequest) -> Reply:
        return json_reply(
            {
                "connection": _connection("reconnect_pending"),
                "authorization": {"stack": _stack()},
            }
        )

    fake_api.route("DELETE", "/v1/aws-connection/reconnect")(
        lambda _: json_reply(_connection("ready", detail="Serving workloads."))
    )
    fake_api.route("POST", "/v1/aws-connection/retry")(
        lambda _: json_reply(
            _connection(
                "action_required",
                detail="Automatic AWS cleanup needs attention.",
                customer_action={"url": "https://console.aws.amazon.com/x", "label": "Open AWS"},
            )
        )
    )

    started = CliRunner().invoke(cli, ["cloud", "reconnect", "--role-arn", ROLE])
    assert started.exit_code == 0, started.output
    assert "Replacement authorization required" in started.stdout
    assert "reconnect pending" in started.stdout
    assert fake_api.calls("POST", "/v1/aws-connection/reconnect")[-1].json() == {"role_arn": ROLE}
    CliRunner().invoke(cli, ["cloud", "reconnect"])
    assert fake_api.calls("POST", "/v1/aws-connection/reconnect")[-1].json() == {}

    cancelled = CliRunner().invoke(cli, ["cloud", "cancel-reconnect"])
    assert cancelled.exit_code == 0, cancelled.output
    assert "Reconnect cancelled" in cancelled.stdout
    assert "Serving workloads." in cancelled.stdout

    retried = CliRunner().invoke(cli, ["cloud", "retry"])
    assert retried.exit_code == 0, retried.output
    assert "Cloud action retried" in retried.stdout
    assert "Open AWS" in retried.stdout
    assert "https://console.aws.amazon.com/x" in retried.stdout
    payload = json.loads(CliRunner().invoke(cli, ["--json", "cloud", "retry"]).stdout)
    assert payload["phase"] == "action_required"


def test_cloud_validate_reports_failure_after_printing(fake_api: FakeApi) -> None:
    result: list[dict[str, object]] = [
        _connection(
            "degraded",
            detail="Checking AWS authorization.",
            pending_authorization=_authorization(
                phase="degraded",
                error_code="assume_role_denied",
                error_message="The role does not trust the platform.",
            ),
        )
    ]
    fake_api.route("POST", "/v1/aws-connection/validate")(lambda _: json_reply(result[0]))

    failed = CliRunner().invoke(cli, ["cloud", "validate"])
    assert failed.exit_code == 1
    assert "AWS validation failed" in failed.stdout
    assert "assume_role_denied: The role does not trust the platform." in failed.stdout

    as_json = CliRunner().invoke(cli, ["--json", "cloud", "validate"])
    assert as_json.exit_code == 1
    payload = json.loads(as_json.stdout)
    assert payload["pending_authorization"]["error_code"] == "assume_role_denied"

    result[0] = _connection(
        "ready", detail="Serving workloads.", active_authorization=_authorization()
    )
    passed = CliRunner().invoke(cli, ["cloud", "validate"])
    assert passed.exit_code == 0, passed.output
    assert "AWS authorization validated" in passed.stdout
    assert "Serving workloads." in passed.stdout


def test_cloud_status_shows_and_waits_for_a_phase(
    fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _Connection(fake_api, None)
    monkeypatch.setattr("lazycloud.cli.resources.time.sleep", _no_sleep)

    none = CliRunner().invoke(cli, ["cloud", "status"])
    assert none.exit_code == 0, none.output
    assert "No cloud account is connected." in none.stdout
    assert json.loads(CliRunner().invoke(cli, ["--json", "cloud", "status"]).stdout) == {
        "connection": None
    }

    connection.current = _connection("validating", detail="Checking AWS authorization.")
    connection.upcoming = [
        _connection("validating", detail="Checking AWS authorization."),
        _connection("validating", detail="Checking AWS authorization."),
        _connection("ready", detail="Serving workloads."),
    ]
    watched = CliRunner().invoke(
        cli, ["cloud", "status", "--watch", "--until", "ready", "--interval", "0.2"]
    )
    assert watched.exit_code == 0, watched.output
    assert watched.stdout.count("Checking AWS authorization.") == 2
    assert "Serving workloads." in watched.stdout

    assert CliRunner().invoke(cli, ["cloud", "status", "--until", "ready"]).exit_code == 2
    assert CliRunner().invoke(cli, ["cloud", "status", "--watch"]).exit_code == 2


def test_cloud_disconnect_waits_for_removal(
    fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _Connection(fake_api, _connection("ready"))
    monkeypatch.setattr("lazycloud.cli.resources.time.sleep", _no_sleep)
    opened: list[str] = []
    monkeypatch.setattr("lazycloud.cli.resources.webbrowser.open", opened.append)

    started = CliRunner().invoke(cli, ["--json", "cloud", "disconnect"])
    assert started.exit_code == 0, started.output
    assert json.loads(started.stdout)["connection"]["phase"] == "disconnect_draining"

    connection.upcoming = [_connection("ready"), None]
    waited = CliRunner().invoke(cli, ["cloud", "disconnect", "--wait", "--open"])
    assert waited.exit_code == 0, waited.output
    assert "disconnect draining" in waited.stdout
    assert "AWS account removed" in waited.stdout
    assert opened == []

    missing = CliRunner().invoke(cli, ["cloud", "disconnect"])
    assert "does not have an AWS account connection" in str(missing.exception)


def test_cloud_disconnect_opens_only_the_recovery_action(
    fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery = _connection(
        "action_required",
        detail="Automatic AWS cleanup needs attention before removal can finish.",
        customer_action={
            "url": "https://console.aws.amazon.com/cloudformation/final",
            "label": "Review cleanup in AWS",
        },
    )
    connection = _Connection(fake_api, _connection("ready"))
    connection.upcoming = [_connection("ready"), recovery]
    monkeypatch.setattr("lazycloud.cli.resources.time.sleep", _no_sleep)
    opened: list[str] = []
    monkeypatch.setattr("lazycloud.cli.resources.webbrowser.open", opened.append)

    result = CliRunner().invoke(cli, ["cloud", "disconnect", "--wait", "--open"])

    assert result.exit_code == 0, result.output
    assert "AWS disconnect status" in result.stdout
    assert "Review cleanup in AWS" in result.stdout
    assert opened == ["https://console.aws.amazon.com/cloudformation/final"]


_FAKE_AWS = """#!/bin/sh
echo "$*" >> "$FAKE_AWS_LOG"
case "$*" in
  *"sts get-caller-identity"*) printf '{"Account": "%s"}' "$FAKE_AWS_ACCOUNT" ;;
  *"ec2 describe-availability-zones"*)
    printf '{"AvailabilityZones": [{"ZoneName": "us-east-2c"}, {"ZoneName": "us-east-2a"}]}' ;;
  *"cloudformation create-stack"*)
    for argument in "$@"; do
      case "$argument" in file://*) cp "${argument#file://}" "$FAKE_AWS_STACK" ;; esac
    done
    printf '{"StackId": "arn:aws:cloudformation:us-east-2:123456789012:stack/lazycloud/1"}' ;;
esac
"""


def test_cloud_authorize_submits_the_stack_with_the_customer_profile(
    fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "aws").write_text(_FAKE_AWS)
    (binary / "aws").chmod(0o755)
    log, stack = tmp_path / "aws.log", tmp_path / "stack.json"
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_AWS_LOG", str(log))
    monkeypatch.setenv("FAKE_AWS_STACK", str(stack))
    monkeypatch.setenv("FAKE_AWS_ACCOUNT", ACCOUNT)
    connection = _Connection(
        fake_api,
        _connection(
            "awaiting_authorization",
            customer_action={"label": "Authorize in AWS", "stack": _stack()},
        ),
    )

    submitted = CliRunner().invoke(cli, ["--json", "cloud", "authorize", "--profile", "customer"])

    assert submitted.exit_code == 0, submitted.output
    assert json.loads(submitted.stdout) == {
        "stack_id": "arn:aws:cloudformation:us-east-2:123456789012:stack/lazycloud/1",
        "status": "CREATE_IN_PROGRESS",
    }
    assert all(
        line.startswith("--region us-east-2 --profile customer ")
        for line in log.read_text().splitlines()
    )
    request = json.loads(stack.read_text())
    assert request["StackName"] == "lazycloud-connection"
    assert request["Parameters"] == [
        {"ParameterKey": "ExternalId", "ParameterValue": EXTERNAL_ID},
        {"ParameterKey": "AvailabilityZoneA", "ParameterValue": "us-east-2a"},
        {"ParameterKey": "AvailabilityZoneB", "ParameterValue": "us-east-2c"},
    ]
    shown = CliRunner().invoke(cli, ["cloud", "authorize"])
    assert "AWS connection stack submitted" in shown.stdout

    stack.unlink()
    monkeypatch.setenv("FAKE_AWS_ACCOUNT", "210987654321")
    other = CliRunner().invoke(cli, ["cloud", "authorize"])
    assert "AWS credentials belong to a different account; no stack was created" in str(
        other.exception
    )
    assert not stack.exists()

    connection.current = _connection("ready")
    nothing = CliRunner().invoke(cli, ["cloud", "authorize"])
    assert "there is no pending AWS connection stack to authorize" in str(nothing.exception)
