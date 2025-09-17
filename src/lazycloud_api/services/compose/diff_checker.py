from typing import Any

from jsondiff import diff

from shared.models.compose import (
    ComposeFile,
    ComposeNetwork,
    ComposeService,
    ComposeVolume,
)
from shared.models.diffs import ComposeDiff, ModifiedSection, ResourceSection


class ComposeDiffChecker:
    """Compares two Docker Compose files with model-aware formatting."""

    # Map resource types to their model classes
    RESOURCE_MODELS = {
        "services": ComposeService,
        "volumes": ComposeVolume,
        "networks": ComposeNetwork,
    }

    # Fields to exclude from diffs (internal/metadata)
    EXCLUDE_FIELDS = {"user_id"}

    # Fields that should be formatted specially
    SPECIAL_FORMAT_FIELDS = {
        "ports": "port_list",
        "volumes": "volume_list",
        "environment": "env_dict",
        "labels": "label_dict",
        "deploy": "deploy_config",
        "scaling": "scaling_config",
    }

    def compare_compose_files(
        self, current: ComposeFile | None, new: ComposeFile
    ) -> ComposeDiff:
        """Compare two compose files and return structured differences."""
        new_dict = new.model_dump(exclude_none=True)

        current_dict = (
            current.model_dump(exclude_none=True)
            if current
            else ComposeFile().model_dump(exclude_none=True)
        )

        raw_diff = diff(current_dict, new_dict, syntax="symmetric", marshal=True)

        return self._structure_diff(current_dict, new_dict, raw_diff)

    def _structure_diff(
        self, current_dict: dict, new_dict: dict, raw_diff: dict
    ) -> ComposeDiff:
        """Convert jsondiff output to structured format."""
        result = ComposeDiff()

        for resource_type in self.RESOURCE_MODELS.keys():
            if resource_type in raw_diff:
                added, modified, removed = self._process_resource_type(
                    resource_type,
                    current_dict.get(resource_type, []),
                    raw_diff[resource_type],
                )

                if added:
                    if not result.added:
                        result.added = ResourceSection()
                    setattr(result.added, resource_type, added)

                if modified:
                    if not result.modified:
                        result.modified = ModifiedSection()
                    setattr(result.modified, resource_type, modified)

                if removed:
                    if not result.removed:
                        result.removed = ResourceSection()
                    setattr(result.removed, resource_type, removed)

        return result

    def _process_resource_type(
        self,
        resource_type: str,
        current_list: list[dict],
        changes: dict,
    ) -> tuple[list, dict, list]:
        """Process changes for a specific resource type."""
        added = []
        modified = {}
        removed = []

        # Handle additions
        if "$insert" in changes:
            for _, resource in changes["$insert"]:
                # Ensure resource is a dict
                if isinstance(resource, dict):
                    added.append(self._clean_resource(resource, resource_type))
                else:
                    # If it's not a dict, skip it
                    continue

        # Handle removals
        if "$delete" in changes:
            for _, resource in changes["$delete"]:
                # Ensure resource is a dict
                if isinstance(resource, dict):
                    removed.append(self._clean_resource(resource, resource_type))
                else:
                    # If it's not a dict, skip it
                    continue

        # Handle modifications
        for key, value in changes.items():
            if isinstance(key, int) and isinstance(value, dict):
                if 0 <= key < len(current_list):
                    resource_name = current_list[key].get(
                        "name", f"{resource_type}[{key}]"
                    )
                    formatted = self._format_changes(value, resource_type)
                    if formatted:
                        modified[resource_name] = formatted

        return added, modified, removed

    def _clean_resource(self, resource: dict, resource_type: str) -> dict:
        """Clean resource dict using model knowledge."""
        cleaned = {}

        # Always include the name for identification
        if "name" in resource:
            cleaned["name"] = resource["name"]

        for key, value in resource.items():
            if key not in self.EXCLUDE_FIELDS:
                # Apply field-specific formatting
                if key in self.SPECIAL_FORMAT_FIELDS:
                    cleaned[key] = self._format_field(key, value)
                else:
                    cleaned[key] = value

        return cleaned

    def _format_changes(self, changes: dict, resource_type: str) -> dict:
        """Format changes for a single resource."""
        formatted = {}

        for field, value in changes.items():
            if field in self.EXCLUDE_FIELDS:
                continue

            if field.startswith("$"):
                # Handle jsondiff operations
                if field == "$insert":
                    for k, v in value.items():
                        formatted[k] = {"from": None, "to": self._format_field(k, v)}

                elif field == "$delete":
                    for k, v in value.items():
                        formatted[k] = {"from": self._format_field(k, v), "to": None}

            elif isinstance(value, list) and len(value) == 2:
                # [old, new] format
                formatted[field] = {
                    "from": self._format_field(field, value[0]),
                    "to": self._format_field(field, value[1]),
                }

            elif isinstance(value, dict):
                # Nested changes
                nested = self._format_changes(value, resource_type)
                if nested:
                    formatted[field] = nested

        return formatted

    def _format_field(self, field_name: str, value: Any) -> Any:
        """Format a field value based on its type."""
        if value is None:
            return None

        format_type = self.SPECIAL_FORMAT_FIELDS.get(field_name)

        if format_type == "port_list" and isinstance(value, list):
            # Format port mappings
            return [self._format_port(p) if isinstance(p, dict) else p for p in value]

        elif format_type == "volume_list" and isinstance(value, list):
            # Format volume mappings
            return [self._format_volume(v) if isinstance(v, dict) else v for v in value]

        elif format_type == "deploy_config" and isinstance(value, dict):
            # Simplify deploy config
            return self._format_deploy(value)

        elif format_type == "scaling_config" and isinstance(value, dict):
            # Format scaling config
            return self._format_scaling(value)

        return value

    def _format_port(self, port: dict) -> str:
        """Format port mapping."""
        published = port.get("published", port.get("published", "?"))
        target = port.get("target", port.get("port", "?"))
        protocol = port.get("protocol", "tcp")

        if protocol == "tcp":
            return f"{published}:{target}"
        return f"{published}:{target}/{protocol}"

    def _format_volume(self, volume: dict) -> str:
        """Format volume mapping."""
        source = volume.get("source", "")
        target = volume.get("target", "?")
        read_only = volume.get("read_only", False)
        vol_type = volume.get("type", "volume")

        if vol_type == "bind" and source:
            base = f"{source}:{target}"
        elif vol_type == "volume" and source:
            base = f"{source}:{target}"
        else:
            base = target

        if read_only:
            return f"{base}:ro"
        return base

    def _format_deploy(self, deploy: dict) -> dict:
        """Simplify deploy config to important fields."""
        result = {}

        if "replicas" in deploy:
            result["replicas"] = deploy["replicas"]

        if "resources" in deploy:
            resources = deploy["resources"]
            if "limits" in resources:
                result["limits"] = resources["limits"]
            if "reservations" in resources:
                result["reservations"] = resources["reservations"]

        return result if result else deploy

    def _format_scaling(self, scaling: dict) -> dict:
        """Format scaling config."""
        result = {}

        # Only include the important fields
        important_fields = ["enabled", "min", "max", "cpu", "memory"]
        for field in important_fields:
            if field in scaling:
                result[field] = scaling[field]

        return result if result else scaling
