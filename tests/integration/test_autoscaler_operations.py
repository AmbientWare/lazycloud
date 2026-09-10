from __future__ import annotations

from collections.abc import Mapping

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from cli.api_client import AdminApiClient
from cli.main import build_admin_cli
from control.service import ControlPlaneService, StubKind, StubRecord
from fastapi.testclient import TestClient
from identity.auth import AuthService
from pydantic import BaseModel, JsonValue, TypeAdapter
from scheduler.autoscaling import (
    ENDPOINT_AUTOSCALER_SOURCE,
)
from shared.autoscaler_state import (
    AutoscalerStateRecord,
    AutoscalerTargetKind,
    autoscaler_state_name,
)
from shared.http_transport import HttpChannel
from typer.testing import CliRunner

from cli import operations

cli = build_admin_cli()


class _AutoscalerCliStatusItem(BaseModel):
    stub_name: str


class _AutoscalerCliStatus(BaseModel):
    items: list[_AutoscalerCliStatusItem]


class _AutoscalerCliControl(BaseModel):
    autoscaling_enabled: bool


_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class _FastApiHttpChannel(HttpChannel):
    def __init__(self, client: TestClient, *, token: str) -> None:
        super().__init__(token=token)
        self.client = client

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, JsonValue] | None = None,
        timeout_seconds: float | None = None,
    ) -> JsonValue:
        # TestClient executes in-process and does not support network timeouts.
        _ = timeout_seconds
        response = self.client.request(
            method,
            path,
            json=dict(payload) if payload is not None else None,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        assert response.status_code < 400, response.text
        if response.status_code == 204:
            return None
        return _JSON_VALUE_ADAPTER.validate_python(response.json())


def test_autoscaler_cli_controls_real_api_and_persists_owner_state(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _create_endpoint_stub(isolated_services)
    _record_state(isolated_services, stub)
    isolated_services.events.emit(
        "endpoint.autoscaler.scale_decision",
        resource_type="stub",
        resource_id=stub.id,
        message="scaled endpoint replicas",
        workspace_id=stub.workspace_id,
        data={"desired_containers": 2},
    )
    token, _ = AuthService(isolated_services.context).create_token(
        "admin",
        scopes=["read", "write"],
    )

    with TestClient(create_app(isolated_services)) as http_client:
        channel = _FastApiHttpChannel(http_client, token=token)

        def resolve_client(workspace: str | None = None) -> AdminApiClient:
            return AdminApiClient(channel=channel, workspace=workspace or "default")

        monkeypatch.setattr(operations, "admin_api_client", resolve_client)
        runner = CliRunner()
        status = runner.invoke(
            cli,
            ["--json", "scheduler", "autoscaler", "status", "--workspace", "default"],
        )
        paused = runner.invoke(
            cli,
            [
                "--json",
                "scheduler",
                "autoscaler",
                "pause",
                stub.name,
                "--workspace",
                "default",
            ],
        )
        resumed = runner.invoke(
            cli,
            [
                "--json",
                "scheduler",
                "autoscaler",
                "resume",
                stub.id,
                "--workspace",
                "default",
            ],
        )
        persisted_metadata = (
            ControlPlaneService(isolated_services.context).get_stub(stub.id).config.metadata
        )
        history_actions = [
            event.action
            for event in isolated_services.autoscaler_operations_service.history(
                workspace="default", target_id=stub.id
            ).events
        ]

    assert status.exit_code == 0, status.output
    assert _AutoscalerCliStatus.model_validate_json(status.output).items[0].stub_name == stub.name
    assert paused.exit_code == 0, paused.output
    assert not _AutoscalerCliControl.model_validate_json(paused.output).autoscaling_enabled
    assert resumed.exit_code == 0, resumed.output
    assert _AutoscalerCliControl.model_validate_json(resumed.output).autoscaling_enabled
    assert persisted_metadata == {"autoscaling_enabled": True}
    assert history_actions == ["endpoint.autoscaler.scale_decision"]


def _create_endpoint_stub(
    services: ApiServices,
    *,
    config: dict[str, JsonValue] | None = None,
) -> StubRecord:
    return ControlPlaneService(services.context).create_stub(
        "endpoint-autoscale",
        kind=StubKind.Endpoint,
        handler="pkg.endpoint:handler",
        config=config
        or {
            "image": {"image_id": "img-endpoint"},
            "autoscaler": {"max_containers": 2, "tasks_per_container": 1},
        },
    )


def _record_state(services: ApiServices, stub: StubRecord) -> AutoscalerStateRecord:
    state = AutoscalerStateRecord(
        name=autoscaler_state_name(AutoscalerTargetKind.Endpoint, stub.id),
        workspace_id=stub.workspace_id,
        source=ENDPOINT_AUTOSCALER_SOURCE,
        target_kind=AutoscalerTargetKind.Endpoint,
        target_id=stub.id,
        deployment_id=stub.deployment_id or "",
        app_id=stub.app_id or "",
        current_count=1,
        desired_count=2,
        signal_name="active_dispatches",
        signal_value=4,
        decision="scale-up",
        reason="queue-pending",
        last_sample={"queue_length": 4},
    )
    return services.autoscaler_states.upsert(state)
