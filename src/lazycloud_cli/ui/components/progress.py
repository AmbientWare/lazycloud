from rich.box import ROUNDED
from rich.console import Console, ConsoleOptions, RenderResult
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text


class ProgressCard:
    """A card that displays progress information with status updates."""

    def __init__(
        self,
        title: str,
        current_status: str = "Initializing...",
        steps: list[tuple[str, str]] | None = None,  # (step_name, status)
        show_elapsed: bool = True,
    ):
        """Initialize a ProgressCard.

        Args:
            title: Title of the operation
            current_status: Current status message
            steps: List of (step_name, status) tuples
            show_elapsed: Whether to show elapsed time
        """
        self.title = title
        self.current_status = current_status
        self.steps = steps or []
        self.show_elapsed = show_elapsed

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        """Render the progress card."""
        # Create content table
        table = Table(show_header=False, box=None)
        table.add_column("Status", style="bright_yellow")
        table.add_column("Details")

        # Add current status
        table.add_row("Current:", self.current_status)

        # Add steps if any
        if self.steps:
            table.add_row("", "")  # Spacing
            for step_name, status in self.steps:
                table.add_row(f"{step_name}:", status)

        yield Panel(
            table,
            title=f"[bold]{self.title}[/bold]",
            border_style="yellow",
            box=ROUNDED,
        )


class SpinnerProgress:
    """A simple spinner with status message."""

    def __init__(self, message: str = "Processing..."):
        """Initialize a SpinnerProgress.

        Args:
            message: Initial status message
        """
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            transient=True,
        )
        self.task_id = None
        self.message = message

    def start(self) -> "SpinnerProgress":
        """Start the spinner."""
        self.task_id = self.progress.add_task(self.message, total=None)
        self.progress.start()
        return self

    def update(self, message: str):
        """Update the status message."""
        if self.task_id is not None:
            self.progress.update(self.task_id, description=message)

    def stop(self):
        """Stop the spinner."""
        self.progress.stop()

    def __enter__(self):
        """Context manager entry."""
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop()


class DeploymentProgress:
    """A specialized progress display for deployments."""

    def __init__(self, deployment_name: str):
        """Initialize deployment progress display.

        Args:
            deployment_name: Name of the deployment
        """
        self.deployment_name = deployment_name
        self.steps = []
        self.current_step = None

    def add_step(self, step_name: str, status: str = "pending"):
        """Add a step to track."""
        self.steps.append({"name": step_name, "status": status})

    def update_step(self, step_name: str, status: str):
        """Update the status of a step."""
        for step in self.steps:
            if step["name"] == step_name:
                step["status"] = status
                break

    def update_status(self, status: str, message: str):
        """Update the current step with a status message"""
        # Map status messages to appropriate steps
        if "sending" in message.lower() or "creating" in status.lower():
            self.update_step("Creating deployment resources", message)
        elif "processing" in message.lower() or "progress" in message.lower():
            self.update_step("Creating deployment resources", message)
        elif "completed" in status.lower() or "success" in message.lower():
            self.update_step("Finalizing deployment", message)
        elif "failed" in status.lower() or "error" in message.lower():
            # Update the current in-progress step with failure
            for step in self.steps:
                if "progress" in step["status"].lower() or "pending" in step["status"]:
                    step["status"] = message
                    break
            else:
                # If no step is in progress, update the first pending one
                for step in self.steps:
                    if step["status"] == "pending":
                        step["status"] = message
                        break

    def render(self) -> Panel:
        """Render the deployment progress as a panel."""
        # Create a table for steps
        table = Table(show_header=True, box=None)
        table.add_column("Step", style="cyan")
        table.add_column("Status")

        for step in self.steps:
            status = step["status"]

            # Style based on status
            if "complete" in status.lower():
                status_style = "green"
            elif "fail" in status.lower():
                status_style = "red"
            elif "progress" in status.lower():
                status_style = "yellow"
            else:
                status_style = "dim"

            table.add_row(f"{step['name']}", Text(status, style=status_style))

        return Panel(
            table,
            title=f"[bold]🚀 Deploying: {self.deployment_name}[/bold]",
            title_align="left",
            border_style="blue",
            box=ROUNDED,
        )

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        """Render the deployment progress for Rich console."""
        yield self.render()
