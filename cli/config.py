from pathlib import Path
from typing import Optional, Dict
from pydantic import BaseModel, Field, field_validator, PrivateAttr
import os


class CLIConfig(BaseModel):
    """CLI Configuration"""

    # API Configuration
    api_base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL for the API",
        validation_alias="MACHINES_API_BASE_URL",
    )
    api_version: str = Field(
        default="v1", description="API version", validation_alias="MACHINES_API_VERSION"
    )

    # SSH Configuration
    ssh_config_path: str = Field(
        default=str(Path.home() / ".ssh" / "config"),
        description="Path to SSH config file",
        validation_alias="MACHINES_SSH_CONFIG_PATH",
    )
    default_ssh_key_path: str = Field(
        default=str(Path.home() / ".ssh" / "id_rsa.pub"),
        description="Path to default SSH public key",
        validation_alias="MACHINES_DEFAULT_SSH_KEY_PATH",
    )

    # Private attributes for token management
    _tokens: Dict[str, str] = PrivateAttr(default_factory=dict)
    _active_token: Optional[str] = PrivateAttr(default=None)

    def __init__(self, **data):
        super().__init__(**data)
        self._load_tokens()

    def _load_tokens(self):
        """Load tokens from the config file"""
        config_path = Path.home() / ".machines"
        if config_path.exists():
            with open(config_path) as f:
                for line in f:
                    if line.startswith("TOKEN_"):
                        key, value = line.strip().split("=", 1)
                        token_name = key.replace("TOKEN_", "").lower()
                        self._tokens[token_name] = value
                    elif line.startswith("ACTIVE_TOKEN="):
                        self._active_token = line.strip().split("=", 1)[1]

    def _save_tokens(self):
        """Save tokens to the config file"""
        config_path = Path.home() / ".machines"
        with open(config_path, "w") as f:
            for token_name, value in self._tokens.items():
                f.write(f"TOKEN_{token_name.upper()}={value}\n")
            if self._active_token:
                f.write(f"ACTIVE_TOKEN={self._active_token}\n")

    @property
    def active_token(self) -> Optional[str]:
        """Get the currently active token"""
        return self._active_token

    @active_token.setter
    def active_token(self, value: Optional[str]):
        """Set the active token"""
        if value is not None and value not in self._tokens:
            raise ValueError(f"Token {value} does not exist")
        self._active_token = value
        self._save_tokens()

    def add_token(self, name: str, value: str):
        """Add a new token"""
        self._tokens[name.lower()] = value
        if not self._active_token:
            self._active_token = name.lower()
        self._save_tokens()

    def remove_token(self, name: str):
        """Remove a token"""
        name = name.lower()
        if name not in self._tokens:
            raise ValueError(f"Token {name} does not exist")

        del self._tokens[name]
        if self._active_token == name:
            self._active_token = next(iter(self._tokens.keys()), None)
        self._save_tokens()

    def get_token(self, name: Optional[str] = None) -> Optional[str]:
        """Get a token value by name. If no name is provided, returns the active token."""
        if name is None:
            return self._tokens.get(self._active_token) if self._active_token else None
        return self._tokens.get(name.lower())

    def list_tokens(self) -> Dict[str, str]:
        """List all tokens"""
        return self._tokens.copy()

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
