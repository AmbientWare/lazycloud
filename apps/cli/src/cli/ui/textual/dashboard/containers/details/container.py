from enum import StrEnum

from models.statuses import DeploymentStatus, ServiceStatus
from responses.deployments import DeploymentResponse
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from cli.api import api
from cli.config import config
from cli.ui.textual.components import Container, SectionContainer
from cli.ui.textual.dashboard.containers.details.deployment_details import (
    DeploymentDetailsContainer,
)
from cli.ui.textual.dashboard.containers.details.empty_state import EmptyStateWidget
from cli.ui.textual.dashboard.containers.details.secret_details.container import (
    SecretsTable,
)
from cli.ui.textual.dashboard.containers.details.service_details import (
    ServiceDetailsContainer,
)


class DisplayMode(StrEnum):
    DEPLOYMENT = "deployment"
    SERVICE = "service"
    SECRET = "secret"


class ContentContainer(Container):
    deployment: reactive[DeploymentResponse | None] = reactive(None)
    deployment_status: reactive[DeploymentStatus | None] = reactive(None)
    service: reactive[ServiceStatus | None] = reactive(None)
    secret_key: reactive[str | None] = reactive(None)
    display_mode: reactive[str] = reactive(DisplayMode.DEPLOYMENT)
    has_deployments: reactive[bool | None] = reactive(
        None
    )  # None = loading, True/False = loaded

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.border_title = self._get_border_title("📋 [4] Details")
        self._deployment_view = None
        self._service_view = None
        self._secret_view = None
        self._service_subtitle = "r: Restart Service"
        self._logs_subtitle = "5: Instances"
        self._secrets_table = None

    def _get_border_title(self, title: str) -> str:
        """Add workspace name to border title."""
        try:
            workspace_name = config.active_workspace_name
            return f"{title} │ Workspace: {workspace_name}"
        except ValueError:
            # No active workspace set
            return title

    def compose(self) -> ComposeResult:
        """Create the content area."""
        with VerticalScroll(id="content-scroll"):
            yield Static()  # Empty placeholder that will show loading

    def on_mount(self) -> None:
        """Setup the container when mounted."""
        self._update_border_subtitle()
        self.can_focus = True
        self.loading = True

    async def on_unmount(self) -> None:
        """Clean up when container unmounts."""
        self._service_view = None

    def _update_border_subtitle(self) -> None:
        navigation_subtitle = "1: Deployments • 2: Services • 3: Secrets • 4: Details"

        parts = [navigation_subtitle]

        if self.display_mode == DisplayMode.SERVICE:
            parts.insert(0, self._service_subtitle)
            parts.append(self._logs_subtitle)

        self.border_subtitle = " • ".join(parts)

    async def watch_has_deployments(self, _old_value, new_value) -> None:
        """Handle when deployments are loaded"""
        if new_value is None:
            # Still loading
            return

        if new_value is False:
            # No deployments found - show empty state
            self.loading = False
            scroll = self.query_one(VerticalScroll)
            scroll.remove_children()
            scroll.mount(EmptyStateWidget())
        else:
            # Has deployments - loading will be turned off when deployment is selected
            pass

    async def watch_deployment(self, _old_value, new_value) -> None:
        """Auto-refresh when deployment changes"""
        if new_value:
            # Mark that we have deployments
            if self.has_deployments is None:
                self.has_deployments = True

            if self.display_mode == DisplayMode.DEPLOYMENT:
                await self.refresh_deployment_content()
        elif self.has_deployments is None:
            # No deployment selected and we haven't determined if deployments exist
            # Keep loading state
            pass

    async def watch_service(self, _old_value, new_value) -> None:
        """Auto-refresh when service changes"""
        if new_value and self.display_mode == DisplayMode.SERVICE:
            await self.refresh_service_content()

    async def watch_secret_key(self, _old_value, new_value) -> None:
        """Auto-refresh when secret key changes"""
        if new_value and self.display_mode == DisplayMode.SECRET:
            await self.refresh_secret_content()

    async def watch_display_mode(self, old_value, new_value) -> None:
        """Switch content based on display mode"""
        # Only switch if mode actually changed
        if old_value == new_value:
            return

        if new_value == DisplayMode.DEPLOYMENT and self.deployment:
            await self.refresh_deployment_content()
        elif new_value == DisplayMode.SERVICE and self.service:
            await self.refresh_service_content()
        elif new_value == DisplayMode.SECRET and self.deployment:
            await self.refresh_secret_content()

    async def refresh_deployment_content(self) -> None:
        if not self.deployment:
            return

        # Turn off loading state
        self.loading = False

        self.border_title = self._get_border_title(
            f"📋 [4] Deployment Details - {self.deployment.name}"
        )
        self._update_border_subtitle()

        scroll = self.query_one(VerticalScroll)
        scroll.remove_children()
        self._service_view = None

        if not self.deployment_status:
            error_widget = Static("[yellow]Loading deployment status...[/yellow]")
            scroll.mount(error_widget)
            return

        self._deployment_view = DeploymentDetailsContainer(
            deployment_id=self.deployment.id,
            deployment_status=self.deployment_status,
        )
        scroll.mount(self._deployment_view)

    async def refresh_service_content(self) -> None:
        if not self.service or not self.deployment:
            return

        self.border_title = self._get_border_title(
            f"📋 [4] Service Details - {self.service.name}"
        )
        self._update_border_subtitle()

        if self._service_view:
            self._service_view = None

        scroll = self.query_one(VerticalScroll)
        scroll.remove_children()

        self._service_view = ServiceDetailsContainer(
            deployment_id=self.deployment.id,
            service_name=self.service.name,
            service_status=self.service,
        )
        scroll.mount(self._service_view)

    async def refresh_secret_content(self) -> None:
        """Refresh the secrets view - shows all secrets for the deployment."""
        if not self.deployment:
            scroll = self.query_one(VerticalScroll)
            scroll.remove_children()
            error_widget = Static("[yellow]Please select a deployment first[/yellow]")
            scroll.mount(error_widget)
            return

        self.border_title = self._get_border_title("📋 [4] Secrets")
        self._update_border_subtitle()

        scroll = self.query_one(VerticalScroll)
        scroll.remove_children()
        self._service_view = None

        try:
            secrets_response = await api.secrets.get_secrets(self.deployment.id)

            if secrets_response and secrets_response.secrets:
                # Create and mount the secrets table
                self._secrets_table = SecretsTable(
                    deployment_id=self.deployment.id,
                    show_header=True,
                    cursor_type="row",
                    zebra_stripes=True,
                )
                scroll.mount(self._secrets_table)
                self._secrets_table.update_secrets(secrets_response.secrets)
                self._secrets_table.focus()
            else:
                section = SectionContainer(
                    "No Secrets",
                    Static("[dim]No secrets configured for this deployment[/dim]"),
                )
                scroll.mount(section)

        except Exception as e:
            error_section = SectionContainer(
                "Error", Static(f"[red]Failed to load secrets: {e}[/red]")
            )
            scroll.mount(error_section)

    async def clear_content(self) -> None:
        self.border_title = self._get_border_title("📋 [4] Details")
        self._update_border_subtitle()

        scroll = self.query_one(VerticalScroll)
        scroll.remove_children()
        self._service_view = None

        scroll.mount(EmptyStateWidget())

    def get_focusable_widget(self):
        """Return the current focusable widget based on display mode.

        Returns:
            Widget | None: The widget to focus, or None if no focusable widget exists
        """
        if self.display_mode == DisplayMode.SECRET:
            return self._secrets_table

        elif self.display_mode == DisplayMode.SERVICE and self._service_view:
            return getattr(self._service_view, "_pods_table", None)
        return None
