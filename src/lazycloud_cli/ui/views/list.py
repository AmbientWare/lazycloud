"""List command view components."""

from datetime import datetime
from typing import Any, Dict, List

from rich.console import Console
from rich.table import Table
from rich.text import Text

from lazycloud_cli.ui.components.badges import DeploymentStatusBadge
from lazycloud_cli.ui.components.card import Card
from lazycloud_cli.ui.theme import theme


class DeploymentListCard(Card):
    """Card for displaying a list of deployments."""

    def __init__(self, deployments: List[Dict[str, Any]], total: int):
        """Initialize deployment list card.

        Args:
            deployments: List of deployment dictionaries
            total: Total number of deployments
        """
        # Create deployments table
        table = Table(
            show_header=True,
            header_style=theme.table_header,
            box=None,
            title_style="bold",
            expand=True,
        )

        # Add columns
        table.add_column("Name", style=theme.highlight, no_wrap=True)
        table.add_column("Status", justify="center")
        table.add_column("Created", style=theme.text_secondary)
        table.add_column("ID", style=theme.text_secondary)

        # Add rows
        for deployment in deployments:
            # Create status badge
            status_badge = DeploymentStatusBadge(deployment.get("status", "unknown"))

            # Format created time
            created_at = deployment.get("created_at", "")
            if created_at:
                created_display = self._format_datetime(created_at)
            else:
                created_display = "-"

            # Truncate ID for display
            deployment_id = deployment.get("id", "")
            id_display = (
                f"{deployment_id[:8]}..." if len(deployment_id) > 8 else deployment_id
            )

            table.add_row(
                deployment.get("name", "-"),
                status_badge,
                created_display,
                id_display,
            )

        # Determine title based on count
        if len(deployments) == 0:
            title = "No Deployments"
        elif len(deployments) == total:
            title = f"🚀 Active Deployments ({total})"
        else:
            title = f"🚀 Active Deployments ({len(deployments)} of {total})"

        super().__init__(
            content=table,
            title=title,
            border_style=theme.primary_bright,
        )

    def _format_datetime(self, timestamp: Any) -> str:
        """Format a timestamp for display.

        Args:
            timestamp: Timestamp to format (string, datetime, or other)

        Returns:
            Formatted time string
        """
        if isinstance(timestamp, datetime):
            # Calculate relative time
            now = datetime.now(timestamp.tzinfo)
            delta = now - timestamp

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
        elif isinstance(timestamp, str):
            try:
                # Try to parse ISO format
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                return self._format_datetime(dt)
            except Exception:
                return timestamp
        else:
            return str(timestamp)


class EmptyStateCard(Card):
    """Card shown when there are no deployments."""

    def __init__(self):
        """Initialize empty state card."""
        content = Text.from_markup(
            "[dim]No deployments found.[/dim]\n\n"
            "💡 [bright_yellow]Get started with:[/bright_yellow]\n"
            "   [cyan]lazycloud deploy[/cyan]\n\n"
            "[dim]This will deploy your docker-compose.yml definitions to the cloud[/dim]"
        )

        super().__init__(
            content=content,
            title="Welcome to LazyCloud",
            border_style=theme.border_warning,
        )


class FilterInfoCard(Card):
    """Card showing active filters."""

    def __init__(self, filters: Dict[str, Any]):
        """Initialize filter info card.

        Args:
            filters: Dictionary of active filters
        """
        active_filters = {k: v for k, v in filters.items() if v is not None}

        if not active_filters:
            return

        filter_text = Text("Active filters: ", style=theme.text_secondary)

        for i, (key, value) in enumerate(active_filters.items()):
            if i > 0:
                filter_text.append(", ", style=theme.text_secondary)
            filter_text.append(f"{key}=", style=theme.primary)
            filter_text.append(str(value), style=theme.primary_bright)

        super().__init__(
            content=filter_text,
            border_style=theme.muted,
        )


class ListView:
    """Main view orchestrator for the list command."""

    def __init__(self, console: Console):
        """Initialize the list view."""
        self.console = console

    def show_deployments(
        self,
        deployments: List[Dict[str, Any]],
        total: int,
        filters: Dict[str, Any] | None = None,
    ):
        """Show the deployments list.

        Args:
            deployments: List of deployment dictionaries
            total: Total number of deployments
            filters: Active filters
        """
        # Show filters if any
        if filters:
            filter_card = FilterInfoCard(filters)
            if filter_card.content:
                self.console.print(filter_card)

        # Show deployments or empty state
        if deployments:
            card = DeploymentListCard(deployments, total)
            self.console.print(card)
        else:
            self.show_empty_state()

    def show_empty_state(self):
        """Show empty state when no deployments found."""
        card = EmptyStateCard()
        self.console.print(card)

    def show_error(self, message: str):
        """Show an error message."""
        error_card = Card(
            content=Text(f"{message}", style=theme.error),
            title="Error",
            border_style=theme.border_error,
        )
        self.console.print(error_card)

    def _format_datetime(self, timestamp: Any) -> str:
        """Format a timestamp for display.

        Args:
            timestamp: Timestamp to format (string, datetime, or other)

        Returns:
            Formatted time string
        """
        if isinstance(timestamp, datetime):
            # Calculate relative time
            now = datetime.now(timestamp.tzinfo)
            delta = now - timestamp

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
        elif isinstance(timestamp, str):
            try:
                # Try to parse ISO format
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                return self._format_datetime(dt)
            except Exception:
                return timestamp
        else:
            return str(timestamp)
