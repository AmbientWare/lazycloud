from collections.abc import Iterator
from contextlib import ExitStack

import pytest
from apps.api.tests.runtime import (
    api_client,
    api_runtime,
    api_workspace,
    isolated_services,
    unpriced_services,
)


@pytest.fixture
def client_stack() -> Iterator[ExitStack]:
    with ExitStack() as stack:
        yield stack


__all__ = ["api_client", "api_runtime", "api_workspace", "isolated_services", "unpriced_services"]
