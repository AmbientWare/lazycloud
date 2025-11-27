"""Pytest configuration - loads all fixtures via pytest_plugins."""

pytest_plugins = [
    "pytest_asyncio",
    "lazycloud_api.tests.fixtures.core",
    "lazycloud_api.tests.fixtures.compose",
    "lazycloud_api.tests.fixtures.k8s",
    "lazycloud_api.tests.fixtures.database",
]

TEST_JWT_SECRET = "test-secret-key"
TEST_JWT_ALGORITHM = "HS256"
