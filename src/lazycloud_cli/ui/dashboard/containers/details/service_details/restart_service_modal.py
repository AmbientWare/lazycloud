from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import ConfirmModal, SuccessModal
from lazycloud_cli.ui.dashboard.theme import theme


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
                f"[dim]All pods will be restarted[/dim]"
            ),
            on_confirm=self.handle_restart,
            icon="🔄",
            border_color=theme.warning,
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
                # Show success modal after this one closes
                success_modal = RestartSuccessModal(self.service_name)
                self.app.push_screen(success_modal)
                return True

            return False

        except Exception:
            return False


class RestartSuccessModal(SuccessModal):
    """Modal showing restart success."""

    def __init__(self, service_name: str):
        super().__init__(
            title="Restart Initiated",
            message=(
                f"Service restart has been triggered successfully.\n\n"
                f"[dim]Service: {service_name}[/dim]\n\n"
                f"[dim]The service will restart shortly.[/dim]"
            ),
            icon="✓",
            border_color=theme.success,
        )
