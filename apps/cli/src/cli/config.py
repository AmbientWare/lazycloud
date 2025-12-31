import os
import tomllib
from pathlib import Path

import platformdirs
from pydantic import PrivateAttr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Platform-based config directory:
# - Linux: ~/.config/lazycloud/
# - macOS: ~/Library/Application Support/lazycloud/
# - Windows: C:\Users\<user>\AppData\Roaming\lazycloud\
CONFIG_DIR = Path(platformdirs.user_config_dir("lazycloud"))
CONFIG_FILE = CONFIG_DIR / "config.toml"


class CLIConfig(BaseSettings):
    """CLI Configuration

    Priority (highest wins):
    1. Environment variables (LAZYCLOUD_API_BASE_URL, etc.)
    2. Config file (~/.config/lazycloud/config.toml)
    3. Defaults

    Example config.toml:
        [api]
        base_url = "http://localhost:8000"
        version = "v1"
    """

    model_config = SettingsConfigDict(env_prefix="LAZYCLOUD_")

    # API Configuration
    api_base_url: str = "https://api.lazycloud.dev"
    api_version: str = "v1"

    # Private attributes for OAuth token management
    _access_token: str | None = PrivateAttr(default=None)
    _refresh_token: str | None = PrivateAttr(default=None)

    # Private attributes for workspace management
    _active_workspace_id: str | None = PrivateAttr(default=None)
    _active_workspace_name: str | None = PrivateAttr(default=None)

    def __init__(self, **data):
        # Load config file values, env vars override via pydantic-settings
        file_config = self._read_config_file()

        # Apply file config for fields not set via env vars
        for key in ["api_base_url", "api_version"]:
            env_key = f"LAZYCLOUD_{key.upper()}"
            if key not in data and env_key not in os.environ:
                if value := file_config.get("api", {}).get(key.replace("api_", "")):
                    data[key] = value

        super().__init__(**data)

        # Load auth tokens from file
        auth = file_config.get("auth", {})
        self._access_token = auth.get("access_token")
        self._refresh_token = auth.get("refresh_token")

        # Load workspace from file
        workspace = file_config.get("workspace", {})
        self._active_workspace_id = workspace.get("id")
        self._active_workspace_name = workspace.get("name")

    @staticmethod
    def _read_config_file() -> dict:
        """Read config from TOML file."""
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "rb") as f:
                return tomllib.load(f)
        return {}

    def _save_config(self):
        """Save configuration to TOML file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        lines = [
            "[api]",
            f'base_url = "{self.api_base_url}"',
            f'version = "{self.api_version}"',
            "",
            "[auth]",
        ]

        if self._access_token:
            lines.append(f'access_token = "{self._access_token}"')
        if self._refresh_token:
            lines.append(f'refresh_token = "{self._refresh_token}"')

        lines.append("")
        lines.append("[workspace]")

        if self._active_workspace_id:
            lines.append(f'id = "{self._active_workspace_id}"')
        if self._active_workspace_name:
            lines.append(f'name = "{self._active_workspace_name}"')

        with open(CONFIG_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")

    @property
    def access_token(self) -> str | None:
        """Get the access token.

        Checks in order:
        1. LAZYCLOUD_API_KEY environment variable (for CI/CD)
        2. Stored access token from config file
        """
        if api_key := os.getenv("LAZYCLOUD_API_KEY"):
            return api_key
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        """Get the refresh token."""
        return self._refresh_token

    def set_tokens(self, access_token: str, refresh_token: str | None = None):
        """Set the OAuth tokens."""
        self._access_token = access_token
        if refresh_token:
            self._refresh_token = refresh_token
        self._save_config()

    def clear_tokens(self):
        """Clear the OAuth tokens."""
        self._access_token = None
        self._refresh_token = None
        self._save_config()

    @property
    def api_url(self) -> str:
        """Get the full API URL."""
        return f"{self.api_base_url}/{self.api_version}"

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url(cls, v: str) -> str:
        """Validate the API base URL."""
        if not v.startswith(("http://", "https://")):
            raise ValueError("API base URL must start with http:// or https://")
        return v.rstrip("/")

    @property
    def active_workspace_id(self) -> str:
        """Get the currently active workspace ID."""
        if self._active_workspace_id is not None:
            return self._active_workspace_id

        if env_id := os.getenv("LAZYCLOUD_WORKSPACE_ID"):
            return env_id

        raise ValueError(
            "No active workspace ID found. Set LAZYCLOUD_WORKSPACE_ID or run 'lazycloud login'"
        )

    @property
    def active_workspace_name(self) -> str:
        """Get the currently active workspace name."""
        if self._active_workspace_name is None:
            raise ValueError("No active workspace name found")
        return self._active_workspace_name

    def set_active_workspace(self, workspace_id: str, workspace_name: str):
        """Set the active workspace."""
        self._active_workspace_id = workspace_id
        self._active_workspace_name = workspace_name
        self._save_config()

    def clear_active_workspace(self):
        """Clear the active workspace."""
        self._active_workspace_id = None
        self._active_workspace_name = None
        self._save_config()

    def check_authentication(self) -> tuple[bool, str]:
        """Check if user is properly authenticated and configured."""
        if not self.access_token:
            return (
                False,
                "Not logged in. Please run 'lazycloud login' first or set LAZYCLOUD_ACCESS_TOKEN.",
            )

        if self._access_token and not self._active_workspace_id:
            return False, "No workspace configured. Please run 'lazycloud login' again."

        return True, ""


# Global config instance
config = CLIConfig()
