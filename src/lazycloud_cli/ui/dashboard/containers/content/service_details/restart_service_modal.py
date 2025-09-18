from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import Static

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import BaseModalScreen, ModalContainer
from lazycloud_cli.ui.dashboard.theme import theme


class RestartServiceModal(BaseModalScreen):
    """Modal for confirming service restart."""

    BINDINGS = [
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name
        self.deployment_id = None

    def compose(self) -> ComposeResult:
        """Create the modal layout."""
        with Container(id="modal-wrapper"):
            with ModalContainer(id="restart-modal-container"):
                yield Static(
                    "[bold]🔄 Restart Service[/bold]",
                    id="restart-header",
                )

                yield Static(
                    f"Are you sure you want to restart this service?\n\n"
                    f"Service: [bold]{self.service_name}[/bold]\n"
                    f"[dim]All pods will be restarted[/dim]\n\n"
                    f"Press [bold green]Y[/bold green] to confirm or [bold red]N[/bold red] to cancel",
                    id="restart-message",
                )

    def on_mount(self) -> None:
        """Style the modal when it mounts."""
        # Style the wrapper to center the modal
        wrapper = self.query_one("#modal-wrapper")
        wrapper.styles.align = ("center", "middle")
        wrapper.styles.width = "100%"
        wrapper.styles.height = "100%"

        # Override modal container size for smaller modal
        container = self.query_one("#restart-modal-container")
        container.styles.width = "50%"
        container.styles.max_width = 60
        container.styles.height = "auto"
        container.styles.max_height = 20
        container.styles.border = (theme.border_style, theme.warning)

        # Style header
        header = self.query_one("#restart-header")
        header.styles.text_align = "center"
        header.styles.margin = (0, 0, 1, 0)

        # Style message
        message = self.query_one("#restart-message")
        message.styles.text_align = "center"
        message.styles.margin = (1, 2, 2, 2)

    def action_confirm(self) -> None:
        """Handle confirmation action."""
        # Perform the restart
        try:
            response = api.deployments.restart_service(
                deployment_id=self.deployment_id,
                service_name=self.service_name,
            )

            if response and response.success:
                # Dismiss this modal first
                self.dismiss(True)
                # Show success modal
                success_modal = RestartSuccessModal(self.service_name)
                self.app.push_screen(success_modal)
            else:
                self.dismiss(False)

        except Exception:
            self.dismiss(False)

    def action_cancel(self) -> None:
        """Handle cancel action."""
        self.dismiss(False)


class RestartSuccessModal(BaseModalScreen):
    """Modal showing restart success."""

    BINDINGS = [
        ("enter", "dismiss", "Close"),
        ("escape", "dismiss", "Close"),
    ]

    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name

    def compose(self) -> ComposeResult:
        """Create the modal layout."""
        with Container(id="modal-wrapper"):
            with ModalContainer(id="success-modal-container"):
                yield Static(
                    "[bold green]✓ Restart Initiated[/bold green]",
                    id="success-header",
                )

                yield Static(
                    f"Service restart has been triggered successfully.\n\n"
                    f"[dim]Service: {self.service_name}[/dim]\n\n"
                    f"[dim]The service will restart shortly. Press Enter to close.[/dim]",
                    id="success-message",
                )

    def on_mount(self) -> None:
        """Style the modal when it mounts."""
        # Style the wrapper to center the modal
        wrapper = self.query_one("#modal-wrapper")
        wrapper.styles.align = ("center", "middle")
        wrapper.styles.width = "100%"
        wrapper.styles.height = "100%"

        # Override modal container size
        container = self.query_one("#success-modal-container")
        container.styles.width = "50%"
        container.styles.max_width = 60
        container.styles.height = "auto"
        container.styles.max_height = 15

        # Style header
        header = self.query_one("#success-header")
        header.styles.text_align = "center"
        header.styles.margin = (0, 0, 1, 0)

        # Style message
        message = self.query_one("#success-message")
        message.styles.text_align = "center"
        message.styles.margin = (1, 2, 1, 2)

    def action_dismiss(self) -> None:
        """Dismiss the success modal."""
        self.dismiss()
