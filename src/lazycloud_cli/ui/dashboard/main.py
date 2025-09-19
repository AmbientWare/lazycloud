from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical

from lazycloud_cli.ui.dashboard.containers import (
    ContentContainer,
    DeploymentsContainer,
    ServicesContainer,
)
from lazycloud_cli.ui.dashboard.containers.content.container import DisplayMode
from lazycloud_cli.ui.dashboard.messages import DeploymentSelected, ServiceSelected
from lazycloud_cli.ui.dashboard.theme import theme


class DashboardApp(App):
    """Main dashboard application"""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("1", "switch_to_deployments", "Deployments"),
        ("2", "switch_to_services", "Services"),
        ("3", "switch_to_content", "Content"),
        ("4", "focus_instances", "Focus Instances"),
    ]

    def on_mount(self) -> None:
        self.screen.styles.background = theme.background
        self.set_focus(self.query_one("#deployments-container"))

    def compose(self) -> ComposeResult:
        """Create the dashboard layout."""
        layout = Horizontal(id="layout")
        layout.styles.layout = "horizontal"
        layout.styles.width = "100%"
        layout.styles.height = "100%"
        layout.styles.padding = theme.padding

        with layout:
            # Left column with deployments and services
            left_column = Vertical(id="left-column")
            left_column.styles.width = theme.left_width
            left_column.styles.height = "100%"

            with left_column:
                yield DeploymentsContainer(id="deployments-container")
                yield ServicesContainer(id="services-container")

            yield ContentContainer(id="main-container")

    async def on_deployment_selected(self, message: DeploymentSelected) -> None:
        """Handle deployment selection."""
        content_container = self.query_one("#main-container", ContentContainer)
        if content_container.display_mode == "service":
            await content_container.clear_content()
        await content_container.update_content(
            message.deployment_id, message.deployment_name
        )

        # update services container so it can show the services for the selected deployment
        services_container = self.query_one("#services-container", ServicesContainer)
        await services_container.update_deployment(
            message.deployment_id, message.deployment_name
        )

    async def on_service_selected(self, message: ServiceSelected) -> None:
        """Handle service selection"""
        content_container = self.query_one("#main-container", ContentContainer)
        await content_container.update_service_content(
            message.deployment_id,
            message.service,
        )

    def action_switch_to_deployments(self) -> None:
        """Switch to deployments section."""
        # Reset all borders to primary
        self._reset_borders()
        # focus on the deployments container and change border
        deployments_container = self.query_one("#deployments-container")
        deployments_container.styles.border = theme.get_border(focused=True)
        self.set_focus(deployments_container)

    def action_switch_to_services(self) -> None:
        """Switch to services section."""
        # reset all borders to primary
        self._reset_borders()
        # focus on the services container
        services_container = self.query_one("#services-container")
        services_container.styles.border = theme.get_border(focused=True)
        self.set_focus(services_container)

    def action_switch_to_content(self) -> None:
        """Switch to content section."""
        # reset all borders to primary
        self._reset_borders()
        # focus on the main content container
        content_container = self.query_one("#main-container")
        content_container.styles.border = theme.get_border(focused=True)
        self.set_focus(content_container)

    def _reset_borders(self) -> None:
        """Reset all container borders to primary color"""
        self.query_one("#deployments-container").styles.border = theme.get_border()
        self.query_one("#services-container").styles.border = theme.get_border()
        self.query_one("#main-container").styles.border = theme.get_border()

    def action_focus_instances(self) -> None:
        """Focus the instances table if it exists."""
        content_container = self.query_one("#main-container", ContentContainer)
        if content_container.display_mode == DisplayMode.SERVICE and content_container._service_view:
            if hasattr(content_container._service_view, '_pods_table') and content_container._service_view._pods_table:
                self.set_focus(content_container._service_view._pods_table)


def run_dashboard():
    app = DashboardApp()
    app.run()


if __name__ == "__main__":
    run_dashboard()
