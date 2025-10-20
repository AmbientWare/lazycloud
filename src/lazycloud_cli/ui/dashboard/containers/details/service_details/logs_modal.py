from textual.app import ComposeResult
from textual.widgets import RichLog
from textual.worker import Worker

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import ContentModal
from lazycloud_cli.ui.dashboard.theme import theme


class LogViewerModal(ContentModal):
    """A modal screen for viewing pod logs."""

    def __init__(self, deployment_id: str, service_name: str, pod_name: str):
        display_name = pod_name if len(pod_name) <= 50 else pod_name[:47] + "..."

        super().__init__(
            title=f"Logs: {display_name}",
            subtitle="(Press ESC or Q to close)",
            icon="📜",
            border_color=theme.info,
        )

        self.deployment_id = deployment_id
        self.service_name = service_name
        self.pod_name = pod_name
        self._logs_widget: RichLog | None = None
        self._stream_task: Worker | None = None

    def compose_body(self) -> ComposeResult:
        """Create the logs widget."""
        self._logs_widget = RichLog(highlight=True, markup=True, auto_scroll=True)
        yield self._logs_widget

    def on_mount(self) -> None:
        """Start log streaming when modal opens."""
        super().on_mount()
        self._logs_widget.styles.height = "1fr"
        self._logs_widget.write("[dim]Connecting to log stream...[/dim]")
        self._stream_task = self.run_worker(self._connect_logs_stream())

    async def on_unmount(self) -> None:
        """Clean up when modal closes."""
        if self._stream_task:
            self._stream_task.cancel()
        if api.logs and api.logs.is_connected():
            await api.logs.disconnect()

    async def _connect_logs_stream(self) -> None:
        """Connect to the logs SSE stream."""

        def on_log_message(data: dict) -> None:
            """Handle incoming log messages."""
            if self._logs_widget:
                # Handle error messages from the server
                if data.get("type") == "error":
                    error_msg = data.get("data", {}).get("message", "Unknown error")
                    self._logs_widget.write(f"[red]Error: {error_msg}[/red]")
                    return

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
                # Check if it's a pod not found error
                error_msg = str(error)
                if (
                    "not found" in error_msg.lower()
                    or "does not exist" in error_msg.lower()
                ):
                    self._logs_widget.write(
                        f"[yellow]Pod '{self.pod_name}' not found or has been deleted[/yellow]"
                    )
                else:
                    self._logs_widget.write(f"[red]Log stream error: {error_msg}[/red]")

        if self._logs_widget:
            self._logs_widget.clear()
            self._logs_widget.write(
                f"[green]Connecting to log stream for pod: {self.pod_name}[/green]\n"
            )

        try:
            await api.logs.stream_logs(
                deployment_id=self.deployment_id,
                service_name=self.service_name,
                tail=100,
                on_message=on_log_message,
                on_error=on_log_error,
                pod_name=self.pod_name,
            )
        except Exception as e:
            if self._logs_widget:
                if "not found" in str(e).lower():
                    self._logs_widget.write(
                        f"[yellow]Pod '{self.pod_name}' not found or has been deleted[/yellow]"
                    )
                else:
                    self._logs_widget.write(f"[red]Failed to connect: {str(e)}[/red]")
