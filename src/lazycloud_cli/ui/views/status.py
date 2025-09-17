from datetime import datetime
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from lazycloud_cli.ui.components.badges import DeploymentStatusBadge
from lazycloud_cli.ui.components.card import Card, CardGroup
from lazycloud_cli.ui.theme import theme


class DeploymentDetailsCard(Card):
    """Card showing detailed deployment information."""

    def __init__(self, deployment: dict[str, Any]):
        """Initialize deployment details card."""
        # Create details table
        table = Table(show_header=False, box=None)
        table.add_column("Property", style=theme.primary)
        table.add_column("Value")

        # Basic info
        table.add_row("Name", deployment.get("name", "-"))

        # Status with badge
        status = deployment.get("status", "unknown")
        status_badge = DeploymentStatusBadge(status)
        table.add_row("Status", status_badge)

        # Timestamps
        if deployment.get("created_at"):
            table.add_row("Created", self._format_datetime(deployment["created_at"]))
        if deployment.get("deployed_at"):
            table.add_row("Deployed", self._format_datetime(deployment["deployed_at"]))
        if deployment.get("updated_at"):
            table.add_row("Updated", self._format_datetime(deployment["updated_at"]))

        # Status message if any
        if deployment.get("status_message"):
            table.add_row(
                "Message",
                Text(deployment["status_message"], style=theme.text_secondary),
            )

        super().__init__(
            content=table,
            title="🚀 Deployment Details",
            border_style=theme.primary_bright,
        )

    def _format_datetime(self, timestamp: Any) -> str:
        """Format a timestamp for display."""
        if isinstance(timestamp, datetime):
            return timestamp.strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(timestamp, str):
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                return str(timestamp)
        else:
            return str(timestamp)


class ServiceStatusCard(Card):
    """Card showing status of services in the deployment."""

    def __init__(self, services: list[dict[str, Any]]):
        """Initialize service status card."""
        if not services:
            content = Text("No services found", style=theme.text_secondary)
        else:
            # Create services table
            table = Table(show_header=True, box=None)
            table.add_column("Service", style=theme.primary)
            table.add_column("Image")
            table.add_column("Replicas")
            table.add_column("Ports")
            table.add_column("Status")

            for service in services:
                # Format replicas
                ready = service.get("ready_replicas", 0)
                total = service.get("replicas", 1)
                replicas = f"{ready}/{total}"

                # Format ports
                ports = service.get("ports", [])
                if ports:
                    port_str = ", ".join(
                        f"{p['port']}/{p.get('protocol', 'TCP')}" for p in ports
                    )
                else:
                    port_str = "-"

                # Status icon
                if ready == total and ready > 0:
                    status = "Ready"
                    status_style = theme.status_ready
                elif ready > 0:
                    status = "Partial"
                    status_style = theme.warning
                else:
                    status = "Not Ready"
                    status_style = theme.error

                table.add_row(
                    service.get("name", "-"),
                    service.get("image", "-"),
                    replicas,
                    port_str,
                    Text(status, style=status_style),
                )

            content = table

        super().__init__(
            content=content,
            title="⚙️  Services",
            subtitle=f"{len(services)}",
            border_style=theme.border_default,
        )


class ResourceTreeCard(Card):
    """Card showing deployment resources as a tree."""

    def __init__(self, resources: dict[str, list[str]]):
        """Initialize resource tree card.

        Args:
            resources: Dict mapping resource type to list of resource names
        """
        tree = Tree("Deployment Resources", style=theme.primary_bright)

        for resource_type, items in resources.items():
            if items:
                branch = tree.add(
                    f"[{theme.primary}]{resource_type}[/{theme.primary}] ({len(items)})"
                )
                for item in items:
                    branch.add(
                        f"[{theme.text_secondary}][/{theme.text_secondary}] {item}"
                    )

        super().__init__(
            content=tree,
            title="📦 Resource Overview",
            border_style=theme.info,
        )


class InstanceDetailsCard(Card):
    """Card showing service instance details."""

    def __init__(self, instances: list[dict[str, Any]]):
        """Initialize instance details card."""
        if not instances:
            content = Text("No instances found", style=theme.text_secondary)
        else:
            table = Table(show_header=True, box=None)
            table.add_column("Instance", style=theme.primary)
            table.add_column("Ready")
            table.add_column("Status")
            table.add_column("Restarts")
            table.add_column("Age")

            for instance in instances:
                # Ready containers
                ready = instance.get("ready_containers", 0)
                total = instance.get("total_containers", 1)
                ready_str = f"{ready}/{total}"

                # Status with color
                status = instance.get("phase", "Unknown")
                if status == "Running" and ready == total:
                    status_style = theme.status_ready
                elif status == "Running":
                    status_style = theme.warning
                elif status == "Pending":
                    status_style = theme.status_pending
                else:
                    status_style = theme.status_failed

                table.add_row(
                    instance.get("name", "-"),
                    ready_str,
                    Text(status, style=status_style),
                    str(instance.get("restart_count", 0)),
                    instance.get("age", "-"),
                )

            content = table

        super().__init__(
            content=content,
            title="🔍 Service Instances",
            subtitle=f"{len(instances)} instances",
            border_style=theme.border_default,
        )


class StatusView:
    """Main view orchestrator for the status command."""

    def __init__(self, console: Console):
        """Initialize the status view."""
        self.console = console

    def show_deployment_status(
        self,
        deployment: dict[str, Any],
        services: list[dict[str, Any]] | None = None,
        instances: list[dict[str, Any]] | None = None,
        resources: dict[str, list[str]] | None = None,
    ):
        """Show complete deployment status.

        Args:
            deployment: Deployment details
            services: List of services
            instances: List of service instances
            resources: Dict of resource types to names
        """
        cards = []

        # Deployment details
        cards.append(DeploymentDetailsCard(deployment))

        # Services
        if services is not None:
            cards.append(ServiceStatusCard(services))

        # Service Instances
        if instances is not None:
            cards.append(InstanceDetailsCard(instances))

        # Resource tree
        if resources:
            cards.append(ResourceTreeCard(resources))

        # Display all cards
        card_group = CardGroup(cards)
        self.console.print(card_group)

    def show_not_found(self, deployment_name: str):
        """Show not found message."""
        card = Card(
            content=Text(
                f"Deployment '{deployment_name}' not found.\n\n"
                "💡 Use 'lazycloud list' to see available deployments.",
                style=theme.warning,
            ),
            title="Not Found",
            border_style=theme.border_warning,
        )
        self.console.print(card)

    def show_error(self, message: str):
        """Show error message."""
        card = Card(
            content=Text(f"❌ {message}", style=theme.error),
            title="Error",
            border_style=theme.border_error,
        )
        self.console.print(card)
