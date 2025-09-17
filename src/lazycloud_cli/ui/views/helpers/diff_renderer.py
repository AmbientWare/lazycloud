"""Simplified diff rendering for deployment changes."""

from dataclasses import dataclass
from typing import Any

from rich.table import Table
from rich.text import Text

from lazycloud_cli.ui.components.card import Card
from lazycloud_cli.ui.theme import theme
from lazycloud_cli.ui.views.helpers.formatters import (
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
from shared.models.diffs import ComposeDiff, EnvVarChanges


@dataclass
class ChangeDisplay:
    """Configuration for displaying a change."""

    icon: str
    style: str
    title: str


# Change type display configurations
CHANGE_DISPLAYS = {
    "added": ChangeDisplay("➕", theme.success, "Added"),
    "modified": ChangeDisplay("🔄", theme.warning, "Modified"),
    "removed": ChangeDisplay("➖", theme.error, "Removed"),
}


def create_resource_table(resources: list[dict[str, Any]], change_type: str) -> Table:
    """Create a table for displaying resource changes."""
    table = Table(show_header=True, header_style="bold", box=None)
    display = CHANGE_DISPLAYS[change_type]

    if change_type == "added":
        table.add_column("Resource", style=display.style, width=20)
        table.add_column("Type", style=theme.text_secondary, width=10)
        table.add_column("Details", style=theme.text_secondary, no_wrap=False)

        for resource in resources:
            details = format_resource_details(resource["details"], resource["type"])
            table.add_row(f"{resource['name']}", resource["type"], details)

    elif change_type == "modified":
        table.add_column("Resource", style=display.style)
        table.add_column("Type", style=theme.text_secondary)
        table.add_column("Changes", style=theme.text_secondary, overflow="fold")

        for resource in resources:
            changes = format_resource_changes(resource["details"])
            table.add_row(f"{resource['name']}", resource["type"], changes)

    elif change_type == "removed":
        table.add_column("Resource", style=display.style, width=20)
        table.add_column("Type", style=theme.text_secondary, width=10)
        table.add_column("Details", style=theme.text_secondary)

        for resource in resources:
            details = (
                resource["details"].get("image", "")
                if resource["type"] == "service"
                else ""
            )
            table.add_row(f"{resource['name']}", resource["type"], details)

    return table


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
        parts.append(f"Image: {details['image']}")

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

    # Command
    if details.get("command"):
        parts.append(f"Command: {format_command(details['command'])}")

    # Limit output
    return "\n".join(parts[:8]) if parts else "-"


def format_resource_changes(changes: dict[str, Any]) -> str:
    """Format resource changes for display."""
    formatted_changes = []

    # Priority fields to show first
    priority_fields = [
        "image",
        "command",
        "ports",
        "volumes",
        "networks",
        "deploy",
        "scaling",
        "healthcheck",
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
    if isinstance(change, dict) and "from" in change and "to" in change:
        old_val = change["from"]
        new_val = change["to"]

        # Special formatting for specific fields
        if field == "image":
            return f"Image: {format_image_change(old_val, new_val)}"
        elif field == "ports":
            return f"Ports: {format_ports_list(old_val)} → {format_ports_list(new_val)}"
        elif field == "command":
            return f"Command: {format_command(old_val)} → {format_command(new_val)}"
        elif field == "deploy":
            return f"Deploy: {format_deploy_config(old_val)} → {format_deploy_config(new_val)}"
        elif field == "scaling":
            return f"Scaling: {format_scaling_config(old_val)} → {format_scaling_config(new_val)}"
        else:
            return f"{field.title()}: {format_value_summary(old_val)} → {format_value_summary(new_val)}"

    # Handle nested changes
    elif isinstance(change, dict):
        nested_changes = []
        for key, value in change.items():
            if isinstance(value, dict) and "from" in value:
                old_str = format_value_summary(value["from"])
                new_str = format_value_summary(value["to"])
                nested_changes.append(f"  {key}: {old_str} → {new_str}")

        if nested_changes:
            return f"{field.title()}:\n" + "\n".join(nested_changes[:3])

    return None


def create_diff_cards(diff: ComposeDiff) -> list[Card]:
    """Create cards for all diff changes."""
    cards = []

    # Process additions
    if diff.added:
        resources = extract_resources(diff.added, "added")
        if resources:
            display = CHANGE_DISPLAYS["added"]
            table = create_resource_table(resources, "added")
            cards.append(
                Card(
                    content=table,
                    title=f"{display.icon} {display.title} Resources ({len(resources)})",
                    border_style=display.style,
                )
            )

    # Process modifications
    if diff.modified:
        resources = extract_modified_resources(diff.modified)
        if resources:
            display = CHANGE_DISPLAYS["modified"]
            table = create_resource_table(resources, "modified")
            cards.append(
                Card(
                    content=table,
                    title=f"{display.icon} {display.title} Resources ({len(resources)})",
                    border_style=display.style,
                )
            )

    # Process removals
    if diff.removed:
        resources = extract_resources(diff.removed, "removed")
        if resources:
            display = CHANGE_DISPLAYS["removed"]
            table = create_resource_table(resources, "removed")
            cards.append(
                Card(
                    content=table,
                    title=f"{display.icon} {display.title} Resources ({len(resources)})",
                    border_style=display.style,
                )
            )

    return cards


def extract_resources(section: Any, change_type: str) -> list[dict[str, Any]]:
    """Extract resources from a diff section."""
    resources = []

    # Services
    for svc in getattr(section, "services", []):
        resources.append(
            {
                "name": svc.get("name", "unknown"),
                "type": "service",
                "details": svc,
                "change_type": change_type,
            }
        )

    # Volumes
    for vol in getattr(section, "volumes", []):
        resources.append(
            {
                "name": vol.get("name", "unknown"),
                "type": "volume",
                "details": vol,
                "change_type": change_type,
            }
        )

    # Networks
    for net in getattr(section, "networks", []):
        resources.append(
            {
                "name": net.get("name", "unknown"),
                "type": "network",
                "details": net,
                "change_type": change_type,
            }
        )

    return resources


def extract_modified_resources(section: Any) -> list[dict[str, Any]]:
    """Extract modified resources from diff section."""
    resources = []

    # Services
    for name, changes in getattr(section, "services", {}).items():
        resources.append(
            {
                "name": name,
                "type": "service",
                "details": changes,
                "change_type": "modified",
            }
        )

    # Volumes
    for name, changes in getattr(section, "volumes", {}).items():
        resources.append(
            {
                "name": name,
                "type": "volume",
                "details": changes,
                "change_type": "modified",
            }
        )

    # Networks
    for name, changes in getattr(section, "networks", {}).items():
        resources.append(
            {
                "name": name,
                "type": "network",
                "details": changes,
                "change_type": "modified",
            }
        )

    return resources


def create_env_var_card(env_changes: EnvVarChanges) -> Card | None:
    """Create card for environment variable changes."""
    if not env_changes or (not env_changes.added and not env_changes.removed):
        return None

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Variable", style=theme.primary)
    table.add_column("Status", style=theme.text_secondary)
    table.add_column("Notes", style=theme.text_secondary)

    # Added variables
    for var in env_changes.added:
        table.add_row(
            f"+ {var}",
            Text("New", style=theme.success),
            "Will be collected during deployment",
        )

    # Removed variables
    for var in env_changes.removed:
        table.add_row(
            f"- {var}",
            Text("Removed", style=theme.error),
            "Will be removed from secrets",
        )

    # Determine style
    if env_changes.added and not env_changes.removed:
        border_style = theme.border_success
        title = f"🔐 Environment Variables (+{len(env_changes.added)})"
    elif env_changes.removed and not env_changes.added:
        border_style = theme.border_error
        title = f"🔐 Environment Variables (-{len(env_changes.removed)})"
    else:
        border_style = theme.border_warning
        title = f"🔐 Environment Variables (+{len(env_changes.added)}, -{len(env_changes.removed)})"

    return Card(content=table, title=title, border_style=border_style)
