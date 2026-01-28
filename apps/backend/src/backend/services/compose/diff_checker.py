from loguru import logger
from models.compose import (
    ComposeFile,
    ComposeNetwork,
    ComposePort,
    ComposeService,
    ComposeVolume,
    LazyCloudLabel,
    ServiceVolume,
)
from models.diffs import (
    ComposeDiff,
    FieldChange,
    ModificationDict,
    ResourceDict,
    ResourceSection,
    StorageTypeChange,
)
from models.storage import STORAGE_CLASS_SHARED, STORAGE_CLASS_STANDARD
from pydantic import BaseModel


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
            f"Service comparison results: Added: {len(added)}, Modified: {len(modified)}, Removed: {len(removed)}"
        )
        return ResourceSection(added=added, modified=modified, removed=removed)

    def _compare_service(
        self, current: ComposeService, new: ComposeService
    ) -> ModificationDict | None:
        """Compare two services and return all changes."""
        changes = {}

        # Compare simple fields
        # Note: "image" is intentionally excluded - we don't want to show
        # image changes to customers as they use our managed registry
        simple_fields = [
            "entrypoint",
            "command",
            "working_dir",
            "stop_grace_period",
            "domain",
        ]
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
            deploy_changes = self._compare_models(
                current.deploy, new.deploy, include_defaults=True
            )
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
        self,
        current: BaseModel | None,
        new: BaseModel | None,
        include_defaults: bool = False,
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
                new_dict = new.model_dump(
                    exclude_defaults=not include_defaults, exclude_none=True
                )
                if new_dict or include_defaults:
                    return FieldChange(from_value=None, to_value=new_dict)
            return None

        if new is None:
            if current:
                # Model removed - show what was removed
                current_dict = current.model_dump(
                    exclude_defaults=not include_defaults, exclude_none=True
                )
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

        # Check for modifications in existing volumes (labels, external, etc.)
        modified = {}
        current_by_name = {v.name: v for v in current}
        new_by_name = {v.name: v for v in new}

        for name in current_names & new_names:
            current_vol = current_by_name[name]
            new_vol = new_by_name[name]
            changes = {}

            # Compare labels (includes storage class changes)
            if current_vol.labels != new_vol.labels:
                changes["labels"] = FieldChange(
                    from_value=current_vol.labels or {},
                    to_value=new_vol.labels or {},
                )

            # Compare external flag
            if current_vol.external != new_vol.external:
                changes["external"] = FieldChange(
                    from_value=current_vol.external,
                    to_value=new_vol.external,
                )

            if changes:
                modified[name] = changes

        logger.info(
            f"Volume comparison results: Added: {len(added)}, Modified: {len(modified)}, Removed: {len(removed)}"
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
            f"Network comparison results: Added: {len(added)}, Modified: {len(modified)}, Removed: {len(removed)}"
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

        # Add graceful shutdown period if specified
        if service.stop_grace_period is not None:
            result["stop_grace_period"] = service.stop_grace_period

        # Add deploy config if it has non-default values
        if "deploy" in data:
            result["deploy"] = data["deploy"]

        # Add scaling config if enabled
        if service.scaling and service.scaling.enabled:
            result["scaling"] = data.get("scaling", {})

        # Add healthcheck if defined
        if "healthcheck" in data:
            result["healthcheck"] = data["healthcheck"]

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


def get_shared_volumes(compose: ComposeFile) -> set[str]:
    """Return volume names used by multiple services."""
    volume_usage: dict[str, int] = {}
    for service in compose.services:
        if not service.volumes:
            continue

        for vol in service.volumes:
            # Extract volume name from volume spec
            if isinstance(vol, str):
                # Format: "volume_name:/path" or just "volume_name"
                vol_name = vol.split(":")[0] if ":" in vol else vol
                # Skip bind mounts (paths starting with / . or ~)
                if vol_name.startswith(("/", ".", "~")):
                    continue
            else:
                # Volume object with source attribute
                # Skip bind mounts (type="bind" or source starting with / . ~)
                vol_type = getattr(vol, "type", "volume")
                if vol_type == "bind":
                    continue
                vol_name = getattr(vol, "source", None)
                if not vol_name:
                    continue
                # Also skip by path pattern for safety
                if vol_name.startswith(("/", ".", "~")):
                    continue

            volume_usage[vol_name] = volume_usage.get(vol_name, 0) + 1

    return {name for name, count in volume_usage.items() if count > 1}


def detect_storage_type_changes(
    compose: ComposeFile,
    existing_pvcs: dict[str, str],
) -> list[StorageTypeChange]:
    """Detect volumes that would change storage type (EBS↔EFS).

    Args:
        compose: The compose file being deployed
        existing_pvcs: Dict mapping PVC name to current storage class

    Returns:
        List of storage type changes that require user confirmation
    """
    if not compose.volumes:
        return []

    changes = []
    shared_volumes = get_shared_volumes(compose)

    for volume in compose.volumes:
        volume_labels = volume.labels or {}

        # Determine new storage class
        use_shared = (
            volume_labels.get(LazyCloudLabel.VOLUME_SHARED) == "true"
            or volume.name in shared_volumes
        )
        new_storage_class = (
            STORAGE_CLASS_SHARED if use_shared else STORAGE_CLASS_STANDARD
        )

        # Check if this volume exists with a different storage class
        if volume.name in existing_pvcs:
            old_storage_class = existing_pvcs[volume.name]
            if old_storage_class != new_storage_class:
                # Determine reason for change
                if volume.name in shared_volumes:
                    reason = "Now used by multiple services"
                elif use_shared:
                    reason = f"Marked with {LazyCloudLabel.VOLUME_SHARED}=true"
                else:
                    reason = "No longer shared by multiple services"

                changes.append(
                    StorageTypeChange(
                        volume_name=volume.name,
                        old_storage_class=old_storage_class,
                        new_storage_class=new_storage_class,
                        reason=reason,
                    )
                )

    return changes
