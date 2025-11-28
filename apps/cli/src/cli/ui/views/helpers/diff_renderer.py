"""Simplified diff rendering for deployment changes."""

from dataclasses import dataclass
from typing import Any

from models.compose import LazyCloudLabel
from models.diffs import ComposeDiff, EnvVarChanges, FieldChange, ResourceSection
from models.statuses import StorageType
from rich.table import Table
from rich.text import Text

from cli.ui.colors import Colors
from cli.ui.components.card import Card
from cli.ui.views.helpers.formatters import (
    format_command,
    format_deploy_config,
    format_env_var_list,
    format_healthcheck,
    format_image_change,
    format_ports_list,
    format_scaling_config,
    format_value_summary,
    format_volume_list,
)
from cli.utils.utils import format_image_name


@dataclass
class ChangeDisplay:
    """Configuration for displaying a change."""

    icon: str
    style: str
    title: str


# Change type display configurations
CHANGE_DISPLAYS = {
    "added": ChangeDisplay("➕", Colors.Ansi.success, "Added"),
    "modified": ChangeDisplay("🔄", Colors.Ansi.warning, "Modified"),
    "removed": ChangeDisplay("➖", Colors.Ansi.error, "Removed"),
}


def create_services_card(services: ResourceSection) -> Card | None:
    """Create a card for service changes."""
    if not services.has_changes():
        return None

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Service", style=Colors.Ansi.primary)
    table.add_column("Change", style=Colors.Ansi.text_muted)
    table.add_column("Details", style=Colors.Ansi.text_muted, overflow="fold")

    # Add added services
    for svc in services.added:
        name = svc.get("name", "unknown") if isinstance(svc, dict) else "unknown"
        details = format_resource_details(svc, "service")
        table.add_row(name, Text("Added", style=Colors.Ansi.success), details)

    # Add modified services
    for name, changes in services.modified.items():
        changes_str = format_service_modifications(changes)
        table.add_row(name, Text("Modified", style=Colors.Ansi.warning), changes_str)

    # Add removed services
    for svc in services.removed:
        name = svc.get("name", "unknown") if isinstance(svc, dict) else "unknown"
        image = svc.get("image", "") if isinstance(svc, dict) else ""
        table.add_row(name, Text("Removed", style=Colors.Ansi.error), image)

    count = len(services.added) + len(services.modified) + len(services.removed)
    return Card(
        content=table,
        title=f"🐳 Service Changes ({count})",
        border_style=Colors.Ansi.info,
    )


def create_volumes_card(volumes: ResourceSection) -> Card | None:
    """Create a card for volume changes."""
    if not volumes.has_changes():
        return None

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Volume", style=Colors.Ansi.primary)
    table.add_column("Change", style=Colors.Ansi.text_muted)
    table.add_column("Details", style=Colors.Ansi.text_muted)

    # Add added volumes
    for vol in volumes.added:
        name = vol.get("name", "unknown") if isinstance(vol, dict) else "unknown"
        table.add_row(name, Text("Added", style=Colors.Ansi.success), "")

    # Add modified volumes
    for name, changes in volumes.modified.items():
        details = _format_volume_changes(changes)
        table.add_row(name, Text("Modified", style=Colors.Ansi.warning), details)

    # Add removed volumes
    for vol in volumes.removed:
        name = vol.get("name", "unknown") if isinstance(vol, dict) else "unknown"
        table.add_row(name, Text("Removed", style=Colors.Ansi.error), "")

    count = len(volumes.added) + len(volumes.modified) + len(volumes.removed)
    return Card(
        content=table,
        title=f"💾 Volume Changes ({count})",
        border_style=Colors.Ansi.info,
    )


