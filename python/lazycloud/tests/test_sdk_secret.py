"""Secret and `lazycloud secret` on the public API's secret operations."""

from __future__ import annotations

import json

import pytest
from lazycloud.abstractions.secret import Secret, SecretOperationError
from lazycloud.cli.main import build_public_cli
from typer.testing import CliRunner

from tests.api_server import ApiRequest, FakeApi, Reply, error_reply, json_reply

SECRETS = "/v1/workspaces/team/secrets"
NOW = "2026-09-30T12:00:00Z"


def _secret(name: str) -> dict[str, str]:
    return {"name": name, "created_at": NOW, "updated_at": NOW, "used_by": []}


@pytest.fixture
def secrets_api(fake_api: FakeApi) -> FakeApi:
    values: dict[str, str] = {}

    @fake_api.route("POST", SECRETS)
    def create(request: ApiRequest) -> Reply:
        body = request.json()
        if body["name"] in values:
            return error_reply("conflict", f"secret already exists: {body['name']}", 409)
        values[body["name"]] = body["value"]
        return json_reply(_secret(body["name"]), 201)

    @fake_api.route("GET", SECRETS)
    def listing(request: ApiRequest) -> Reply:
        names = sorted(values)
        cursor = request.query.get("cursor", [""])[0]
        page = [n for n in names if n > cursor][:2]
        more = page and page[-1] != names[-1]
        payload: dict[str, object] = {"secrets": [_secret(n) for n in page]}
        if more:
            payload["next_cursor"] = page[-1]
        return json_reply(payload)

    @fake_api.route("GET", SECRETS + r"/(?P<name>\w+)")
    def get(request: ApiRequest) -> Reply:
        name = request.path.rsplit("/", 1)[1]
        if name not in values:
            return error_reply("not_found", f"secret not found: {name}", 404)
        return json_reply(_secret(name))

    @fake_api.route("GET", SECRETS + r"/\w+/value")
    def reveal(request: ApiRequest) -> Reply:
        name = request.path.split("/")[-2]
        if name not in values:
            return error_reply("not_found", f"secret not found: {name}", 404)
        return json_reply(
            {"name": name, "value": values[name], "created_at": NOW, "updated_at": NOW}
        )

    @fake_api.route("PUT", SECRETS + r"/\w+")
    def put(request: ApiRequest) -> Reply:
        values[request.path.rsplit("/", 1)[1]] = request.json()["value"]
        return json_reply(_secret(request.path.rsplit("/", 1)[1]))

    @fake_api.route("PATCH", SECRETS + r"/\w+")
    def patch(request: ApiRequest) -> Reply:
        name = request.path.rsplit("/", 1)[1]
        if name not in values:
            return error_reply("not_found", f"secret not found: {name}", 404)
        values[name] = request.json()["value"]
        return json_reply(_secret(name))

    @fake_api.route("DELETE", SECRETS + r"/\w+")
    def delete(request: ApiRequest) -> Reply:
        name = request.path.rsplit("/", 1)[1]
        if values.pop(name, None) is None:
            return error_reply("not_found", f"secret not found: {name}", 404)
        return 204, {}, b""

    return fake_api


def test_secret_lifecycle_and_errors(secrets_api: FakeApi) -> None:
    secret = Secret("API_TOKEN")

    assert secret.create("first").value == "first"
    with pytest.raises(SecretOperationError, match="secret already exists: API_TOKEN"):
        secret.create("again")
    assert secret.update("second").value == "second"
    assert secret.set("third").value == "third"
    assert secret.get() == "third"
    assert secret.delete() is True
    with pytest.raises(SecretOperationError, match="secret not found: API_TOKEN"):
        secret.get()
    with pytest.raises(SecretOperationError, match="secret not found: MISSING"):
        Secret("MISSING").update("x")


def test_cli_masks_values_unless_revealed_and_lists_every_page(secrets_api: FakeApi) -> None:
    cli = build_public_cli()
    runner = CliRunner()
    for name in ("A_KEY", "B_KEY", "C_KEY"):
        assert runner.invoke(cli, ["secret", "create", name, f"value-of-{name}"]).exit_code == 0

    masked = runner.invoke(cli, ["secret", "show", "A_KEY"])
    assert masked.exit_code == 0
    assert "********" in masked.stdout
    assert "value-of-A_KEY" not in masked.stdout
    # A masked show never fetches the value.
    assert not secrets_api.calls("GET", SECRETS + "/A_KEY/value")

    revealed = runner.invoke(cli, ["--json", "secret", "show", "A_KEY", "--reveal"])
    assert json.loads(revealed.stdout)["value"] == "value-of-A_KEY"

    listed = runner.invoke(cli, ["--json", "secret", "list"])
    rows = json.loads(listed.stdout)
    assert [row["name"] for row in rows] == ["A_KEY", "B_KEY", "C_KEY"]
    assert {row["value"] for row in rows} == {"********"}

    modified = runner.invoke(cli, ["--json", "secret", "modify", "B_KEY", "new"])
    assert json.loads(modified.stdout) == {"name": "B_KEY", "updated": True}
    deleted = runner.invoke(cli, ["--json", "secret", "delete", "C_KEY"])
    assert json.loads(deleted.stdout) == {"name": "C_KEY", "deleted": True}
