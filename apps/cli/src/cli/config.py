import os
from pathlib import Path

from pydantic import PrivateAttr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CLIConfig(BaseSettings):
    """CLI Configuration"""

    model_config = SettingsConfigDict(env_prefix="LAZYCLOUD_")

    # API Configuration
    api_base_url: str = "http://localhost:8000"
    api_version: str = "v1"
    registry_type: str = "ecr"

    # Private attributes for OAuth token management (WorkOS CLI Auth)
    _access_token: str | None = PrivateAttr(default=None)
    _refresh_token: str | None = PrivateAttr(default=None)

    # Private attributes for workspace management
    _active_workspace_id: str | None = PrivateAttr(default=None)
    _active_workspace_name: str | None = PrivateAttr(default=None)

    def __init__(self, **data):
        super().__init__(**data)
        self._load_config()

    def _load_config(self):
        """Load configuration from the config file"""
        config_path = Path.home() / ".lazycloud"
        if config_path.exists():
            with open(config_path) as f:
                for line in f:
                    if line.startswith("ACCESS_TOKEN="):
                        self._access_token = line.strip().split("=", 1)[1]
                    elif line.startswith("REFRESH_TOKEN="):
                        self._refresh_token = line.strip().split("=", 1)[1]
                    elif line.startswith("ACTIVE_WORKSPACE_ID="):
                        self._active_workspace_id = line.strip().split("=", 1)[1]
                    elif line.startswith("ACTIVE_WORKSPACE_NAME="):
                        self._active_workspace_name = line.strip().split("=", 1)[1]

    def _save_config(self):
        """Save configuration to the config file"""
        config_path = Path.home() / ".lazycloud"
        with open(config_path, "w") as f:
            if self._access_token:
                f.write(f"ACCESS_TOKEN={self._access_token}\n")
            if self._refresh_token:
                f.write(f"REFRESH_TOKEN={self._refresh_token}\n")
            if self._active_workspace_id:
                f.write(f"ACTIVE_WORKSPACE_ID={self._active_workspace_id}\n")
            if self._active_workspace_name:
                f.write(f"ACTIVE_WORKSPACE_NAME={self._active_workspace_name}\n")

    @property
    def access_token(self) -> str | None:
        """Get the access token.

        Checks in order:
        1. LAZYCLOUD_API_KEY environment variable (for CI/CD)
        2. Stored access token from config file
        """
        # API key takes precedence (CI/CD use case)
        api_key = os.getenv("LAZYCLOUD_API_KEY")
        if api_key:
            return api_key

        if self._access_token:
            return self._access_token

        return None

    @property
    def refresh_token(self) -> str | None:
        """Get the refresh token."""
        return self._refresh_token

    def set_tokens(self, access_token: str, refresh_token: str | None = None):
        """Set the OAuth tokens"""
        self._access_token = access_token
        if refresh_token:
            self._refresh_token = refresh_token
        self._save_config()

    def clear_tokens(self):
        """Clear the OAuth tokens"""
        self._access_token = None
        self._refresh_token = None
        self._save_config()

    @property
    def api_url(self) -> str:
        """Get the full API URL"""
        return f"{self.api_base_url}/{self.api_version}"

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url(cls, v: str) -> str:
        """Validate the API base URL"""
        if not v.startswith(("http://", "https://")):
            raise ValueError("API base URL must start with http:// or https://")
        return v.rstrip("/")

    # Workspace management methods
    @property
    def active_workspace_id(self) -> str:
        """Get the currently active workspace ID.

        Can fall back to LAZYCLOUD_WORKSPACE_ID env var for CI/CD usage.
        """
        if self._active_workspace_id is not None:
            return self._active_workspace_id

        # Fall back to env var for CI/CD
        env_workspace_id = os.getenv("LAZYCLOUD_WORKSPACE_ID")
        if env_workspace_id:
            return env_workspace_id

        raise ValueError(
            "No active workspace ID found. Set LAZYCLOUD_WORKSPACE_ID or run 'lazycloud login'"
        )

    @property
    def active_workspace_name(self) -> str:
        """Get the currently active workspace name"""
        if self._active_workspace_name is None:
            raise ValueError("No active workspace name found")

        return self._active_workspace_name

    def set_active_workspace(self, workspace_id: str, workspace_name: str):
        """Set the active workspace"""
        self._active_workspace_id = workspace_id
        self._active_workspace_name = workspace_name
        self._save_config()

    def clear_active_workspace(self):
        """Clear the active workspace"""
        self._active_workspace_id = None
        self._active_workspace_name = None
        self._save_config()

    def check_authentication(self) -> tuple[bool, str]:
        """Check if user is properly authenticated and configured."""
        # Check for access token (stored or env var)
        if not self.access_token:
            return (
                False,
                "Not logged in. Please run 'lazycloud login' first or set LAZYCLOUD_ACCESS_TOKEN.",
            )

        # Check for workspace configuration (only required for stored tokens)
        # Env var usage (CI/CD) can skip workspace config
        if self._access_token and not self._active_workspace_id:
            return False, "No workspace configured. Please run 'lazycloud login' again."

        return True, ""


# Global config instance
config = CLIConfig()
