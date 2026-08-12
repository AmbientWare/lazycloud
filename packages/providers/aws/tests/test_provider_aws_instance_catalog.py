from __future__ import annotations

import pytest
from provider_aws import (
    aws_instance_catalog_entry,
)


def test_instance_catalog_rejects_unsupported_capacity() -> None:
    with pytest.raises(ValueError, match="unsupported AWS managed-capacity instance type"):
        aws_instance_catalog_entry("mystery.large")
