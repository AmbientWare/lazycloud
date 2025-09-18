"""
Dashboard view for LazyCloud using Textual.
"""

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical

from lazycloud_cli.ui.dashboard.containers import (
    ContentContainer,
    DeploymentsContainer,
    ServicesContainer,
)
from lazycloud_cli.ui.dashboard.theme import theme


class DashboardApp(App):
    """Main dashboard application."""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("1", "switch_to_deployments", "Deployments"),
        ("2", "switch_to_services", "Services"),
        ("3", "switch_to_content", "Content"),
    ]

    def on_mount(self) -> None:
        """Style the app when mounted."""
        # Style the screen
        self.screen.styles.background = theme.background

    def compose(self) -> ComposeResult:
        """Create the dashboard layout."""
        # Main layout container
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

    async def on_deployments_container_deployment_selected(
        self, message: DeploymentsContainer.DeploymentSelected
    ) -> None:
        """Handle deployment selection."""
        # Update content container (clear any service view first)
        content_container = self.query_one("#main-container", ContentContainer)
        if content_container.display_mode == "service":
            await content_container.clear_content()
        await content_container.update_content(
            message.deployment_id, message.deployment_name
        )

        # Update services container
        services_container = self.query_one("#services-container", ServicesContainer)
        await services_container.update_deployment(
            message.deployment_id, message.deployment_name
        )

    async def on_services_container_service_selected(
        self, message: ServicesContainer.ServiceSelected
    ) -> None:
        """Handle service selection."""
        # Update content container to show service details
        content_container = self.query_one("#main-container", ContentContainer)
        await content_container.update_service_content(
            message.deployment_id,
            message.service,
        )

    async def action_refresh(self) -> None:
        """Refresh all data."""
        deployments_container = self.query_one(
            "#deployments-container", DeploymentsContainer
        )
        await deployments_container.refresh_deployments()

    def action_switch_to_deployments(self) -> None:
        """Switch to deployments section."""
        # Reset all borders to primary
        self._reset_borders()
        # Focus on the deployments container and change border
        deployments_container = self.query_one("#deployments-container")
        deployments_container.styles.border = theme.get_border(focused=True)
        self.set_focus(deployments_container)

    def action_switch_to_services(self) -> None:
        """Switch to services section."""
        # Reset all borders to primary
        self._reset_borders()
        # Focus on the services container
        services_container = self.query_one("#services-container")
        services_container.styles.border = theme.get_border(focused=True)
        self.set_focus(services_container)

    def action_switch_to_content(self) -> None:
        """Switch to content section."""
        # Reset all borders to primary
        self._reset_borders()
        # Focus on the main content container
        content_container = self.query_one("#main-container")
        content_container.styles.border = theme.get_border(focused=True)
        self.set_focus(content_container)

    def _reset_borders(self) -> None:
        """Reset all container borders to primary color."""
        self.query_one("#deployments-container").styles.border = theme.get_border()
        self.query_one("#services-container").styles.border = theme.get_border()
        self.query_one("#main-container").styles.border = theme.get_border()


def run_dashboard():
    """Run the dashboard application."""
    app = DashboardApp()
    app.run()


if __name__ == "__main__":
    run_dashboard()
