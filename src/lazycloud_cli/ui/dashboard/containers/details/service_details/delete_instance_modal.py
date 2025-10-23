from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import ConfirmModal, ErrorModal


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
        )

    def handle_delete(self):
        """Handle the delete action."""
        try:
            response = api.instances.delete_instance(
                deployment_id=self.deployment_id,
                pod_name=self.pod_name,
            )

            if response and response.task_id:
                # TODO: handle task monitoring
                return True

            # Show error modal if response is falsy or no task_id
            error_modal = ErrorModal(
                title="Failed to Delete Instance",
                message=f"Could not delete the instance.\n\n[dim]Service: {self.service_name}[/dim]\n[dim]Instance: {self.pod_name}[/dim]\n\n[dim]Please try again.[/dim]",
            )
            self.app.push_screen(error_modal)
            return False

        except Exception as e:
            error_modal = ErrorModal(
                title="Error Deleting Instance",
                message=f"An error occurred while deleting the instance:\n\n{str(e)}\n\n[dim]Service: {self.service_name}[/dim]\n[dim]Instance: {self.pod_name}[/dim]\n\n[dim]Please try again.[/dim]",
            )
            self.app.push_screen(error_modal)
            return False
