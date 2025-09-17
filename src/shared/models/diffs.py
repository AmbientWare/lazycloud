from typing import Any, Dict, List, Optional

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

    services: List[Dict[str, Any]] = []
    volumes: List[Dict[str, Any]] = []
    networks: List[Dict[str, Any]] = []


class ModifiedSection(BaseModel):
    """Modified resources with their changes."""

    services: Dict[str, Dict[str, Any]] = {}
    volumes: Dict[str, Dict[str, Any]] = {}
    networks: Dict[str, Dict[str, Any]] = {}


class ComposeDiff(BaseModel):
    """Structured diff between two compose files."""

    added: Optional[ResourceSection] = None
    modified: Optional[ModifiedSection] = None
    removed: Optional[ResourceSection] = None

    def has_changes(self) -> bool:
        """Check if there are any changes."""
        if self.added or self.modified or self.removed:
            return True

        return False
