from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical

from lazycloud_cli.ui.textual.dashboard.containers import (
    ContentContainer,
    DeploymentsContainer,
    DisplayMode,
    SecretsContainer,
    ServicesContainer,
)
from lazycloud_cli.ui.textual.dashboard.containers.details.service_details import (
    RestartServiceModal,
)
from lazycloud_cli.ui.textual.theme import Layout, lazycloud_theme


class DashboardApp(App):
    """Main dashboard application"""

    CSS_PATH = "../styles.tcss"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("escape", "quit", "Quit"),
        ("r", "restart_service", "Restart Service"),
        ("1", "switch_to_deployments", "Deployments"),
        ("2", "switch_to_services", "Services"),
        ("3", "switch_to_secrets", "Secrets"),
        ("4", "switch_to_content", "Content"),
        ("5", "focus_instances", "Focus Instances"),
    ]

    def on_mount(self) -> None:
        """Setup the layout once the app is mounted"""
        # Register and activate theme
        self.register_theme(lazycloud_theme)
        self.theme = "lazycloud"

        self.set_focus(self.query_one(DeploymentsContainer))

    def compose(self) -> ComposeResult:
        """Create the dashboard layout."""
        layout = Horizontal(id="layout")
        layout.styles.layout = "horizontal"
        layout.styles.width = "100%"
        layout.styles.height = "100%"
        layout.styles.padding = Layout.padding

        with layout:
            # Left column with deployments and services
            left_column = Vertical(id="left-column")
            left_column.styles.width = Layout.left_width
            left_column.styles.height = "100%"

            with left_column:
                yield DeploymentsContainer(id="deployments-container")
                yield ServicesContainer(id="services-container")
                yield SecretsContainer(id="secrets-container")

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
        deployments_container = self.query_one(DeploymentsContainer)
        self.set_focus(deployments_container)

        # get the main content container and set the mode to deployment
        main_content_container = self.query_one(ContentContainer)
        main_content_container.display_mode = DisplayMode.DEPLOYMENT

    def action_switch_to_services(self) -> None:
        """Switch to services section."""
        services_container = self.query_one(ServicesContainer)
        self.set_focus(services_container)

        # get the main content container and set the mode to service
        main_content_container = self.query_one(ContentContainer)
        main_content_container.display_mode = DisplayMode.SERVICE

    def action_switch_to_content(self) -> None:
        """Switch to content section."""
        content_container = self.query_one(ContentContainer)
        self.set_focus(content_container)

    def action_switch_to_secrets(self) -> None:
        """Switch to secrets section - focuses secrets table in [4]."""
        main_content_container = self.query_one(ContentContainer)
        main_content_container.display_mode = DisplayMode.SECRET

        # Focus the secrets table if it exists, otherwise the content container
        if main_content_container._secrets_table:
            self.set_focus(main_content_container._secrets_table)
        else:
            self.set_focus(main_content_container)

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
