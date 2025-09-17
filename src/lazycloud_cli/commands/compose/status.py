import asyncio
from datetime import datetime
from typing import Any

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from lazycloud_cli.api import api
from lazycloud_cli.ui.views import StatusView
from lazycloud_cli.utils import get_current_deployment_name

console = Console()


class RealtimeStatusView:
    """Real-time status view with WebSocket updates."""

    def __init__(self, console: Console):
        """Initialize real-time status view."""
        self.console = console
        self.view = StatusView(console)
        self.ws_client = None
        self._current_status: dict[str, Any] | None = None
        self._deployment_info: dict[str, Any] | None = None
        self._live: Live | None = None
        self._error_message: str | None = None

    def set_deployment_info(self, deployment_info: dict[str, Any]):
        """Set the deployment info."""
        self._deployment_info = deployment_info

    def _parse_ports(self, ports: list) -> list:
        """Parse port strings from the API into the format expected by the view."""
        if not ports:
            return []

        parsed_ports = []
        for port_str in ports:
            if isinstance(port_str, str) and "/" in port_str:
                # Port is already formatted as "8000:8000/TCP"
                # Extract just the port part
                port_part, protocol = port_str.rsplit("/", 1)
                parsed_ports.append({"port": port_part, "protocol": protocol})
            else:
                # Just a port string without protocol
                parsed_ports.append({"port": str(port_str)})
        return parsed_ports

    def _render_status(self) -> Group:
        """Render the current status and return a Group for Live display."""
        if not self._current_status or not self._deployment_info:
            return Group(Text("Waiting for status data...", style="dim"))

        # Prepare deployment data
        # Map status from k8s to deployment status
        k8s_status = self._current_status.get("status", "unknown")
        if k8s_status == "running":
            deployment_status = "deployed"
        elif k8s_status == "partially running":
            deployment_status = "deploying"
        elif k8s_status == "stopped":
            deployment_status = "failed"
        else:
            deployment_status = "unknown"

        deployment_data = {
            "name": self._current_status.get(
                "deployment_name", self._deployment_info.get("name")
            ),
            "status": deployment_status,
            "created_at": self._deployment_info.get("created_at"),
            "deployed_at": self._deployment_info.get("deployed_at"),
            "updated_at": self._deployment_info.get("updated_at"),
            "status_message": k8s_status,
        }

        # Convert services
        services = []
        services_data = self._current_status.get("services")
        if services_data and isinstance(services_data, dict):
            for service_name, service in services_data.items():
                if not isinstance(service, dict):
                    continue
                services.append(
                    {
                        "name": service_name,
                        "image": service.get("image", "N/A"),
                        "replicas": service.get("replicas", 0),
                        "ready_replicas": service.get("ready_replicas", 0),
                        "ports": self._parse_ports(service.get("ports", [])),
                        "status": service.get("status", "unknown"),
                    }
                )

        # Build resources dict
        resources = {}
        if self._current_status.services:
            resources["Services"] = list(self._current_status["services"].keys())
        if self._current_status.get("volumes"):
            resources["Volumes"] = list(self._current_status["volumes"].keys())
        if self._current_status.get("networks"):
            resources["Networks"] = list(self._current_status["networks"].keys())

        # Create a temporary console to capture the output
        from io import StringIO

        string_buffer = StringIO()
        temp_console = Console(file=string_buffer, force_terminal=True)
        temp_view = StatusView(temp_console)

        # Render to the temporary console
        temp_view.show_deployment_status(
            deployment=deployment_data,
            services=services,
            resources=resources if resources else None,
        )

        # Get the rendered content
        rendered_content = string_buffer.getvalue()

        # Add connection status with clock
        current_time = datetime.now().strftime("%H:%M:%S")
        if self.ws_client and self.ws_client.is_connected():
            status_msg = (
                f"Live updates active (Press Ctrl+C to exit) • 🕐 {current_time}"
            )
            status_style = "dim"
        else:
            status_msg = (
                f"\n⏸  Connection lost. Attempting to reconnect... • 🕐 {current_time}"
            )
            status_style = "yellow"

        # Check for error message
        if self._error_message:
            status_msg = f"\n{self._error_message} • 🕐 {current_time}"
            status_style = "red"

        # Return as a Group
        return Group(
            Text.from_ansi(rendered_content),
            Text(status_msg, style=status_style),
        )

    def _on_status_update(self, data: dict[str, Any]):
        """Handle status update from WebSocket."""
        self._current_status = data
        if self._live:
            self._live.update(self._render_status())

    def _on_error(self, error: Exception):
        """Handle WebSocket errors."""
        self._error_message = f"❌ Connection error: {error}"
        if self._live:
            self._live.update(self._render_status())

    async def run(self, deployment_id: str):
        """Run the real-time status view."""
        try:
            # Use Live display for real-time updates
            with Live(
                self._render_status(),
                console=self.console,
                refresh_per_second=4,
                transient=False,
            ) as live:
                self._live = live
                self._error_message = None

                # Connect to WebSocket
                self.ws_client = await api.status.stream_deployment_status(
                    deployment_id=deployment_id,
                    on_update=self._on_status_update,
                    on_error=self._on_error,
                )

        except KeyboardInterrupt:
            pass
        except Exception as e:
            self._error_message = f"❌ Failed to connect: {e}"
            if self._live:
                self._live.update(self._render_status())
        finally:
            self._live = None
            if self.ws_client:
                await self.ws_client.disconnect()


def status(
    deployment: str | None = typer.Argument(None, help="Deployment ID or name"),
):
    """Get real-time status of a compose deployment.

    DEPLOYMENT can be either a deployment ID or name. If not provided,
    uses the deployment from the current directory's lazycloud.yaml file.

    The status will automatically update in real-time as changes occur.
    """
    view = StatusView(console)

    # If no deployment specified, try to infer from current directory
    if not deployment:
        deployment = get_current_deployment_name()
        if not deployment:
            view.show_error(
                "No deployment specified and no lazycloud.yaml found in current directory",
                suggestion="Specify a deployment name or run from a directory with lazycloud.yaml",
            )
            raise typer.Exit(1)

    try:
        # First try to get deployment by name or ID
        deployment_info = api.deployments.get_deployment(name=deployment)
        if not deployment_info:
            deployment_info = api.deployments.get_deployment(deployment_id=deployment)

        if not deployment_info:
            view.show_not_found(deployment)
            return

        # Always use WebSocket for real-time updates
        realtime_view = RealtimeStatusView(console)
        realtime_view.set_deployment_info(deployment_info.model_dump())
        asyncio.run(realtime_view.run(deployment_info.id))

    except Exception as e:
        view.show_error(str(e))
        raise typer.Exit(1)
