"""
Handle .lazycloud configuration files for deployments.
"""

import os
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class LazyCloudConfig(BaseModel):
    """Configuration stored in .lazycloud file."""

    deployment_name: str = Field(
        ...,
        description="Unique name for this deployment",
        pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        max_length=63,
    )
    compose_file: str = Field(
        default="docker-compose.yml",
        description="Path to the compose file relative to .lazycloud",
    )
    last_deployed: datetime | None = Field(
        None, description="Last deployment timestamp"
    )
    environment: str | None = Field(
        None, description="Environment name (e.g., production, staging)"
    )

    @field_validator("compose_file")
    @classmethod
    def validate_compose_file(cls, v: str) -> str:
        """Ensure compose file path is relative and safe."""
        if os.path.isabs(v):
            raise ValueError("Compose file path must be relative")
        if ".." in v:
            raise ValueError("Compose file path cannot contain '..'")
        return v


class LazyCloudFile:
    """Manage .lazycloud configuration files."""

    DEFAULT_FILENAME = ".lazycloud"

    def __init__(self, directory: Path):
        """Initialize with a directory path."""
        self.directory = directory or Path.cwd()

    @property
    def file_path(self) -> Path:
        """Get the full path to the .lazycloud file."""
        return self.directory / self.DEFAULT_FILENAME

    def exists(self) -> bool:
        """Check if .lazycloud file exists."""
        return self.file_path.exists()

    def read(self) -> LazyCloudConfig:
        """Read and parse the .lazycloud file."""
        if not self.exists():
            raise FileNotFoundError(
                f"No {self.DEFAULT_FILENAME} file found in {self.directory}. "
                "Run 'lazycloud compose init' to create one."
            )

        with open(self.file_path, "r") as f:
            data = yaml.safe_load(f)

        return LazyCloudConfig(**data)

    def write(self, config: LazyCloudConfig) -> None:
        """Write configuration to .lazycloud file."""
        # Convert to dict and handle datetime serialization
        data = config.model_dump(exclude_none=True)
        if "last_deployed" in data and data["last_deployed"]:
            data["last_deployed"] = data["last_deployed"].isoformat()

        with open(self.file_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def update(self, **kwargs) -> LazyCloudConfig:
        """Update specific fields in the config."""
        if self.exists():
            config = self.read()
            # Update fields
            for key, value in kwargs.items():
                if hasattr(config, key):
                    setattr(config, key, value)
        else:
            # Create new config with provided values
            config = LazyCloudConfig(**kwargs)

        self.write(config)
        return config

    def find_in_parents(self) -> Path | None:
        """
        Search for .lazycloud file in current directory and parents.
        Returns the directory containing the file, or None if not found.
        """
        current = self.directory.resolve()

        while current != current.parent:
            if (current / self.DEFAULT_FILENAME).exists():
                return current
            current = current.parent

        return None

    @classmethod
    def find_and_load(cls, start_dir: Path) -> "LazyCloudFile | None":
        """
        Find .lazycloud file in current or parent directories and load it.
        Returns LazyCloudFile instance or None if not found.
        """
        searcher = cls(start_dir)
        found_dir = searcher.find_in_parents()

        if found_dir:
            return cls(found_dir)
        return None

    def get_compose_file_path(self) -> Path:
        """Get the full path to the compose file."""
        config = self.read()
        return self.directory / config.compose_file

    def validate_compose_file_exists(self) -> bool:
        """Check if the referenced compose file exists."""
        compose_path = self.get_compose_file_path()
        return compose_path.exists()
