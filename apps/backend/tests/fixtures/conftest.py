"""Expose all fixtures for pytest auto-discovery."""

from tests.fixtures.compose import *  # noqa: F401, F403
from tests.fixtures.core import *  # noqa: F401, F403
from tests.fixtures.database import *  # noqa: F401, F403
from tests.fixtures.k8s import *  # noqa: F401, F403
