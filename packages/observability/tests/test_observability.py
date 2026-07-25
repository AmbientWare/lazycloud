from __future__ import annotations

import pytest
from observability.telemetry import (
    plan_telemetry_endpoint,
)


def test_invalid_telemetry_endpoint_is_rejected() -> None:
    with pytest.raises(ValueError, match="http or https"):
        plan_telemetry_endpoint("collector:4318")
