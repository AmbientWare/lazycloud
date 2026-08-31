from __future__ import annotations

import pytest
from networking.settings import (
    BackendRouteSettings,
    validate_remote_provider_network_configuration,
)
from pydantic import SecretStr


def test_remote_provider_gate_reports_every_missing_network_requirement() -> None:
    route = BackendRouteSettings(auth_key=SecretStr("short"))

    with pytest.raises(ValueError) as error:
        validate_remote_provider_network_configuration(
            gateway_origin="https://user@control.example.test/path",
            presigned_origin="http://object-store:9002",
            backend_route=route,
        )

    message = str(error.value)
    assert "gateway HTTP URL must be an HTTPS origin" in message
    assert "'object-store' is unreachable from a remote machine" in message
    assert "backend route authentication key must be at least 32 bytes" in message