def _format_volume_changes(changes: dict[str, Any]) -> str:
    """Format volume changes for display."""
    parts = []

    # Check for label changes (storage class)
    if "labels" in changes:
        label_change = changes["labels"]
        if isinstance(label_change, FieldChange):
            old_labels = label_change.from_value or {}
            new_labels = label_change.to_value or {}

            # Check specifically for storage class changes (shared label)
            old_shared = old_labels.get(LazyCloudLabel.VOLUME_SHARED)
            new_shared = new_labels.get(LazyCloudLabel.VOLUME_SHARED)

            if old_shared != new_shared:
                old_class = (
                    StorageType.SHARED if old_shared == "true" else StorageType.STANDARD
                )
                new_class = (
                    StorageType.SHARED if new_shared == "true" else StorageType.STANDARD
                )
                parts.append(f"Storage: {old_class} → {new_class}")
            elif old_labels != new_labels:
                parts.append("Labels changed")

    # Check for external flag changes
    if "external" in changes:
        ext_change = changes["external"]
        if isinstance(ext_change, FieldChange):
            parts.append(f"External: {ext_change.from_value} → {ext_change.to_value}")

    return ", ".join(parts) if parts else "Properties changed"


def create_networks_card(networks: ResourceSection) -> Card | None:
    """Create a card for network changes."""
    if not networks.has_changes():
        return None

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Network", style=Colors.Ansi.primary)
    table.add_column("Change", style=Colors.Ansi.text_muted)
    table.add_column("Type", style=Colors.Ansi.text_muted)

    # Add added networks
    for net in networks.added:
        name = net.get("name", "unknown") if isinstance(net, dict) else "unknown"
        external = net.get("external", False) if isinstance(net, dict) else False
        net_type = "External" if external else "Internal"
        table.add_row(name, Text("Added", style=Colors.Ansi.success), net_type)

    # Add modified networks
    for name, _ in networks.modified.items():
        table.add_row(name, Text("Modified", style=Colors.Ansi.warning), "")

    # Add removed networks
    for net in networks.removed:
        name = net.get("name", "unknown") if isinstance(net, dict) else "unknown"
        table.add_row(name, Text("Removed", style=Colors.Ansi.error), "")

    count = len(networks.added) + len(networks.modified) + len(networks.removed)
    return Card(
        content=table,
        title=f"🌐 Network Changes ({count})",
        border_style=Colors.Ansi.info,
    )


def format_resource_details(details: dict[str, Any], resource_type: str) -> str:
    """Format resource details based on type."""
    if resource_type == "service":
        return format_service_details(details)
    elif resource_type == "volume":
        return "Persistent volume"
    elif resource_type == "network":
        return "External" if details.get("external") else "Internal"
    return ""


def format_service_details(details: dict[str, Any]) -> str:
    """Format service details for display."""
    parts = []

    # Image (most important)
    if details.get("image"):
        parts.append(f"Image: {format_image_name(details['image'])}")

    # Ports
    if details.get("ports"):
        parts.append(f"Ports: {format_ports_list(details['ports'])}")

    # Deploy config
    if details.get("deploy"):
        parts.append(f"Deploy: {format_deploy_config(details['deploy'])}")

    # Scaling
    if details.get("scaling"):
        parts.append(f"Scaling: {format_scaling_config(details['scaling'])}")

    # Volumes
    if details.get("volumes"):
        parts.append(f"Volumes: {format_volume_list(details['volumes'])}")

    # Environment
    if details.get("environment"):
        parts.append(f"Environment: {format_env_var_list(details['environment'])}")

    # Health check
    if details.get("healthcheck"):
        parts.append(f"Health check: {format_healthcheck(details['healthcheck'])}")

    # Graceful shutdown period
    if details.get("stop_grace_period"):
        parts.append(f"Shutdown: {details['stop_grace_period']}s")

    # Command
    if details.get("command"):
        parts.append(f"Command: {format_command(details['command'])}")

    # Limit output
    return "\n".join(parts[:8]) if parts else "-"


def format_service_modifications(changes: dict[str, Any]) -> str:
    """Format service modification changes for display."""
    formatted_changes = []

    # Priority fields to show first
    priority_fields = [
        "image",
        "entrypoint",
        "command",
        "working_dir",
        "ports",
        "volumes",
        "networks",
        "deploy",
        "scaling",
        "healthcheck",
        "stop_grace_period",
    ]

    for field in priority_fields:
        if field in changes:
            change = format_field_change(field, changes[field])
            if change:
                formatted_changes.append(change)

    # Show up to 5 changes
    return "\n".join(formatted_changes[:5]) if formatted_changes else "No changes"


