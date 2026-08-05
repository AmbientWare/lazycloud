from __future__ import annotations

import pytest
from observability.settings import UsageMetricsSettings
from pydantic import SecretStr, ValidationError
from shared.usage import UsageCollectorKind


def test_observability_settings_reject_incomplete_runtime_configuration() -> None:
    with pytest.raises(ValidationError, match="OpenMeter URL is required"):
        UsageMetricsSettings(collector=UsageCollectorKind.OpenMeter)


def test_observability_secrets_are_masked_in_settings_repr() -> None:
    settings = UsageMetricsSettings(
        collector=UsageCollectorKind.OpenMeter,
        openmeter_url="https://openmeter.example",
        openmeter_api_key=SecretStr("openmeter-token"),
    )

    rendered = repr(settings)

    assert "openmeter-token" not in rendered
