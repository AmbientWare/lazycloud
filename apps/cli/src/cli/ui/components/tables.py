"""Table components for data display."""

from datetime import datetime
from typing import Any, Dict, List

from rich import box
from rich.table import Table

from cli.ui.components.badges import DeploymentStatusBadge


def format_key_value_list(data: Dict[str, Any], style: str = "dim") -> Table:
    """Format a dictionary as a key-value table.

    Args:
        data: Dictionary to format
        style: Style for the keys

    Returns:
        Rich Table with key-value pairs
    """
    table = Table(show_header=False, box=None)
    table.add_column("Key", style=style)
    table.add_column("Value")

    for key, value in data.items():
        table.add_row(f"{key}:", str(value))

    return table


def create_deployment_list_table(
    deployments: List[Dict[str, Any]],
    total: int = 0,
    show_id: bool = True,
    show_relative_time: bool = True,
) -> Table:
    """Create a table listing multiple deployments.

    Args:
        deployments: List of deployment dictionaries
        total: Total number of deployments (for title)
        show_id: Whether to show deployment IDs
        show_relative_time: Whether to show relative time instead of absolute

    Returns:
        Rich Table with deployment list
    """
    title = "[bold bright_cyan]Active Deployments[/bold bright_cyan]"
    if total:
        title += f" ({len(deployments)} of {total})"

    table = Table(
        title=title,
        box=box.ROUNDED,
        show_header=True,
        header_style="bold bright_white on deep_sky_blue4",
        title_style="bold bright_cyan",
        border_style="bright_blue",
        row_styles=["", "dim"],  # Alternating row colors
    )

    # Add columns
    table.add_column("Name", style="bright_cyan", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("Created", style="dim white")

    if show_id:
        table.add_column("ID", style="dim cyan", no_wrap=True)

    for deployment in deployments:
        # Status badge
        status_badge = DeploymentStatusBadge(deployment.get("status", "unknown"))

        # Time formatting
        created_at = deployment.get("created_at")
        if created_at and show_relative_time:
            time_str = _format_relative_time(created_at)
        else:
            time_str = str(created_at) if created_at else "unknown"

        # Build row
        row = [
            deployment.get("name", ""),
            status_badge.render(),
            time_str,
        ]

        if show_id:
            # Truncate ID for display
            dep_id = deployment.get("id", "")
            short_id = dep_id[:8] + "..." if len(dep_id) > 11 else dep_id
            row.append(short_id)

        table.add_row(*row)

    return table


def _format_relative_time(timestamp: Any) -> str:
    """Format a timestamp as relative time.

    Args:
        timestamp: Timestamp to format (string or datetime)

    Returns:
        Formatted relative time string
    """
    if isinstance(timestamp, str):
        try:
            # Try to parse ISO format
            created_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except Exception:
            return str(timestamp)
    elif isinstance(timestamp, datetime):
        created_dt = timestamp
    else:
        return str(timestamp)

    # Calculate relative time
    now = datetime.now(created_dt.tzinfo)
    delta = now - created_dt

    if delta.days > 0:
        return f"{delta.days}d ago"
    elif delta.seconds > 3600:
        hours = delta.seconds // 3600
        return f"{hours}h ago"
    elif delta.seconds > 60:
        minutes = delta.seconds // 60
        return f"{minutes}m ago"
    else:
        return "just now"