def format_field_change(field: str, change: Any) -> str | None:
    """Format a single field change."""
    # Check if it's a FieldChange object
    if isinstance(change, FieldChange):
        # Special formatting for specific fields
        if field == "image":
            return f"Image: {format_image_change(change.from_value, change.to_value)}"
        elif field == "ports":
            return f"Ports: {format_ports_list(change.from_value)} → {format_ports_list(change.to_value)}"
        elif field == "command":
            return f"Command: {format_command(change.from_value)} → {format_command(change.to_value)}"
        elif field == "stop_grace_period":
            from_val = f"{change.from_value}s" if change.from_value else "default"
            to_val = f"{change.to_value}s" if change.to_value else "default"
            return f"Shutdown grace period: {from_val} → {to_val}"
        elif field == "entrypoint":
            from_val = (
                format_command(change.from_value) if change.from_value else "default"
            )
            to_val = format_command(change.to_value) if change.to_value else "default"
            return f"Entrypoint: {from_val} → {to_val}"
        elif field == "working_dir":
            from_val = change.from_value or "default"
            to_val = change.to_value or "default"
            return f"Working dir: {from_val} → {to_val}"
        else:
            return f"{field.title()}: {format_value_summary(change.from_value)} → {format_value_summary(change.to_value)}"

    # Handle dict of FieldChanges (nested changes like deploy.replicas)
    elif isinstance(change, dict):
        nested_changes = []
        for key, value in change.items():
            if isinstance(value, FieldChange):
                old_str = format_value_summary(value.from_value)
                new_str = format_value_summary(value.to_value)
                nested_changes.append(f"  {key}: {old_str} → {new_str}")

        if nested_changes:
            return f"{field.title()}:\n" + "\n".join(nested_changes)

    return None


def create_diff_cards(diff: ComposeDiff) -> list[Card]:
    """Create cards for all diff changes."""
    cards = []

    # Create card for services if there are any changes
    if diff.services.has_changes():
        services_card = create_services_card(diff.services)
        if services_card:
            cards.append(services_card)

    # Create card for volumes if there are any changes
    if diff.volumes.has_changes():
        volumes_card = create_volumes_card(diff.volumes)
        if volumes_card:
            cards.append(volumes_card)

    # Create card for networks if there are any changes
    if diff.networks.has_changes():
        networks_card = create_networks_card(diff.networks)
        if networks_card:
            cards.append(networks_card)

    return cards


def create_env_var_card(env_changes: EnvVarChanges) -> Card | None:
    """Create card for environment variable changes."""
    if not env_changes or (
        not env_changes.added
        and not env_changes.removed
        and not env_changes.user_managed
    ):
        return None

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Variable", style=Colors.Ansi.primary)
    table.add_column("Change", style=Colors.Ansi.text_muted)
    table.add_column("Notes", style=Colors.Ansi.text_muted)

    # Add added variables
    for var in env_changes.added:
        table.add_row(
            var,
            Text("Added", style=Colors.Ansi.success),
            "Will be added to deployment",
        )

    # Add removed variables
    for var in env_changes.removed:
        table.add_row(
            var,
            Text("Removed", style=Colors.Ansi.error),
            "Will be removed from deployment",
        )

    # Add user-managed variables (no changes, just informational)
    if env_changes.user_managed:
        if env_changes.added or env_changes.removed:
            table.add_row("", "", "")  # Empty row for spacing

        for var in env_changes.user_managed:
            table.add_row(
                var,
                Text("User-Managed", style=Colors.Ansi.info),
                "Added via dashboard, no changes",
            )

    count = len(env_changes.added) + len(env_changes.removed)
    user_managed_count = (
        len(env_changes.user_managed) if env_changes.user_managed else 0
    )

    # Update title based on what's shown
    if count > 0 and user_managed_count > 0:
        title = f"🔐 Environment Variables ({count} changes, {user_managed_count} user-managed)"
    elif count > 0:
        title = f"🔐 Environment Variable Changes ({count})"
    else:
        title = f"🔐 User-Managed Environment Variables ({user_managed_count})"

    return Card(
        content=table,
        title=title,
        border_style=Colors.Ansi.info,
    )
