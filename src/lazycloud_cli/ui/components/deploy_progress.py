"""Deploy progress display component for CLI."""

from datetime import datetime

from rich.table import Table
from rich.text import Text

from lazycloud_cli.ui.colors import Colors
from lazycloud_cli.ui.components.card import Card
from shared.models.monitoring import DeployOverallPhase
from shared.models.statuses import DeployServicePhase


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

        overall_raw = data.get("overall", "deploying")
        try:
            self.overall = DeployOverallPhase(overall_raw)
        except ValueError:
            self.overall = overall_raw

    def is_complete(self) -> bool:
        """Check if deployment is in a terminal state."""
        if isinstance(self.overall, DeployOverallPhase):
            return self.overall in (
                DeployOverallPhase.COMPLETED,
                DeployOverallPhase.FAILED,
            )
        return self.overall in ("completed", "failed")

    def render(self) -> Card:
        """Render the current deploy status."""
        table = Table(
            show_header=True,
            header_style=f"bold {Colors.Ansi.primary}",
            box=None,
            expand=True,
        )
        table.add_column("Service", style=Colors.Ansi.primary, width=18)
        table.add_column("Status", width=10)
        table.add_column("Containers", width=20)
        table.add_column("Message", style=Colors.Ansi.text_muted)

        # When deployment is completed, show final clean state
        is_completed = (
            self.overall == DeployOverallPhase.COMPLETED or self.overall == "completed"
        )

        for service in self.services:
            name = service.get("name", "unknown")
            status = service.get("status", "pending")
            message = service.get("message", "")
            containers = service.get("containers", {})

            desired = containers.get("desired", 0)
            running = containers.get("running", 0)
            pending = containers.get("pending", 0) + containers.get("creating", 0)
            stopping = containers.get("stopping", 0)

            # If deployment completed, show final healthy state
            if is_completed and not self.failure_detected:
                status = "running"
                message = "Healthy"
                running = desired
                pending = 0
                stopping = 0

            # Status with color
            status_style = self._get_status_style(status)
            status_display = Text(status, style=status_style)

            # Build containers display showing actual state
            container_parts = []
            if running > 0:
                container_parts.append(Text(f"{running} up", style=Colors.Ansi.success))
            if pending > 0:
                if container_parts:
                    container_parts.append(Text(", ", style=Colors.Ansi.text_muted))
                container_parts.append(
                    Text(f"{pending} starting", style=Colors.Ansi.warning)
                )
            if stopping > 0:
                if container_parts:
                    container_parts.append(Text(", ", style=Colors.Ansi.text_muted))
                container_parts.append(
                    Text(f"{stopping} stopping", style=Colors.Ansi.warning)
                )

            if not container_parts:
                container_parts.append(
                    Text(f"0/{desired}", style=Colors.Ansi.text_muted)
                )

            containers_display = Text()
            for part in container_parts:
                containers_display.append_text(part)

            table.add_row(
                name,
                status_display,
                containers_display,
                message,
            )

        # Elapsed time row
        table.add_row(
            "",
            "",
            "",
            Text(f"Elapsed: {self.elapsed_seconds}s", style=Colors.Ansi.text_muted),
        )

        # Failure message
        if self.failure_message:
            table.add_row("", "", "", "")
            table.add_row(
                Text("⚠", style=Colors.Ansi.warning),
                "",
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
            phase = DeployServicePhase(status)
        except ValueError:
            return Colors.Ansi.text_muted

        if phase == DeployServicePhase.RUNNING:
            return Colors.Ansi.success
        elif phase in (DeployServicePhase.ERROR, DeployServicePhase.EXITED):
            return Colors.Ansi.error
        elif phase in (
            DeployServicePhase.STARTING,
            DeployServicePhase.PENDING,
            DeployServicePhase.RESTARTING,
        ):
            return Colors.Ansi.warning

        return Colors.Ansi.text_muted

    def __rich__(self):
        """Support Rich rendering."""
        return self.render()
