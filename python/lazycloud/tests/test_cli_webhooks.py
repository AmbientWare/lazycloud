"""`lazycloud webhook` on the public API's webhook operations."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from lazycloud.cli.main import build_public_cli
from typer.testing import CliRunner

from tests.api_server import ApiRequest, FakeApi, Reply, json_reply

cli = build_public_cli()

WEBHOOKS = "/v1/workspaces/team/webhooks"
NOW = "2026-11-02T12:00:00Z"


@pytest.fixture
def webhooks_api(fake_api: FakeApi) -> FakeApi:
    stored: list[dict[str, object]] = []

    @fake_api.route("POST", WEBHOOKS)
    def create(request: ApiRequest) -> Reply:
        body = request.json()
        webhook: dict[str, object] = {
            "id": str(uuid4()),
            "url": body["url"],
            "events": body["events"],
            "description": body.get("description", ""),
            "enabled": body.get("enabled", True),
            "apps": body.get("apps", []),
            "created_at": NOW,
        }
        stored.append(webhook)
        return json_reply(webhook, 201)

    @fake_api.route("GET", WEBHOOKS)
    def listing(request: ApiRequest) -> Reply:
        return json_reply({"webhooks": stored})

    return fake_api


def test_added_webhooks_list_with_their_events_and_app_scope(webhooks_api: FakeApi) -> None:
    runner = CliRunner()
    scoped = runner.invoke(
        cli,
        [
            "webhook",
            "add",
            "https://hooks.example.com/a",
            "--event",
            "task.failed",
            "--event",
            "workload.deployed",
            "--app",
            "tools",
            "--app",
            "reports",
            "--disabled",
        ],
    )
    assert scoped.exit_code == 0, scoped.output
    everywhere = runner.invoke(
        cli, ["webhook", "add", "https://hooks.example.com/b", "--event", "task.succeeded"]
    )
    assert everywhere.exit_code == 0, everywhere.output
    assert runner.invoke(cli, ["webhook", "add", "https://x.example.com"]).exit_code != 0
    assert len(webhooks_api.calls("POST", WEBHOOKS)) == 2

    listed = runner.invoke(cli, ["--json", "webhook", "list"])
    rows = json.loads(listed.stdout)
    assert [(row["url"], row["apps"], row["events"], row["enabled"]) for row in rows] == [
        (
            "https://hooks.example.com/a",
            ["tools", "reports"],
            ["task.failed", "workload.deployed"],
            False,
        ),
        ("https://hooks.example.com/b", [], ["task.succeeded"], True),
    ]

    table = runner.invoke(cli, ["webhook", "list"])
    assert "all apps" in table.stdout
