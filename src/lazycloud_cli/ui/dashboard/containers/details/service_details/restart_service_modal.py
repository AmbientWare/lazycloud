from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import ConfirmModal, ErrorModal


class RestartServiceModal(ConfirmModal):
    """Modal for confirming service restart."""

    def __init__(self, service_name: str, deployment_id: str):
        self.service_name = service_name
        self.deployment_id = deployment_id

        super().__init__(
            title="Restart Service",
            message=(
                f"Are you sure you want to restart this service?\n\n"
                f"Service: [bold]{service_name}[/bold]\n"
                f"[dim]All instances will be restarted[/dim]"
            ),
            on_confirm=self.handle_restart,
            icon="🔄",
        )

    def handle_restart(self):
        """Handle the restart action."""
        try:
            response = api.services.restart_service(
                deployment_id=self.deployment_id,
                service_name=self.service_name,
            )

            if response and response.task_id:
                # TODO: handle task monitoring
                return True

            # Show error modal if response is falsy or no task_id
            error_modal = ErrorModal(
                title="Failed to Restart Service",
                message=f"Could not restart the service.\n\n[dim]Service: {self.service_name}[/dim]\n\n[dim]Please try again.[/dim]",
            )
            self.app.push_screen(error_modal)
            return False

        except Exception as e:
            error_modal = ErrorModal(
                title="Error Restarting Service",
                message=f"An error occurred while restarting the service:\n\n{str(e)}\n\n[dim]Service: {self.service_name}[/dim]\n\n[dim]Please try again.[/dim]",
            )
            self.app.push_screen(error_modal)
            return False
