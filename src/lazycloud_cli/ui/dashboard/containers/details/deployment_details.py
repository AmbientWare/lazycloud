from textual.widgets import Static

from lazycloud_cli.ui.dashboard.components import Container
from lazycloud_cli.ui.dashboard.components.section import SectionContainer
from lazycloud_cli.ui.dashboard.containers.details.utils import get_status_color
from shared.models.statuses import DeploymentStatus


class DeploymentDetailsContainer:
    """Handles deployment-specific UI rendering."""

    def __init__(self, parent_container: Container):
        self.parent = parent_container

    def render(self, deployment: DeploymentStatus) -> None:
        overview_content = self._build_overview_content(deployment)
        self._create_section("📦 Overview", overview_content)

        if deployment.services:
            services_content = self._build_services_content(deployment)
            self._create_section("🔧 Services", services_content)

        if deployment.volumes:
            volumes_content = self._build_volumes_content(deployment)
            self._create_section("💾 Volumes", volumes_content)

        if deployment.networks:
            networks_content = self._build_networks_content(deployment)
            self._create_section("🌐 Networks", networks_content)

    def _build_overview_content(self, deployment: DeploymentStatus) -> list[str]:
        status_color = get_status_color(deployment.status)

        content = [
            f"Deployment Name: {deployment.deployment_name}",
            f"Status:          [{status_color}]{deployment.status.upper()}[/{status_color}]",
            f"Ready:           {'✅ Yes' if deployment.ready else '⏳ No'}",
        ]

        if deployment.last_updated:
            content.append(
                f"Last Updated:    {deployment.last_updated.strftime('%Y-%m-%d %H:%M:%S')}"
            )

        return content

    def _build_services_content(self, deployment: DeploymentStatus) -> list[str]:
        content = []
        for service in deployment.services:
            status_color = get_status_color(service.status)
            ready_replicas = service.ready_replicas
            replicas = service.replicas

            content.append(
                f"● {service.name}: [{status_color}]{service.status.upper()}[/{status_color}] "
                f"({ready_replicas}/{replicas} replicas)"
            )
        return content

    def _build_volumes_content(self, deployment: DeploymentStatus) -> list[str]:
        return [f"● {v.name} - {v.status}" for v in deployment.volumes]

    def _build_networks_content(self, deployment: DeploymentStatus) -> list[str]:
        return [f"● {n.name} - {n.status}" for n in deployment.networks]

    def _create_section(self, title: str, content: list[str]) -> None:
        section = SectionContainer(title)
        self.parent.mount(section)

        text_content = "\n".join(content).strip()
        widget = Static(text_content, markup=True)
        section.mount(widget)
