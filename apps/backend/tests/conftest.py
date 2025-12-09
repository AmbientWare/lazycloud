"""Pytest configuration - expose all fixtures for auto-discovery."""

# Core fixtures (test_user, test_workspace, test_deployment, etc.)
# Compose fixtures (simple_compose_file, complex_compose_file, etc.)
from tests.fixtures.compose import *  # noqa: F401, F403
from tests.fixtures.core import *  # noqa: F401, F403

# Database fixtures (db, db_session, db_user, etc.)
from tests.fixtures.database import *  # noqa: F401, F403

# K8s fixtures (helm_generator, conversion cases, etc.)
from tests.fixtures.k8s import *  # noqa: F401, F403
