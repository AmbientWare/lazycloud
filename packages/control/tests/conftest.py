from collections.abc import Iterator
from contextlib import ExitStack

import pytest
from tests.service_fixtures import isolated_services, postgres_database_url, postgres_services


@pytest.fixture
def client_stack() -> Iterator[ExitStack]:
    with ExitStack() as stack:
        yield stack


__all__ = ["client_stack", "isolated_services", "postgres_database_url", "postgres_services"]
