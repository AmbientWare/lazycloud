from __future__ import annotations

import json
from pathlib import Path

import pytest
from runner.serve import EndpointServeRunner
from shared.env import HOT_RELOAD_DIR_ENV
from shared.http.endpoints import EndpointForwardRequest


def test_endpoint_runner_reload_evicts_mounted_user_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_code = tmp_path / "endpoint-code"
    user_code.mkdir()
    module = user_code / "app.py"
    module.write_text("def handler():\n    return 'v1'\n", encoding="utf-8")
    monkeypatch.setattr("foundation.handler_loading.USER_CODE_DIR", user_code)
    monkeypatch.setenv(HOT_RELOAD_DIR_ENV, str(user_code))
    runner = EndpointServeRunner(handler_ref="app:handler")

    assert runner.handler()() == "v1"

    module.write_text("def handler():\n    return 'version-two'\n", encoding="utf-8")
    runner.reload_handler()

    assert runner.handler()() == "version-two"


def test_endpoint_runner_coerces_pydantic_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_code = tmp_path / "endpoint-code"
    user_code.mkdir()
    module = user_code / "app.py"
    module.write_text(
        """
from pydantic import BaseModel


class Payload(BaseModel):
    value: str
    count: int = 10


class Result(BaseModel):
    value: str


def handler(payload: Payload) -> Result:
    assert isinstance(payload, Payload)
    return Result(value=f"{payload.value}:{payload.count}")
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("foundation.handler_loading.USER_CODE_DIR", user_code)
    runner = EndpointServeRunner(handler_ref="app:handler")

    response = runner.handle(
        EndpointForwardRequest(
            stub_id="endpoint",
            method="POST",
            body=json.dumps({"payload": {"value": "clip"}}).encode("utf-8"),
        )
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {"value": "clip:10"}
