from loguru import logger
from pydantic import BaseModel

from shared.models.compose import (
    ComposeFile,
    ComposeNetwork,
    ComposePort,
    ComposeService,
    ComposeVolume,
    ServiceVolume,
)
from shared.models.diffs import (
    ComposeDiff,
    FieldChange,
    ModificationDict,
    ResourceDict,
    ResourceSection,
)


class ComposeDiffChecker:
    """Compares two Docker Compose files with compose-aware logic."""

    def compare_compose_files(
        self, current: ComposeFile | None, new: ComposeFile
    ) -> ComposeDiff:
        """Compare two compose files and return structured differences."""
        if not current:
            logger.info("No current compose file found, everything is new")
            return ComposeDiff(
                services=ResourceSection(
                    added=[self._service_to_dict(s) for s in new.services],
                    modified={},
                    removed=[],
                ),
                volumes=ResourceSection(
                    added=[self._volume_to_dict(v) for v in new.volumes],
                    modified={},
                    removed=[],
                ),
                networks=ResourceSection(
                    added=[self._network_to_dict(n) for n in new.networks],
                    modified={},
                    removed=[],
                ),
            )

        logger.info("Comparing compose files...")
        diff = ComposeDiff(
            services=self._compare_services(current.services, new.services),
            volumes=self._compare_volumes(current.volumes, new.volumes),
            networks=self._compare_networks(current.networks, new.networks),
        )

        return diff

    def _compare_services(
        self, current: list[ComposeService], new: list[ComposeService]
    ) -> ResourceSection:
        """Compare service lists."""
        logger.info("Comparing services...")
        current_by_name = {s.name: s for s in current}
        new_by_name = {s.name: s for s in new}

        # Find added services
        added = [
            self._service_to_dict(new_by_name[name])
            for name in new_by_name
            if name not in current_by_name
        ]

        # Find removed services
        removed = [
            self._service_to_dict(current_by_name[name])
            for name in current_by_name
            if name not in new_by_name
        ]

        # Find modified services
        modified = {}
        for name in current_by_name:
            if name in new_by_name:
                changes = self._compare_service(
                    current_by_name[name], new_by_name[name]
                )
                if changes:
                    modified[name] = changes

        logger.info(
            f"Service comparison results: Added: {added}, Modified: {modified}, Removed: {removed}"
        )
        return ResourceSection(added=added, modified=modified, removed=removed)

    def _compare_service(
        self, current: ComposeService, new: ComposeService
    ) -> ModificationDict | None:
        """Compare two services and return all changes."""
        changes = {}

        # Compare simple fields
        simple_fields = ["image", "command", "entrypoint", "working_dir", "user"]
        for field in simple_fields:
            current_val = getattr(current, field, None)
            new_val = getattr(new, field, None)
            if current_val != new_val:
                changes[field] = FieldChange(
                    from_value=current_val,
                    to_value=new_val,
                )

        # Compare lists with custom serialization
        if current.ports != new.ports:
            changes["ports"] = FieldChange(
                from_value=[self._port_to_string(p) for p in (current.ports or [])],
                to_value=[self._port_to_string(p) for p in (new.ports or [])],
            )

        if current.volumes != new.volumes:
            changes["volumes"] = FieldChange(
                from_value=[self._volume_to_string(v) for v in (current.volumes or [])],
                to_value=[self._volume_to_string(v) for v in (new.volumes or [])],
            )

        # Compare networks
        if current.networks != new.networks:
            changes["networks"] = FieldChange(
                from_value=current.networks or [],
                to_value=new.networks or [],
            )

        # Compare nested models - show all changes including defaults
        if current.deploy != new.deploy:
            deploy_changes = self._compare_models(current.deploy, new.deploy, include_defaults=True)
            if deploy_changes:
                changes["deploy"] = deploy_changes

        if current.healthcheck != new.healthcheck:
            health_changes = self._compare_models(current.healthcheck, new.healthcheck)
            if health_changes:
                changes["healthcheck"] = health_changes

        if current.scaling != new.scaling:
            scaling_changes = self._compare_models(current.scaling, new.scaling)
            if scaling_changes:
                changes["scaling"] = scaling_changes

        return changes if changes else None

    def _compare_models(
        self, current: BaseModel | None, new: BaseModel | None, include_defaults: bool = False
    ) -> ModificationDict | None:
        """Generic comparison for Pydantic models.

        Args:
            current: Current model state
            new: New model state
            include_defaults: If True, show changes even for default values
        """
        # Both None - no changes
        if current is None and new is None:
            return None

        # One is None - model added or removed
        if current is None:
            if new:
                # Model added - show what was added
                new_dict = new.model_dump(exclude_defaults=not include_defaults, exclude_none=True)
                if new_dict or include_defaults:
                    return FieldChange(from_value=None, to_value=new_dict)
            return None

        if new is None:
            if current:
                # Model removed - show what was removed
                current_dict = current.model_dump(exclude_defaults=not include_defaults, exclude_none=True)
                if current_dict or include_defaults:
                    return FieldChange(from_value=current_dict, to_value=None)
            return None

        # Both exist - compare all fields
        current_dict = current.model_dump(exclude_none=True)
        new_dict = new.model_dump(exclude_none=True)

        changes = {}
        all_keys = set(current_dict.keys()) | set(new_dict.keys())

        for key in all_keys:
            current_val = current_dict.get(key)
            new_val = new_dict.get(key)

            if current_val != new_val:
                if isinstance(current_val, dict) and isinstance(new_val, dict):
                    nested_changes = self._compare_dicts(current_val, new_val)
                    if nested_changes:
                        changes[key] = nested_changes
                else:
                    changes[key] = FieldChange(from_value=current_val, to_value=new_val)

        return changes if changes else None

    def _compare_dicts(self, current: dict, new: dict) -> ModificationDict | None:
        """Compare two dictionaries recursively."""
        changes = {}

        all_keys = set(current.keys()) | set(new.keys())
        for key in all_keys:
            current_val = current.get(key)
            new_val = new.get(key)

            if current_val == new_val:
                continue

            if isinstance(current_val, dict) and isinstance(new_val, dict):
                nested_changes = self._compare_dicts(current_val, new_val)
                if nested_changes:
                    changes[key] = nested_changes
            else:
                changes[key] = FieldChange(from_value=current_val, to_value=new_val)

        return changes if changes else None

    def _compare_volumes(
        self, current: list[ComposeVolume], new: list[ComposeVolume]
    ) -> ResourceSection:
        """Compare volume lists."""
        logger.info("Comparing volumes...")
        current_names = {v.name for v in current}
        new_names = {v.name for v in new}

        added = [self._volume_to_dict(v) for v in new if v.name not in current_names]
        removed = [self._volume_to_dict(v) for v in current if v.name not in new_names]

        # Volumes typically don't have properties to modify
        modified = {}
        logger.info(
            f"Volume comparison results: Added: {added}, Modified: {modified}, Removed: {removed}"
        )

        return ResourceSection(added=added, modified=modified, removed=removed)

    def _compare_networks(
        self, current: list[ComposeNetwork], new: list[ComposeNetwork]
    ) -> ResourceSection:
        """Compare network lists."""
        logger.info("Comparing networks...")
        current_names = {n.name for n in current}
        new_names = {n.name for n in new}

        added = [self._network_to_dict(n) for n in new if n.name not in current_names]
        removed = [self._network_to_dict(n) for n in current if n.name not in new_names]

        # Networks typically don't have properties to modify
        modified = {}
        logger.info(
            f"Network comparison results: Added: {added}, Modified: {modified}, Removed: {removed}"
        )

        return ResourceSection(added=added, modified=modified, removed=removed)

    # Helper methods to convert models to dicts for display

    def _service_to_dict(self, service: ComposeService) -> ResourceDict:
        """Convert service to dict for display."""
        data = service.model_dump(exclude_none=True, exclude_defaults=True)

        # Always include name and image
        result = {"name": service.name, "image": service.image}

        # Add other fields if they're set
        if "command" in data:
            result["command"] = data["command"]

        if service.ports:
            result["ports"] = [self._port_to_string(p) for p in service.ports]

        if service.volumes:
            result["volumes"] = [self._volume_to_string(v) for v in service.volumes]

        # Only show replicas if not 1
        if data.get("deploy", {}).get("replicas", 1) != 1:
            result["replicas"] = data["deploy"]["replicas"]

        return result

    def _volume_to_dict(self, volume: ComposeVolume) -> ResourceDict:
        """Convert volume to dict for display."""
        return volume.model_dump(exclude_none=True, exclude_defaults=True)

    def _network_to_dict(self, network: ComposeNetwork) -> ResourceDict:
        """Convert network to dict for display."""
        return network.model_dump(exclude_none=True, exclude_defaults=True)

    def _port_to_string(self, port: ComposePort) -> str:
        """Convert port to string representation."""
        if port.published == port.target:
            return f"{port.target}/{port.protocol}"
        return f"{port.published}:{port.target}/{port.protocol}"

    def _volume_to_string(self, volume: ServiceVolume) -> str:
        """Convert volume to string representation."""
        if not volume.target:
            return volume.source or ""

        # Format as source:target or just target
        if volume.source:
            result = f"{volume.source}:{volume.target}"
        else:
            result = volume.target

        # Add read-only flag if set
        if volume.read_only:
            result += ":ro"

        return result
