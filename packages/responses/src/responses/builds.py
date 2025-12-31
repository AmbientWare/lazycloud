from datetime import datetime

from pydantic import BaseModel, Field

# Patterns that indicate transient/retryable build errors
RETRYABLE_BUILD_ERROR_PATTERNS = [
    "tls: bad record mac",
    "rpc error",
    "unavailable",
    "error reading from server",
    "connection reset",
    "connection refused",
    "timeout",
    "network is unreachable",
    "temporary failure",
    "eof",
    "broken pipe",
]


def is_retryable_build_error(error_text: str) -> bool:
    """Check if a build error is transient and worth retrying."""
    error_lower = error_text.lower()
    return any(pattern in error_lower for pattern in RETRYABLE_BUILD_ERROR_PATTERNS)


class DepotTokenResponse(BaseModel):
    """Response with Depot project token for CLI builds."""

    project_id: str = Field(description="Depot project ID for this workspace")
    token: str = Field(description="Short-lived project token for depot build")
    expires_at: datetime = Field(description="Token expiration time")
    registry_url: str = Field(description="Depot registry URL for images")
