"""Deploy progress display component for CLI."""

from datetime import datetime

from models.monitoring import DeployOverallPhase
from models.statuses import StatusPhase
from rich.table import Table
from rich.text import Text

from cli.ui.colors import Colors
from cli.ui.components.card import Card


class ServiceStatusDisplay:
    """Live progress tracker for deploying services."""

    def __init__(self, deployment_name: str):
        """Initialize deploy progress display."""
        self.deployment_name = deployment_name
        self.services: list[dict] = []
        self.overall: DeployOverallPhase | str = DeployOverallPhase.DEPLOYING
        self.elapsed_seconds = 0
        self.failure_detected = False
        self.failure_message: str | None = None
        self.start_time = datetime.now()

    def update(self, data: dict) -> None:
        """Update from SSE event data."""
        self.services = data.get("services", [])
        self.elapsed_seconds = data.get("elapsed_seconds", 0)
        self.failure_detected = data.get("failure_detected", False)
        self.failure_message = data.get("failure_message")

    def render(self) -> Card:
        """Render the current deploy status."""
        table = Table(
            show_header=True,
            header_style=f"bold {Colors.Ansi.primary}",
            box=None,
            expand=True,
        )
        table.add_column("Service", style=Colors.Ansi.primary, width=18)
        table.add_column("Status", width=12)
        table.add_column("Containers", width=30)

        for service in self.services:
            name = service.get("name", "unknown")
            status = service.get("status", "pending")
            containers = service.get("containers", {})

            desired = containers.get("desired", 0)
            running = containers.get("running", 0)
            creating = containers.get("creating", 0)
            health_check = containers.get("health_check", 0)
            pending = containers.get("pending", 0)
            stopping = containers.get("stopping", 0)

            # Status with color
            status_style = self._get_status_style(status)
            status_display = Text(status, style=status_style)

            # Build pods display: "1 running, 1 health_check"
            pod_parts = []
            if running > 0:
                pod_parts.append(Text(f"{running} running", style=Colors.Ansi.success))
            if health_check > 0:
                if pod_parts:
                    pod_parts.append(Text(", ", style=Colors.Ansi.text_muted))
                pod_parts.append(
                    Text(f"{health_check} health_check", style=Colors.Ansi.warning)
                )
            if creating > 0:
                if pod_parts:
                    pod_parts.append(Text(", ", style=Colors.Ansi.text_muted))
                pod_parts.append(
                    Text(f"{creating} creating", style=Colors.Ansi.warning)
                )
            if pending > 0:
                if pod_parts:
                    pod_parts.append(Text(", ", style=Colors.Ansi.text_muted))
                pod_parts.append(
                    Text(f"{pending} pending", style=Colors.Ansi.text_muted)
                )
            if stopping > 0:
                if pod_parts:
                    pod_parts.append(Text(", ", style=Colors.Ansi.text_muted))
                pod_parts.append(
                    Text(f"{stopping} stopping", style=Colors.Ansi.warning)
                )

            if not pod_parts:
                pod_parts.append(
                    Text(f"0/{desired}", style=Colors.Ansi.text_muted)
                )

            pods_display = Text()
            for part in pod_parts:
                pods_display.append_text(part)

            table.add_row(
                name,
                status_display,
                pods_display,
            )

        # Elapsed time row
        table.add_row(
            "",
            "",
            Text(f"Elapsed: {self.elapsed_seconds}s", style=Colors.Ansi.text_muted),
        )

        # Failure message
        if self.failure_message:
            table.add_row("", "", "")
            table.add_row(
                Text("⚠", style=Colors.Ansi.warning),
                "",
                Text(self.failure_message, style=Colors.Ansi.warning),
            )

        title, border_style = self._get_title_and_style()

        return Card(
            content=table,
            title=title,
            border_style=border_style,
        )

    def _get_title_and_style(self) -> tuple[str, str]:
        """Get title and border style based on overall status."""
        overall = self.overall
        if isinstance(overall, str):
            try:
                overall = DeployOverallPhase(overall)
            except ValueError:
                pass

        if overall == DeployOverallPhase.COMPLETED:
            return f"✅ Deployed: {self.deployment_name}", Colors.Ansi.success
        elif overall == DeployOverallPhase.FAILED:
            return f"🛑 Deployment Failed: {self.deployment_name}", Colors.Ansi.error
        else:
            return f"🚀 Deploying: {self.deployment_name}", Colors.Ansi.info

    def _get_status_style(self, status: str) -> str:
        """Get style for a status string."""
        try:
            phase = StatusPhase(status)
        except ValueError:
            return Colors.Ansi.text_muted

        if phase == StatusPhase.RUNNING:
            return Colors.Ansi.success
        elif phase in (StatusPhase.ERROR, StatusPhase.EXITED):
            return Colors.Ansi.error
        elif phase in (
            StatusPhase.CREATING,
            StatusPhase.HEALTH_CHECK,
            StatusPhase.PENDING,
            StatusPhase.RESTARTING,
            StatusPhase.UPDATING,
            StatusPhase.STOPPING,
        ):
            return Colors.Ansi.warning

        return Colors.Ansi.text_muted

    def __rich__(self):
        """Support Rich rendering."""
        return self.render()
