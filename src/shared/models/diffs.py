from typing import Any

from pydantic import BaseModel


class EnvVarChanges(BaseModel):
    """Changes to environment variables."""

    added: list[str] = []
    removed: list[str] = []
    existing: list[str] = []


class FieldChange(BaseModel):
    """Represents a change to a single field."""

    from_value: Any = None
    to_value: Any = None

    class Config:
        json_encoders = {
            object: str  # Fallback for any non-serializable objects
        }


class ResourceSection(BaseModel):
    """Resources in a section (services, volumes, networks)."""

    services: list[dict[str, Any]] = []
    volumes: list[dict[str, Any]] = []
    networks: list[dict[str, Any]] = []


class ModifiedSection(BaseModel):
    """Modified resources with their changes."""

    services: dict[str, dict[str, Any]] = {}
    volumes: dict[str, dict[str, Any]] = {}
    networks: dict[str, dict[str, Any]] = {}


class ComposeDiff(BaseModel):
    """Structured diff between two compose files."""

    added: ResourceSection | None = None
    modified: ModifiedSection | None = None
    removed: ResourceSection | None = None

    def has_changes(self) -> bool:
        """Check if there are any changes."""
        if self.added or self.modified or self.removed:
            return True

        return False
