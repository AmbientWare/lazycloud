from typing import Any

from pydantic import BaseModel

from shared.models.billing import STORAGE_CLASS_TO_TYPE


class FieldChange(BaseModel):
    """Represents a change to a single field."""

    model_config = {"ser_json_bytes": "utf8"}

    from_value: Any = None
    to_value: Any = None


ResourceDict = dict[str, str | int | list | dict]
ModificationDict = dict[str, FieldChange | dict[str, FieldChange]]


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
    user_managed: list[str] = []


class StorageTypeChange(BaseModel):
    """Represents a volume storage type change that requires user confirmation."""

    volume_name: str
    old_storage_class: str
    new_storage_class: str
    reason: str

    @property
    def old_type(self) -> str:
        """Get the display name for the old storage type."""

        return STORAGE_CLASS_TO_TYPE.get(self.old_storage_class, self.old_storage_class)

    @property
    def new_type(self) -> str:
        """Get the display name for the new storage type."""

        return STORAGE_CLASS_TO_TYPE.get(self.new_storage_class, self.new_storage_class)
