from typing import Any, Dict, Union

from pydantic import BaseModel


class FieldChange(BaseModel):
    """Represents a change to a single field."""

    from_value: Any = None
    to_value: Any = None

    class Config:
        json_encoders = {object: str}


ResourceDict = Dict[str, Union[str, int, list, dict]]
ModificationDict = Dict[str, Union[FieldChange, Dict[str, FieldChange]]]


class ResourceSection(BaseModel):
    """Changes to a specific type of resource (services, volumes, or networks)."""

    added: list[ResourceDict] = []
    modified: dict[str, ModificationDict] = {}
    removed: list[ResourceDict] = []

    def has_changes(self) -> bool:
        """Check if there are any changes in this section."""
        return bool(self.added or self.modified or self.removed)


class ComposeDiff(BaseModel):
    """Structured diff between two compose files."""

    services: ResourceSection = ResourceSection()
    volumes: ResourceSection = ResourceSection()
    networks: ResourceSection = ResourceSection()

    def has_changes(self) -> bool:
        """Check if there are any changes."""
        return (
            self.services.has_changes()
            or self.volumes.has_changes()
            or self.networks.has_changes()
        )

    def summary(self) -> dict[str, dict[str, int]]:
        """Get a summary of changes."""
        return {
            "services": {
                "added": len(self.services.added),
                "modified": len(self.services.modified),
                "removed": len(self.services.removed),
            },
            "volumes": {
                "added": len(self.volumes.added),
                "modified": len(self.volumes.modified),
                "removed": len(self.volumes.removed),
            },
            "networks": {
                "added": len(self.networks.added),
                "modified": len(self.networks.modified),
                "removed": len(self.networks.removed),
            },
        }


class EnvVarChanges(BaseModel):
    """Changes to environment variables."""

    added: list[str] = []
    removed: list[str] = []
    existing: list[str] = []
