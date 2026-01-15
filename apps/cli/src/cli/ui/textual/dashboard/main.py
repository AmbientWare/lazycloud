from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical

from cli.ui.textual.dashboard.containers import (
    ContentContainer,
    DeploymentsContainer,
    DisplayMode,
    SecretsContainer,
    ServicesContainer,
)
from cli.ui.textual.dashboard.containers.details.service_details import (
    RestartServiceModal,
)
from cli.ui.textual.messages import (
    DeploymentSelected,
    DeploymentStatusUpdated,
    DeploymentsLoaded,
    SecretSelected,
    ServiceSelected,
    ServiceStatusUpdated,
)
from cli.ui.textual.theme import lazycloud_theme


class DashboardApp(App):
    """Main dashboard application"""

    CSS_PATH = [
        "../components/container.tcss",
        "../components/datatable.tcss",
        "../components/listview.tcss",
        "../components/modals/modals.tcss",
        "../styles/utilities.tcss",
        "../styles/dashboard.tcss",
        "../styles/usage.tcss",
    ]
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("escape", "quit", "Quit"),
        ("r", "restart_service", "Restart Service"),
        ("c", "copy_endpoint", "Copy Endpoint"),
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
        with Horizontal(id="layout"):
            # Left column with deployments and services
            with Vertical(id="left-column"):
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
        focusable_widget = main_content_container.get_focusable_widget()
        if focusable_widget:
            self.set_focus(focusable_widget)
        else:
            self.set_focus(main_content_container)

    def action_focus_instances(self) -> None:
        """Focus the instances table if it exists."""
        content_container = self.query_one(ContentContainer)
        focusable_widget = content_container.get_focusable_widget()
        if focusable_widget:
            self.set_focus(focusable_widget)

    def action_copy_endpoint(self) -> None:
        """Copy the service endpoint URL to clipboard when service details are displayed."""
        content_container = self.query_one(ContentContainer)

        # Only works when viewing service details
        if content_container.display_mode != DisplayMode.SERVICE:
            return

        # Get endpoint from the service status
        if content_container.service and content_container.service.endpoint:
            endpoint = content_container.service.endpoint
            endpoint_url = (
                endpoint
                if endpoint.startswith(("http://", "https://"))
                else f"https://{endpoint}"
            )
            self.copy_to_clipboard(endpoint_url)
            self.notify(f"Copied: {endpoint_url}", timeout=2)
        else:
            self.notify("No endpoint URL available", severity="warning", timeout=2)

    def on_deployments_loaded(self, message: DeploymentsLoaded) -> None:
        """Handle deployments loaded - update content container."""
        content = self.query_one(ContentContainer)
        content.has_deployments = message.has_deployments

    def on_deployment_selected(self, message: DeploymentSelected) -> None:
        """Handle deployment selection - coordinate updates between components."""
        # Update content container
        content = self.query_one(ContentContainer)
        content.deployment = message.deployment
        content.deployment_status = message.status

        # Update services container
        services = self.query_one(ServicesContainer)
        services.deployment_id = message.deployment.id

        # Update secrets container
        secrets = self.query_one(SecretsContainer)
        secrets.deployment_id = message.deployment.id

    def on_service_selected(self, message: ServiceSelected) -> None:
        """Handle service selection - update content container."""
        content = self.query_one(ContentContainer)
        content.service = message.service_status

    def on_secret_selected(self, message: SecretSelected) -> None:
        """Handle secret selection - update content container display mode."""
        content = self.query_one(ContentContainer)
        content.display_mode = DisplayMode.SECRET

    def on_deployment_status_updated(self, message: DeploymentStatusUpdated) -> None:
        """Handle deployment status SSE update - update cached state."""
        content = self.query_one(ContentContainer)
        # Only update if this is for the currently selected deployment
        if content.deployment and content.deployment.id == message.deployment_id:
            content.deployment_status = message.status

    def on_service_status_updated(self, message: ServiceStatusUpdated) -> None:
        """Handle service status SSE update - update cached state."""
        content = self.query_one(ContentContainer)
        # Only update if this is for the currently selected service
        if content.service and content.service.name == message.service_status.name:
            content.service = message.service_status


def run_dashboard():
    app = DashboardApp()
    app.run()


if __name__ == "__main__":
    run_dashboard()
