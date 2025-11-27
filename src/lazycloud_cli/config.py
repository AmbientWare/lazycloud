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

    # Private attributes for api key management
    _api_key: str | None = PrivateAttr(default=None)

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
                    if line.startswith("API_KEY="):
                        self._api_key = line.strip().split("=", 1)[1]
                    elif line.startswith("ACTIVE_WORKSPACE_ID="):
                        self._active_workspace_id = line.strip().split("=", 1)[1]
                    elif line.startswith("ACTIVE_WORKSPACE_NAME="):
                        self._active_workspace_name = line.strip().split("=", 1)[1]

    def _save_config(self):
        """Save configuration to the config file"""
        config_path = Path.home() / ".lazycloud"
        with open(config_path, "w") as f:
            if self._api_key:
                f.write(f"API_KEY={self._api_key}\n")
            if self._active_workspace_id:
                f.write(f"ACTIVE_WORKSPACE_ID={self._active_workspace_id}\n")
            if self._active_workspace_name:
                f.write(f"ACTIVE_WORKSPACE_NAME={self._active_workspace_name}\n")

    @property
    def api_key(self) -> str | None:
        """Get the API key.

        Checks in order:
        1. Stored API key from config file
        2. LAZYCLOUD_API_KEY environment variable
        """
        if self._api_key:
            return self._api_key

        env_key = os.getenv("LAZYCLOUD_API_KEY")
        if env_key:
            return env_key

        return None

    def set_api_key(self, value: str):
        """Set the API key"""
        self._api_key = value
        self._save_config()

    def clear_api_key(self):
        """Clear the API key"""
        self._api_key = None
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
        # Check for API key (stored or env var)
        if not self.api_key:
            return (
                False,
                "Not logged in. Please run 'lazycloud login' first or set LAZYCLOUD_API_KEY.",
            )

        # Check for workspace configuration (only required for stored keys)
        # Env var usage (CI/CD) can skip workspace config
        if self._api_key and not self._active_workspace_id:
            return False, "No workspace configured. Please run 'lazycloud login' again."

        return True, ""


# Global config instance
config = CLIConfig()
