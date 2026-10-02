from __future__ import annotations

import pytest
from pydantic import ValidationError
from shared.deployment_records import DeploymentSpec


@pytest.mark.parametrize("port", [0, 65536, 8080.5, "8080", True])
def test_deployment_spec_rejects_invalid_or_coerced_ports(port: object) -> None:
    with pytest.raises(ValidationError):
        DeploymentSpec.model_validate({"name": "pod", "ports": {"http": port}})
