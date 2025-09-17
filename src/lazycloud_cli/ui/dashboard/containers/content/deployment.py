"""
Deployment-specific UI components for the dashboard.
"""

from textual.containers import Container
from textual.widgets import Static

from lazycloud_cli.ui.dashboard.containers.common import SectionContainer
from shared.models.statuses import DeploymentStatus


class DeploymentView:
    """Handles deployment-specific UI rendering."""

    def __init__(self, parent_container: Container):
        self.parent = parent_container

    def render(self, deployment: DeploymentStatus) -> None:
        """Render deployment details in the parent container"""
        # Deployment Overview Section
        overview_content = self._build_overview_content(deployment)
        self._create_section("📦 Deployment Overview", overview_content)

        # Services Section
        if deployment.services:
            services_content = self._build_services_content(deployment)
            self._create_section("🔧 Services", services_content)

        # Volumes Section
        if deployment.volumes:
            volumes_content = self._build_volumes_content(deployment)
            self._create_section("💾 Volumes", volumes_content)

        # Networks Section
        if deployment.networks:
            networks_content = self._build_networks_content(deployment)
            self._create_section("🌐 Networks", networks_content)

    def _build_overview_content(self, deployment: DeploymentStatus) -> list[str]:
        """Build deployment overview section content."""
        status_color = self._get_status_color(deployment.status)

        content = [
            f"Deployment ID:   {deployment.deployment_id}",
            f"Deployment Name: {deployment.deployment_name}",
            f"Namespace:       {deployment.namespace}",
            f"Status:          [{status_color}]{deployment.status.upper()}[/{status_color}]",
            f"Ready:           {'✅ Yes' if deployment.ready else '⏳ No'}",
        ]

        if deployment.last_updated:
            content.append(
                f"Last Updated:    {deployment.last_updated.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        return content

    def _build_services_content(self, deployment: DeploymentStatus) -> list[str]:
        """Build services section content."""
        content = []
        # Services is a dictionary from the API
        for service in deployment.services:
            # Get status and format it
            status_str = str(service.status).lower()
            status_color = self._get_status_color_str(status_str)
            ready_replicas = service.ready_replicas
            replicas = service.replicas

            content.append(
                f"● {service.name}: [{status_color}]{status_str}[/{status_color}] "
                f"({ready_replicas}/{replicas} replicas)"
            )
        return content

    def _build_volumes_content(self, deployment: DeploymentStatus) -> list[str]:
        """Build volumes section content."""
        return [f"● {name} - {status}" for name, status in deployment.volumes.items()]

    def _build_networks_content(self, deployment: DeploymentStatus) -> list[str]:
        """Build networks section content."""
        return [f"● {name} - {status}" for name, status in deployment.networks.items()]

    def _create_section(self, title: str, content: list[str]) -> None:
        """Create a section with content in the parent container."""
        section = SectionContainer(title)
        self.parent.mount(section)

        text_content = "\n".join(content).strip()
        widget = Static(text_content, markup=True)
        section.mount(widget)

    def _get_status_color(self, status: str) -> str:
        """Get color for status display."""
        if status == "running":
            return "green"
        elif status == "partially running":
            return "yellow"
        elif status == "stopped":
            return "dim"
        else:
            return "red"

    def _get_status_color_str(self, status: str) -> str:
        """Get color for status string display."""
        status_lower = status.lower()
        if status_lower == "running":
            return "green"
        elif status_lower in ("pending", "partially running"):
            return "yellow"
        elif status_lower == "stopped":
            return "dim"
        else:
            return "red"
