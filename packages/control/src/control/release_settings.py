from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ReleaseSettings(BaseSettings):
    manifest_url: str = ""
    active_file: Path = Path("/run/lazycloud/release/active.json")
    fetch_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    model_config = SettingsConfigDict(env_prefix="LAZYCLOUD_RELEASE_", extra="ignore")

    @field_validator("manifest_url")
    @classmethod
    def validate_manifest_url(cls, value: str) -> str:
        url = value.strip()
        if not url:
            return url
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("release manifest URL must be an HTTPS URL without credentials")
        return url


__all__ = ["ReleaseSettings"]
