from __future__ import annotations

from collections.abc import Callable

import pytest
from observability.managed_billing import ManagedBillingMode
from observability.settings import (
    ManagedBillingClientSettings,
    UsageMetricsSettings,
)
from pydantic import SecretStr, ValidationError
from shared.usage import UsageCollectorKind


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: UsageMetricsSettings(collector=UsageCollectorKind.OpenMeter),
            "OpenMeter URL is required",
        ),
        (
            lambda: ManagedBillingClientSettings(mode=ManagedBillingMode.Http),
            "managed billing endpoint is required",
        ),
        (
            lambda: ManagedBillingClientSettings(
                required=True,
                mode=ManagedBillingMode.Disabled,
            ),
            "required managed billing cannot be disabled",
        ),
    ],
)
def test_observability_settings_reject_incomplete_runtime_configuration(
    factory: Callable[[], UsageMetricsSettings | ManagedBillingClientSettings],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        factory()


def test_observability_secrets_are_masked_in_settings_repr() -> None:
    settings = ManagedBillingClientSettings(
        auth_token=SecretStr("billing-token"),
        headers={"X-Secret": SecretStr("header-token")},
    )

    rendered = repr(settings)

    assert "billing-token" not in rendered
    assert "header-token" not in rendered
