"""
AWS Secrets Manager client for fetching secrets (kubeconfigs, etc.)
"""

import boto3
from botocore.exceptions import ClientError
from cachetools import TTLCache
from loguru import logger

from backend.config import app_config

_client = None
_cache: TTLCache[str, str] = TTLCache(maxsize=100, ttl=300)  # 5 min cache


def get_secrets_client():
    """Get or create the Secrets Manager client."""
    global _client
    if _client is None:
        _client = boto3.client(
            "secretsmanager",
            region_name=app_config.AWS_REGION or "us-east-1",
            aws_access_key_id=app_config.AWS_ACCESS_KEY_ID or None,
            aws_secret_access_key=app_config.AWS_SECRET_ACCESS_KEY or None,
        )
    return _client


def get_secret(secret_id: str, use_cache: bool = True) -> str | None:
    """Fetch a secret from AWS Secrets Manager.

    Args:
        secret_id: The secret name or ARN
        use_cache: Whether to use cached value if available (default True)

    Returns:
        The secret string value, or None if not found
    """
    if use_cache and secret_id in _cache:
        logger.debug(f"Cache hit for secret: {secret_id}")
        return _cache[secret_id]

    try:
        client = get_secrets_client()
        response = client.get_secret_value(SecretId=secret_id)
        value = response.get("SecretString")

        if value and use_cache:
            _cache[secret_id] = value
            logger.debug(f"Cached secret: {secret_id}")

        return value

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "ResourceNotFoundException":
            logger.warning(f"Secret not found: {secret_id}")
        elif error_code == "AccessDeniedException":
            logger.error(f"Access denied to secret: {secret_id}")
        else:
            logger.error(f"Failed to fetch secret {secret_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching secret {secret_id}: {e}")
        return None


def clear_cache():
    """Clear the secrets cache (useful for testing or forced refresh)."""
    _cache.clear()
    logger.debug("Secrets cache cleared")


def invalidate_secret(secret_id: str):
    """Remove a specific secret from the cache."""
    if secret_id in _cache:
        del _cache[secret_id]
        logger.debug(f"Invalidated cached secret: {secret_id}")
