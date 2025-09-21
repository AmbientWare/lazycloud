from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import ConfirmModal, SuccessModal
from lazycloud_cli.ui.dashboard.theme import theme


class DeleteInstanceModal(ConfirmModal):
    """Modal for confirming service restart."""

    def __init__(self, service_name: str, deployment_id: str, pod_name: str):
        self.service_name = service_name
        self.deployment_id = deployment_id
        self.pod_name = pod_name

        super().__init__(
            title="Delete Instance",
            message=(
                f"Are you sure you want to delete this instance?\n\n"
                f"Service: [bold]{service_name}[/bold]\n"
                f"[dim]Instance: {pod_name}[/dim]"
            ),
            on_confirm=self.handle_delete,
            icon="🔄",
            border_color=theme.warning,
        )

    def handle_delete(self):
        """Handle the delete action."""
        try:
            response = api.instances.delete_instance(
                deployment_id=self.deployment_id,
                service_name=self.service_name,
                pod_name=self.pod_name,
            )

            if response and response.task_id:
                # TODO: handle task monitoring
                self.dismiss(True)
                success_modal = DeleteInstanceSuccessModal(
                    self.service_name, self.pod_name
                )
                self.app.push_screen(success_modal)
                return True

            return False

        except Exception:
            return False


class DeleteInstanceSuccessModal(SuccessModal):
    """Modal showing delete instance success."""

    def __init__(self, service_name: str, pod_name: str):
        super().__init__(
            title="Delete Instance Initiated",
            message=(
                f"Service delete has been triggered successfully.\n\n"
                f"[dim]Service: {service_name}[/dim]\n\n"
                f"[dim]Instance: {pod_name}[/dim]\n\n"
                f"[dim]The instance will delete shortly.[/dim]"
            ),
            icon="✓",
            border_color=theme.success,
        )
