from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from lazycloud.cli.main import build_public_cli
from lazycloud.clients.compute.control import ComputeClient
from pydantic import JsonValue
from typer.testing import CliRunner


@dataclass(slots=True)
class _RecordingComputeChannel:
    calls: list[tuple[str, str, dict[str, JsonValue] | None]] = field(default_factory=list)

    def get(self, path: str) -> JsonValue:
        self.calls.append(("GET", path, None))
        return {
            "name": "managed-aws",
            "desired_machines": 0,
            "max_machines": 1,
            "observed_machines": 0,
            "phase": "ready",
            "status": "ready",
        }

    def post(
        self,
        path: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue:
        raise AssertionError(f"unexpected POST {path}: {payload}")

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue:
        self.calls.append((method, path, payload))
        return {
            "name": "managed-aws",
            "desired_machines": 0,
            "max_machines": 1,
            "observed_machines": 1,
            "phase": "updating",
            "status": "scaling to zero",
        }


def _install_channel(monkeypatch: pytest.MonkeyPatch) -> _RecordingComputeChannel:
    channel = _RecordingComputeChannel()

    def compute_client(*, workspace: str | None = None) -> ComputeClient:
        assert workspace == "acceptance"
        return ComputeClient(channel=channel, workspace=workspace)

    monkeypatch.setattr("lazycloud.cli.resources.compute_client", compute_client)
    return channel


def test_pool_scale_cli_puts_durable_capacity_through_the_public_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _install_channel(monkeypatch)

    result = CliRunner().invoke(
        build_public_cli(),
        ["--json", "pool", "scale", "managed-aws", "--nodes", "0", "--workspace", "acceptance"],
    )

    assert result.exit_code == 0, result.output
    assert channel.calls == [
        (
            "PUT",
            "/api/v1/pools/managed-aws/scale?workspace=acceptance",
            {"desired_machines": 0},
        )
    ]
    payload = json.loads(result.stdout)
    assert payload["desired_machines"] == 0
    assert payload["phase"] == "updating"


def test_pool_status_cli_reads_durable_state_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _install_channel(monkeypatch)

    result = CliRunner().invoke(
        build_public_cli(),
        ["--json", "pool", "status", "managed-aws", "--workspace", "acceptance"],
    )

    assert result.exit_code == 0, result.output
    assert channel.calls == [("GET", "/api/v1/pools/managed-aws/state?workspace=acceptance", None)]
    assert json.loads(result.stdout)["phase"] == "ready"
