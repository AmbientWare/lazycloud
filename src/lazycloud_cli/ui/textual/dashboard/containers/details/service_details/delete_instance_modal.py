from lazycloud_cli.api import api
from lazycloud_cli.ui.textual.components import ConfirmModal, ErrorModal
from lazycloud_cli.ui.textual.theme import Icons


class DeleteInstanceModal(ConfirmModal):
    """Modal for confirming instance deletion."""

    def __init__(
        self, service_name: str, deployment_id: str, pod_name: str, force: bool = False
    ):
        self.service_name = service_name
        self.deployment_id = deployment_id
        self.pod_name = pod_name
        self.force = force

        force_text = " (FORCE)" if force else ""
        super().__init__(
            title=f"Delete Instance{force_text}",
            message=(
                f"Are you sure you want to delete this instance{' (FORCE)' if force else ''}?\n\n"
                f"Service: [bold]{service_name}[/bold]\n"
                f"[dim]Instance: {pod_name}[/dim]"
                + (
                    "\n\n[yellow]⚠ Force delete will bypass graceful shutdown[/yellow]"
                    if force
                    else ""
                )
            ),
            on_confirm=self.handle_delete,
            icon=Icons.TRASH,
        )

    def handle_delete(self):
        """Handle the delete action."""
        try:
            response = api.instances.delete_instance(
                deployment_id=self.deployment_id,
                pod_name=self.pod_name,
                force=self.force,
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
