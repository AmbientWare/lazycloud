from pathlib import Path
from typing import Optional, Dict
from pydantic import Field, field_validator, PrivateAttr
import os
from pydantic_settings import BaseSettings


class CLIConfig(BaseSettings):
    """CLI Configuration"""

    # API Configuration
    api_base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL for the API",
    )
    api_version: str = Field(
        default="v1",
        description="API version"
    )

    # SSH Configuration
    ssh_config_path: str = Field(
        default=str(Path.home() / ".ssh" / "config"),
        description="Path to SSH config file",
    )
    default_ssh_key_path: str = Field(
        default=str(Path.home() / ".ssh" / "id_rsa.pub"),
        description="Path to default SSH public key",
    )

    # Private attributes for api key management
    _api_keys: Dict[str, str] = PrivateAttr(default_factory=dict)
    _active_api_key: Optional[str] = PrivateAttr(default=None)

    class Config:
        env_prefix = "MACHINES_"

    def __init__(self, **data):
        super().__init__(**data)
        self._load_api_keys()

    def _load_api_keys(self):
        """Load api keys from the config file"""
        config_path = Path.home() / ".machines"
        if config_path.exists():
            with open(config_path) as f:
                for line in f:
                    if line.startswith("API_KEY_"):
                        key, value = line.strip().split("=", 1)
                        api_key_name = key.replace("API_KEY_", "").lower()
                        self._api_keys[api_key_name] = value
                    elif line.startswith("ACTIVE_API_KEY="):
                        self._active_api_key = line.strip().split("=", 1)[1]

    def _save_api_keys(self):
        """Save api keys to the config file"""
        config_path = Path.home() / ".machines"
        with open(config_path, "w") as f:
            for api_key_name, value in self._api_keys.items():
                f.write(f"API_KEY_{api_key_name.upper()}={value}\n")
            if self._active_api_key:
                f.write(f"ACTIVE_API_KEY={self._active_api_key}\n")

    @property
    def active_api_key(self) -> Optional[str]:
        """Get the currently active api key"""
        return self._active_api_key

    @property
    def active_api_key_value(self) -> Optional[str]:
        """Get the value of the currently active api key"""
        if not self._active_api_key:
            raise ValueError("No active api key found")

        value = self._api_keys.get(self._active_api_key)
        if value is None:
            raise ValueError("No active api key found")

        return value

    @active_api_key.setter
    def active_api_key(self, value: Optional[str]):
        """Set the active api key"""
        if value is not None and value not in self._api_keys:
            raise ValueError(f"Api key {value} does not exist")
        self._active_api_key = value
        self._save_api_keys()

    def add_api_key(self, name: str, value: str):
        """Add a new api key"""
        name = name.lower()
        self._api_keys[name] = value
        # Only set as active if it's the first key
        if not self._active_api_key:
            self._active_api_key = name
        self._save_api_keys()

    def remove_api_key(self, name: str):
        """Remove an api key"""
        name = name.lower()
        if name not in self._api_keys:
            raise ValueError(f"Api key {name} does not exist")

        del self._api_keys[name]
        # if the active key is being removed, set the active key to the next key
        if self._active_api_key == name:
            self._active_api_key = next(iter(self._api_keys.keys()), None)
        self._save_api_keys()

    def get_api_key(self, name: Optional[str] = None) -> Optional[str]:
        """Get an api key value by name. If no name is provided, returns the active api key."""
        if name is None:
            return (
                self._api_keys.get(self._active_api_key)
                if self._active_api_key
                else None
            )
        return self._api_keys.get(name.lower())

    def list_api_keys(self) -> Dict[str, str]:
        """List all api keys"""
        return self._api_keys.copy()

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

    @field_validator("ssh_config_path", "default_ssh_key_path")
    @classmethod
    def validate_path_exists(cls, v: str) -> str:
        """Validate that the path exists"""
        if not os.path.exists(v):
            raise ValueError(f"Path does not exist: {v}")
        return v


# Global config instance
config = CLIConfig()
