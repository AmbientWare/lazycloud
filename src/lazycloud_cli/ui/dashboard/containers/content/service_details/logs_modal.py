from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import RichLog, Static
from textual.worker import Worker

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import BaseModalScreen, ModalContainer


class LogViewerModal(BaseModalScreen):
    """A modal screen for viewing pod logs."""

    def __init__(self, deployment_id: str, service_name: str, pod_name: str):
        super().__init__()
        self.deployment_id = deployment_id
        self.service_name = service_name
        self.pod_name = pod_name
        self._logs_widget: RichLog | None = None
        self._ws_task: Worker | None = None

    def compose(self) -> ComposeResult:
        """Create the modal layout."""
        with Container(id="modal-wrapper"):
            with ModalContainer(id="log-modal-container"):
                display_name = (
                    self.pod_name
                    if len(self.pod_name) <= 50
                    else self.pod_name[:47] + "..."
                )
                yield Static(
                    f"[bold]📜 Logs: {display_name}[/bold] [dim](Press ESC or Q to close)[/dim]",
                    id="log-header",
                )
                self._logs_widget = RichLog(
                    highlight=True, markup=True, auto_scroll=True
                )
                yield self._logs_widget

    def on_mount(self) -> None:
        """Start log streaming when modal opens."""
        self._logs_widget.write("[dim]Connecting to log stream...[/dim]")
        self._ws_task = self.run_worker(self._connect_logs_stream())

        # Style the wrapper to center the modal
        wrapper = self.query_one("#modal-wrapper")
        wrapper.styles.align = ("center", "middle")
        wrapper.styles.width = "100%"
        wrapper.styles.height = "100%"

        # Apply header styles
        header = self.query_one("#log-header")
        header.styles.dock = "top"
        header.styles.height = 1
        header.styles.margin = (0, 0, 1, 0)

        self._logs_widget.styles.height = "1fr"

    async def on_unmount(self) -> None:
        """Clean up when modal closes."""
        if self._ws_task:
            self._ws_task.cancel()
        if api.logs and api.logs.is_connected():
            await api.logs.disconnect()

    async def _connect_logs_stream(self) -> None:
        """Connect to the logs WebSocket stream."""

        def on_log_message(data: dict) -> None:
            """Handle incoming log messages."""
            if self._logs_widget:
                log_line = data.get("line", "")
                if log_line:
                    # Check if user has scrolled up
                    is_at_bottom = self._logs_widget.scroll_offset.y >= (
                        self._logs_widget.virtual_size.height
                        - self._logs_widget.size.height
                    )

                    if is_at_bottom:
                        self._logs_widget.write(log_line)
                        self._logs_widget.auto_scroll = True
                    else:
                        self._logs_widget.auto_scroll = False

        def on_log_error(error: Exception) -> None:
            """Handle log stream errors."""
            if self._logs_widget:
                self._logs_widget.write(f"[red]Log stream error: {str(error)}[/red]")

        if self._logs_widget:
            self._logs_widget.clear()
            self._logs_widget.write(
                f"[green]Connected to log stream for pod: {self.pod_name}[/green]\n"
            )

        await api.logs.stream_logs(
            deployment_id=self.deployment_id,
            service_name=self.service_name,
            tail=100,
            on_message=on_log_message,
            on_error=on_log_error,
            pod_name=self.pod_name,
        )
