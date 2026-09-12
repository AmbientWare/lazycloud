from __future__ import annotations

import pytest
from networking.settings import (
    validate_remote_provider_network_configuration,
)


def test_remote_provider_gate_reports_every_missing_network_requirement() -> None:
    with pytest.raises(ValueError) as error:
        validate_remote_provider_network_configuration(
            gateway_origin="https://user@control.example.test/path",
            presigned_origin="http://object-store:9002",
        )

    message = str(error.value)
    assert "gateway HTTP URL must be an HTTPS origin" in message
    assert "'object-store' is unreachable from a remote machine" in message
