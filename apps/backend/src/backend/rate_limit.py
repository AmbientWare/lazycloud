"""Rate limiting configuration for the API."""

from slowapi import Limiter
from slowapi.util import get_remote_address

from backend.config import app_config

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[app_config.RATE_LIMIT],
    storage_uri=app_config.REDIS_URL,
)
