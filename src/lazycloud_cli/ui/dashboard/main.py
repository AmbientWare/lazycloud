from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical

from lazycloud_cli.ui.dashboard.containers import (
    ContentContainer,
    DeploymentsContainer,
    DisplayMode,
    ServicesContainer,
)
from lazycloud_cli.ui.dashboard.containers.details.service_details import (
    RestartServiceModal,
)
from lazycloud_cli.ui.dashboard.theme import theme


class DashboardApp(App):
    """Main dashboard application"""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "restart_service", "Restart Service"),
        ("1", "switch_to_deployments", "Deployments"),
        ("2", "switch_to_services", "Services"),
        ("3", "switch_to_content", "Content"),
        ("4", "focus_instances", "Focus Instances"),
    ]

    def on_mount(self) -> None:
        self.screen.styles.background = theme.background
        self.set_focus(self.query_one(DeploymentsContainer))

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

            # Main content container
            yield ContentContainer(id="main-container")

    def action_restart_service(self) -> None:
        """Handle the restart service action."""
        content_container = self.query_one(ContentContainer)
        if (
            content_container.display_mode == DisplayMode.SERVICE
            and not content_container.service
            and not content_container.deployment
        ):
            return

        # Show confirmation modal
        modal = RestartServiceModal(
            service_name=content_container.service.name,
            deployment_id=content_container.deployment.id,
        )
        self.app.push_screen(modal)

    def action_switch_to_deployments(self) -> None:
        """Switch to deployments section."""
        # Reset all borders to primary
        self._reset_borders()
        # focus on the deployments container and change border
        deployments_container = self.query_one(DeploymentsContainer)
        deployments_container.styles.border = theme.get_border(focused=True)
        self.set_focus(deployments_container)

        # get the main content container and set the mode to deployment
        main_content_container = self.query_one(ContentContainer)
        main_content_container.display_mode = DisplayMode.DEPLOYMENT

    def action_switch_to_services(self) -> None:
        """Switch to services section."""
        # reset all borders to primary
        self._reset_borders()
        # focus on the services container
        services_container = self.query_one(ServicesContainer)
        services_container.styles.border = theme.get_border(focused=True)
        self.set_focus(services_container)

        # get the main content container and set the mode to service
        main_content_container = self.query_one(ContentContainer)
        main_content_container.display_mode = DisplayMode.SERVICE

    def action_switch_to_content(self) -> None:
        """Switch to content section."""
        # reset all borders to primary
        self._reset_borders()
        # focus on the main content container
        content_container = self.query_one(ContentContainer)
        content_container.styles.border = theme.get_border(focused=True)
        self.set_focus(content_container)

    def _reset_borders(self) -> None:
        """Reset all container borders to primary color"""
        self.query_one(DeploymentsContainer).styles.border = theme.get_border()
        self.query_one(ServicesContainer).styles.border = theme.get_border()
        self.query_one(ContentContainer).styles.border = theme.get_border()

    def action_focus_instances(self) -> None:
        """Focus the instances table if it exists."""
        content_container = self.query_one(ContentContainer)
        if (
            content_container.display_mode == DisplayMode.SERVICE
            and content_container._service_view
        ):
            if (
                hasattr(content_container._service_view, "_pods_table")
                and content_container._service_view._pods_table
            ):
                self.set_focus(content_container._service_view._pods_table)


def run_dashboard():
    app = DashboardApp()
    app.run()


if __name__ == "__main__":
    run_dashboard()
