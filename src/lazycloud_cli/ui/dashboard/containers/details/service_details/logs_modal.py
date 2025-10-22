from textual.app import ComposeResult
from textual.widgets import RichLog
from textual.worker import Worker

from lazycloud_cli.api import api
from lazycloud_cli.ui.dashboard.components import ContentModal
from lazycloud_cli.ui.dashboard.theme import Borders


class LogViewerModal(ContentModal):
    """A modal screen for viewing container logs."""

    def __init__(self, deployment_id: str, service_name: str, pod_name: str):
        display_name = pod_name if len(pod_name) <= 60 else pod_name[:57] + "..."

        super().__init__(
            title="",  # No centered title - using border title instead
            subtitle="",
            icon="",
            modal_width="95%",
            modal_height="90%",
            border_style=Borders.focus,  # Use accent color border
        )

        self.deployment_id = deployment_id
        self.service_name = service_name
        self.pod_name = pod_name
        self.display_name = display_name
        self._logs_widget: RichLog | None = None
        self._stream_task: Worker | None = None

    def compose_body(self) -> ComposeResult:
        """Create the logs widget."""
        self._logs_widget = RichLog(highlight=True, markup=True, auto_scroll=True)
        self._logs_widget.styles.background = "transparent"
        self._logs_widget.styles.border = None
        self._logs_widget.styles.padding = 0
        yield self._logs_widget

    def on_mount(self) -> None:
        """Start log streaming when modal opens."""
        super().on_mount()

        # Set title and subtitle on the modal container
        container = self.query_one("#content-modal-container")
        container.border_title = f"📄 Instance Logs: {self.display_name}"
        container.border_subtitle = "q: Close"

        self._logs_widget.styles.height = "1fr"
        self._logs_widget.write("[dim]Connecting to log stream...[/dim]")
        self._start_stream()

    async def on_unmount(self) -> None:
        """Clean up when modal closes."""
        await self.cleanup()

    def _start_stream(self) -> None:
        """Start SSE stream connection for log updates."""
        if self._stream_task and not self._stream_task.is_finished:
            self._stream_task.cancel()

        self._stream_task = self.run_worker(self._connect_logs_stream(), exclusive=True)

    async def cleanup(self) -> None:
        """Clean up SSE stream connections and tasks."""
        if self._stream_task and not self._stream_task.is_finished:
            self._stream_task.cancel()
            self._stream_task.wait()
        self._stream_task = None

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
                # Check if it's a container not found error
                error_msg = str(error)
                if (
                    "not found" in error_msg.lower()
                    or "does not exist" in error_msg.lower()
                ):
                    self._logs_widget.write(
                        f"[yellow]Instance '{self.pod_name}' not found or has been deleted[/yellow]"
                    )
                else:
                    self._logs_widget.write(f"[red]Log stream error: {error_msg}[/red]")

        if self._logs_widget:
            self._logs_widget.clear()
            self._logs_widget.write(
                f"[green]Connecting to log stream for instance: {self.pod_name}[/green]\n"
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
                        f"[yellow]Container '{self.pod_name}' not found or has been deleted[/yellow]"
                    )
                else:
                    self._logs_widget.write(f"[red]Failed to connect: {str(e)}[/red]")
